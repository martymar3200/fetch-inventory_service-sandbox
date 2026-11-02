"""
Refile job rules the API now enforces itself instead of trusting the screen:
- reverting to the queue refuses refiled items and Completed jobs
"""
from datetime import datetime, timezone

import pytest

from app.main import app as fastapi_app
from app.auth.dependencies import get_current_user_with_permissions
from app.models.items import Item
from app.models.non_tray_items import NonTrayItem
from app.models.refile_items import RefileItem
from app.models.refile_jobs import RefileJob
from app.models.refile_non_tray_items import RefileNonTrayItem
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
