"""
DELETE /withdraw-jobs/{id} must be refused while the job's pick list is still open.
Deleting it then left the job's items in PickList status with their requests still on
the pick list, which carried on without the job (user decision, 2026-11-03).
"""
from app.models.items import Item, ItemStatus
from app.models.withdraw_jobs import WithdrawJob
from tests.domain.test_batch_upload_request import _requests_for, uploader, warehouse  # noqa: F401
from tests.domain.test_withdraw_job_updates import staff, _job, _patch, _saved  # noqa: F401


def test_an_open_pick_list_blocks_the_delete(staff, session, warehouse):  # noqa: F811
    item = warehouse.tray_item()
    job_id = _job(session, item)
    pick_list_id = _patch(staff, job_id, create_pick_list=True)["pick_list_id"]

    response = staff.delete(f"/withdraw-jobs/{job_id}")

    assert response.status_code == 400, response.text
    assert f"Pick list {pick_list_id}" in response.json()["detail"]
    assert _saved(session, job_id) is not None
    assert session.get(Item, item.id).status == ItemStatus.PickList


def test_a_running_pick_list_blocks_the_delete(staff, session, warehouse):  # noqa: F811
    item = warehouse.tray_item()
    job_id = _job(session, item)
    pick_list_id = _patch(staff, job_id, create_pick_list=True)["pick_list_id"]
    assert staff.patch(f"/pick-lists/{pick_list_id}", json={"status": "Running"}).status_code == 200

    assert staff.delete(f"/withdraw-jobs/{job_id}").status_code == 400
    assert _saved(session, job_id) is not None


def test_a_completed_pick_list_allows_the_delete(staff, session, warehouse):  # noqa: F811
    item = warehouse.tray_item()
    job_id = _job(session, item)
    pick_list_id = _patch(staff, job_id, create_pick_list=True)["pick_list_id"]
    request_id = _requests_for(session, item)[-1].id
    assert staff.patch(f"/pick-lists/{pick_list_id}", json={"status": "Running"}).status_code == 200
    assert staff.patch(f"/pick-lists/{pick_list_id}/update_request/{request_id}", json={"status": "Out"}).status_code == 200
    assert staff.patch(f"/pick-lists/{pick_list_id}", json={"status": "Completed"}).status_code == 200

    assert staff.delete(f"/withdraw-jobs/{job_id}").status_code == 204
    session.expire_all()
    assert session.get(WithdrawJob, job_id) is None


def test_a_job_without_a_pick_list_can_still_be_deleted(staff, session, warehouse):  # noqa: F811
    job_id = _job(session, warehouse.tray_item())

    assert staff.delete(f"/withdraw-jobs/{job_id}").status_code == 204
