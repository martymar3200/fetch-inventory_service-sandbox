"""
Refile job rules the API now enforces itself instead of trusting the screen:
- reverting to the queue refuses refiled items and Completed jobs
- each item reverted to the queue is recorded in the job's history
- scans are taken only while the job is Running, and only for items in the job
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.main import app as fastapi_app
from app.auth.dependencies import get_current_user_with_permissions
from app.models.audit_trails import AuditTrail
from app.models.items import Item
from app.models.non_tray_items import NonTrayItem
from app.models.refile_items import RefileItem
from app.models.refile_jobs import RefileJob
from app.models.refile_non_tray_items import RefileNonTrayItem
from app.services.audit_service import AuditEventType
from tests.domain.test_batch_upload_request import uploader, warehouse  # noqa: F401


@pytest.fixture
def staff(client, test_database, uploader):  # noqa: F811
    class P:
        def __init__(self, name):
            self.name = name

    class G:
        permissions = [P(name) for name in [
            "can_access_refile",
            "process_refile_jobs",
        ]]

    class MockUser:
        id = uploader.id
        first_name = "Refile"
        last_name = "Worker"
        groups = [G()]

    # the uploader fixture puts the previous user back afterwards
    fastapi_app.dependency_overrides[get_current_user_with_permissions] = lambda: MockUser()
    return client


def _out(session, entity):
    entity.status = "Out"
    session.commit()
    return entity


def _job(session, *entities, status="Running"):
    now = datetime.now(timezone.utc)
    job = RefileJob(status=status, create_dt=now, update_dt=now, last_transition=now)
    session.add(job)
    session.commit()
    for entity in entities:
        if isinstance(entity, Item):
            session.add(RefileItem(refile_job_id=job.id, item_id=entity.id))
        else:
            session.add(RefileNonTrayItem(refile_job_id=job.id, non_tray_item_id=entity.id))
    session.commit()
    return job.id


def _revert(client, job_id, *entities):
    return client.request(
        "DELETE",
        f"/refile-jobs/{job_id}/remove_items",
        json={"barcode_values": [entity.barcode.value for entity in entities]},
    )


def _in_job(session, job_id, entity):
    session.expire_all()
    if isinstance(entity, Item):
        link = session.query(RefileItem).filter_by(refile_job_id=job_id, item_id=entity.id)
    else:
        link = session.query(RefileNonTrayItem).filter_by(refile_job_id=job_id, non_tray_item_id=entity.id)
    return link.first() is not None


def _queued(session, entity):
    session.expire_all()
    return session.get(type(entity), entity.id).scanned_for_refile_queue


def _removal_entries(session, job_id):
    session.expire_all()
    return session.execute(
        select(AuditTrail).where(
            AuditTrail.job_type == "refile_jobs",
            AuditTrail.job_id == str(job_id),
            AuditTrail.event_type == AuditEventType.ITEM_REMOVED,
        )
    ).scalars().all()


def test_revert_sends_pending_items_back_to_the_queue(staff, session, warehouse):  # noqa: F811
    item = _out(session, warehouse.tray_item())
    non_tray = _out(session, warehouse.non_tray())
    job_id = _job(session, item, non_tray)

    response = _revert(staff, job_id, item, non_tray)

    assert response.status_code == 200, response.text
    assert not _in_job(session, job_id, item)
    assert not _in_job(session, job_id, non_tray)
    assert _queued(session, item)
    assert _queued(session, non_tray)


def test_revert_records_each_removal_in_the_history(staff, session, warehouse):  # noqa: F811
    item = _out(session, warehouse.tray_item())
    non_tray = _out(session, warehouse.non_tray())
    job_id = _job(session, item, non_tray)

    _revert(staff, job_id, item, non_tray)

    entries = _removal_entries(session, job_id)
    assert {(row.entity_type, row.entity_id) for row in entries} == {
        ("items", str(item.id)),
        ("non_tray_items", str(non_tray.id)),
    }
    assert any(item.barcode.value in row.description for row in entries)
    assert any(non_tray.barcode.value in row.description for row in entries)


def test_refused_revert_records_nothing(staff, session, warehouse):  # noqa: F811
    pending = _out(session, warehouse.tray_item())
    refiled = warehouse.tray_item()
    job_id = _job(session, pending, refiled)

    assert _revert(staff, job_id, pending, refiled).status_code == 400
    assert _removal_entries(session, job_id) == []


@pytest.mark.parametrize("kind", ["tray_item", "non_tray"])
def test_revert_refuses_refiled_items_and_changes_nothing(staff, session, warehouse, kind):  # noqa: F811
    pending = _out(session, warehouse.tray_item())
    refiled = getattr(warehouse, kind)()  # status In, as after a refile scan
    job_id = _job(session, pending, refiled)

    response = _revert(staff, job_id, pending, refiled)

    assert response.status_code == 400
    assert refiled.barcode.value in response.json()["detail"]
    assert pending.barcode.value not in response.json()["detail"]
    # the pending item in the same request stays too
    assert _in_job(session, job_id, pending)
    assert _in_job(session, job_id, refiled)
    assert not _queued(session, pending)
    assert not _queued(session, refiled)


def test_revert_refuses_a_completed_job(staff, session, warehouse):  # noqa: F811
    item = _out(session, warehouse.tray_item())
    job_id = _job(session, item, status="Completed")

    response = _revert(staff, job_id, item)

    assert response.status_code == 400
    assert "Completed" in response.json()["detail"]
    assert _in_job(session, job_id, item)
    assert not _queued(session, item)


@pytest.mark.parametrize("status", ["Created", "Assigned", "Paused"])
def test_revert_still_works_before_completion(staff, session, warehouse, status):  # noqa: F811
    item = _out(session, warehouse.tray_item())
    job_id = _job(session, item, status=status)

    assert _revert(staff, job_id, item).status_code == 200
    assert not _in_job(session, job_id, item)


def _scan(client, job_id, entity):
    if isinstance(entity, Item):
        url = f"/refile-jobs/{job_id}/update_item/{entity.id}"
    else:
        url = f"/refile-jobs/{job_id}/update_non_tray_items/{entity.id}"
    return client.patch(url, json={})


def _status(session, entity):
    session.expire_all()
    return session.get(type(entity), entity.id).status


@pytest.mark.parametrize("kind", ["tray_item", "non_tray"])
def test_scan_refiles_an_item_in_a_running_job(staff, session, warehouse, kind):  # noqa: F811
    entity = _out(session, getattr(warehouse, kind)())
    job_id = _job(session, entity)

    assert _scan(staff, job_id, entity).status_code == 200, "scan refused"
    assert _status(session, entity) == "In"


@pytest.mark.parametrize("kind", ["tray_item", "non_tray"])
@pytest.mark.parametrize("status", ["Created", "Assigned", "Paused", "Completed"])
def test_scan_refuses_a_job_that_is_not_running(staff, session, warehouse, kind, status):  # noqa: F811
    entity = _out(session, getattr(warehouse, kind)())
    job_id = _job(session, entity, status=status)

    response = _scan(staff, job_id, entity)

    assert response.status_code == 400
    assert status in response.json()["detail"]
    assert _status(session, entity) == "Out"


@pytest.mark.parametrize("kind", ["tray_item", "non_tray"])
def test_scan_refuses_an_item_not_in_the_job(staff, session, warehouse, kind):  # noqa: F811
    stranger = _out(session, getattr(warehouse, kind)())
    job_id = _job(session, _out(session, getattr(warehouse, kind)()))

    response = _scan(staff, job_id, stranger)

    assert response.status_code == 400
    assert f"not in Refile Job ID {job_id}" in response.json()["detail"]
    assert _status(session, stranger) == "Out"


def test_scan_refuses_an_item_reverted_by_someone_else(staff, session, warehouse):  # noqa: F811
    item = _out(session, warehouse.tray_item())
    job_id = _job(session, item)
    assert _revert(staff, job_id, item).status_code == 200

    # a second worker's screen still lists the item and scans it
    assert _scan(staff, job_id, item).status_code == 400
    assert _status(session, item) == "Out"
    assert _queued(session, item)


def test_scan_uses_the_item_type_of_the_endpoint(staff, session, warehouse):  # noqa: F811
    # a tray item's id sent to the non-tray endpoint must not refile a non-tray item with that id
    item = _out(session, warehouse.tray_item())
    job_id = _job(session, item)

    response = staff.patch(f"/refile-jobs/{job_id}/update_non_tray_items/{item.id}", json={})

    assert response.status_code in (400, 404)
    assert _status(session, item) == "Out"
