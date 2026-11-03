"""
PATCH /withdraw-jobs/{id} must save what it's told. Since the 2.0 release it saved neither
status nor assignee, so finished jobs stayed open and the audit trail recorded changes
that hadn't happened.
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.main import app as fastapi_app
from app.auth.dependencies import get_current_user_with_permissions
from app.models.audit_trails import AuditTrail
from app.models.items import Item, ItemStatus
from app.models.users import User
from app.models.withdraw_jobs import WithdrawJob
from tests.domain.test_batch_upload_request import _requests_for, uploader, warehouse  # noqa: F401


@pytest.fixture
def staff(client, test_database, uploader):  # noqa: F811
    class P:
        def __init__(self, name):
            self.name = name

    class G:
        permissions = [P(name) for name in [
            "can_access_withdraw",
            "can_access_request",
            "can_access_picklist",
            "process_pick_lists",
        ]]

    class MockUser:
        id = uploader.id
        first_name = "File"
        last_name = "Importer"
        groups = [G()]

    # the uploader fixture puts the previous user back afterwards
    fastapi_app.dependency_overrides[get_current_user_with_permissions] = lambda: MockUser()
    return client


def _job(session, *items):
    now = datetime.now(timezone.utc)
    job = WithdrawJob(create_dt=now, update_dt=now, last_transition=now)
    for item in items:
        job.items.append(session.get(Item, item.id))
    session.add(job)
    session.commit()
    return job.id


def _out_job(session, *items):
    """A job whose items have been pulled (Out), so it may be completed."""
    for item in items:
        session.get(Item, item.id).status = ItemStatus.Out
    session.commit()
    return _job(session, *items)


def _worker(session):
    user = User(first_name="Other", last_name="Worker", email=f"{uuid.uuid4().hex[:10]}@t.test")
    session.add(user)
    session.commit()
    return user


def _saved(session, job_id):
    session.expire_all()
    return session.get(WithdrawJob, job_id)


def _patch(client, job_id, **fields):
    response = client.patch(f"/withdraw-jobs/{job_id}", json=fields)
    assert response.status_code == 200, response.text
    return response.json()


def _audit_messages(session, job_id, fragment):
    session.expire_all()
    rows = session.execute(
        select(AuditTrail).where(AuditTrail.job_type == "withdraw_jobs", AuditTrail.job_id == str(job_id))
    ).scalars().all()
    return [row.description for row in rows if fragment in row.description]


def test_complete_withdraws_items_and_saves_the_status(staff, session, warehouse):  # noqa: F811
    item = warehouse.tray_item()
    job_id = _out_job(session, item)

    body = _patch(staff, job_id, status="Completed", assigned_user_id=_worker(session).id)

    assert body["status"] == "Completed"
    assert _saved(session, job_id).status == "Completed"
    assert session.get(Item, item.id).status == ItemStatus.Withdrawn


def test_complete_moves_the_job_out_of_the_open_queue(staff, session, warehouse):  # noqa: F811
    job_id = _out_job(session, warehouse.tray_item())
    assert staff.get(f"/withdraw-jobs/?queue=true&workflow_id={job_id}").json()["total"] == 1

    _patch(staff, job_id, status="Completed")

    assert staff.get(f"/withdraw-jobs/?queue=true&workflow_id={job_id}").json()["total"] == 0
    assert staff.get(f"/withdraw-jobs/?status=Completed&workflow_id={job_id}").json()["total"] == 1


def test_assigning_saves_the_user(staff, session, warehouse):  # noqa: F811
    job_id = _job(session, warehouse.tray_item())
    worker = _worker(session)

    body = _patch(staff, job_id, assigned_user_id=worker.id)

    assert body["assigned_user_id"] == worker.id
    assert body["status"] == "Assigned"
    assert _saved(session, job_id).assigned_user_id == worker.id
    assert _audit_messages(session, job_id, "assigned to Other Worker")


def test_unassigning_returns_the_job_to_created(staff, session, warehouse):  # noqa: F811
    job_id = _job(session, warehouse.tray_item())
    _patch(staff, job_id, assigned_user_id=_worker(session).id)

    body = _patch(staff, job_id, assigned_user_id=None)

    assert body["assigned_user_id"] is None
    assert body["status"] == "Created"


def test_audit_trail_matches_what_was_saved(staff, session, warehouse):  # noqa: F811
    job_id = _out_job(session, warehouse.tray_item())

    _patch(staff, job_id, status="Running")
    _patch(staff, job_id, status="Completed")

    assert len(_audit_messages(session, job_id, "to Running")) == 1
    assert len(_audit_messages(session, job_id, "to Completed")) == 1
    assert _saved(session, job_id).status == "Completed"


def test_repeating_complete_changes_nothing(staff, session, warehouse):  # noqa: F811
    item = warehouse.tray_item()
    job_id = _out_job(session, item)
    _patch(staff, job_id, status="Completed")
    session.expire_all()
    withdrawn_barcode = session.get(Item, item.id).withdrawn_barcode_id
    transition = _saved(session, job_id).last_transition

    body = _patch(staff, job_id, status="Completed")

    assert body["status"] == "Completed"
    session.expire_all()
    assert session.get(Item, item.id).withdrawn_barcode_id == withdrawn_barcode
    assert _saved(session, job_id).last_transition == transition
    assert len(_audit_messages(session, job_id, "to Completed")) == 1


@pytest.mark.parametrize("fields", [
    {"status": "Running"},
    {"status": "Created"},
    {"create_pick_list": True},
    {"add_to_picklist": True},
])
def test_completed_job_can_not_reopen_or_take_on_more(staff, session, warehouse, fields):  # noqa: F811
    job_id = _out_job(session, warehouse.tray_item())
    _patch(staff, job_id, status="Completed")

    response = staff.patch(f"/withdraw-jobs/{job_id}", json=fields)

    assert response.status_code == 400
    assert _saved(session, job_id).status == "Completed"


def test_completed_job_can_not_be_deleted(staff, session, warehouse):  # noqa: F811
    job_id = _out_job(session, warehouse.tray_item())
    _patch(staff, job_id, status="Completed")

    assert staff.delete(f"/withdraw-jobs/{job_id}").status_code == 400
    assert _saved(session, job_id) is not None


def test_unfinished_job_can_still_be_deleted(staff, session, warehouse):  # noqa: F811
    job_id = _job(session, warehouse.tray_item())

    assert staff.delete(f"/withdraw-jobs/{job_id}").status_code == 204


def test_fields_the_screen_never_sends_stay_ignored(staff, session, warehouse):  # noqa: F811
    job_id = _job(session, warehouse.tray_item())

    _patch(staff, job_id, created_by_id=_worker(session).id, run_time="99:00:00", pick_list_id=123)

    job = _saved(session, job_id)
    assert job.created_by_id is None
    assert job.pick_list_id is None
    assert job.run_time.total_seconds() < 60


def test_whole_flow_through_a_pick_list(staff, session, warehouse):  # noqa: F811
    item = warehouse.tray_item()
    job_id = _job(session, item)

    pick_list_id = _patch(staff, job_id, create_pick_list=True)["pick_list_id"]
    assert pick_list_id
    session.expire_all()
    assert session.get(Item, item.id).status == ItemStatus.PickList

    request_id = _requests_for(session, item)[-1].id
    assert staff.patch(f"/pick-lists/{pick_list_id}", json={"status": "Running"}).status_code == 200
    assert staff.patch(f"/pick-lists/{pick_list_id}/update_request/{request_id}", json={"status": "Out"}).status_code == 200
    assert staff.patch(f"/pick-lists/{pick_list_id}", json={"status": "Completed"}).status_code == 200
    session.expire_all()
    assert session.get(Item, item.id).status == ItemStatus.Out

    body = _patch(staff, job_id, status="Completed")

    assert body["status"] == "Completed"
    session.expire_all()
    assert session.get(Item, item.id).status == ItemStatus.Withdrawn
