"""
run_time is the time a job spends Running: not waiting to start, and not paused. It feeds
the Worker Efficiency report's Active Time and Items per Hour.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.main import app as fastapi_app
from app.auth.dependencies import get_current_user_with_permissions
from app.models.pick_lists import PickList
from app.models.refile_jobs import RefileJob
from app.models.shelving_jobs import ShelvingJob
from tests.domain.test_batch_upload_request import _requests_for, uploader, warehouse  # noqa: F401


@pytest.fixture
def staff(client, test_database, uploader):  # noqa: F811
    class P:
        def __init__(self, name):
            self.name = name

    class G:
        permissions = [P(name) for name in [
            "can_access_request",
            "can_create_and_submit_manual_requests",
            "can_access_picklist",
            "can_create_picklist_job",
            "process_pick_lists",
            "can_access_shelving",
            "can_create_and_execute_shelving_job",
            "can_edit_shelving_job",
            "process_refile_jobs",
            "can_access_refile",
            "can_view_reports",
            "can_access_reports",
        ]]

    class MockUser:
        id = uploader.id
        first_name = "File"
        last_name = "Importer"
        groups = [G()]

    # the uploader fixture puts the previous user back afterwards
    fastapi_app.dependency_overrides[get_current_user_with_permissions] = lambda: MockUser()
    return client


JOBS = {
    "shelving-jobs": ShelvingJob,
    "refile-jobs": RefileJob,
}


def _new_job(session, model, queued_since, warehouse):  # noqa: F811
    required = {"building_id": warehouse.building_a.id} if model is ShelvingJob else {}
    job = model(create_dt=queued_since, update_dt=queued_since, last_transition=queued_since, **required)
    session.add(job)
    session.commit()
    return job.id


def _run_time(session, model, job_id):
    session.expire_all()
    return session.get(model, job_id).run_time


def _at(start, minutes):
    return (start + timedelta(minutes=minutes)).isoformat()


@pytest.mark.parametrize("path,model", JOBS.items())
def test_only_time_spent_running_counts(staff, session, warehouse, path, model):  # noqa: F811
    created = datetime.now(timezone.utc) - timedelta(days=2)
    job_id = _new_job(session, model, created, warehouse)

    # waits an hour, works 10 minutes, paused 50, works 10 more
    for status, minute in (("Running", 60), ("Paused", 70), ("Running", 120), ("Paused", 130)):
        response = staff.patch(f"/{path}/{job_id}", json={"status": status, "run_timestamp": _at(created, minute)})
        assert response.status_code == 200, response.text

    assert _run_time(session, model, job_id) == timedelta(minutes=20)


@pytest.mark.parametrize("path,model", JOBS.items())
def test_status_changes_without_a_timestamp_still_time_correctly(staff, session, warehouse, path, model):  # noqa: F811
    # Shelving List jobs send no run_timestamp
    job_id = _new_job(session, model, datetime.now(timezone.utc) - timedelta(days=2), warehouse)

    for status in ("Running", "Paused"):
        assert staff.patch(f"/{path}/{job_id}", json={"status": status}).status_code == 200

    assert timedelta(0) <= _run_time(session, model, job_id) < timedelta(minutes=1)


def _pick_list(staff, session, warehouse, count=1):  # noqa: F811
    request_ids = []
    for _ in range(count):
        item = warehouse.tray_item()
        response = staff.post("/requests/", json={
            "barcode_value": item.barcode.value,
            "external_request_id": uuid.uuid4().hex[:10],
        })
        assert response.status_code == 201, response.text
        request_ids.append(_requests_for(session, item)[-1].id)
    response = staff.post("/pick-lists/", json={"request_ids": request_ids})
    assert response.status_code == 201, response.text
    return response.json()["id"], request_ids


def test_pick_list_from_creation_through_completion(staff, session, warehouse):  # noqa: F811
    pick_list_id, request_ids = _pick_list(staff, session, warehouse)
    created = datetime.now(timezone.utc) - timedelta(days=2)
    session.get(PickList, pick_list_id).last_transition = created
    session.commit()

    def change(status, minute):
        response = staff.patch(
            f"/pick-lists/{pick_list_id}", json={"status": status, "run_timestamp": _at(created, minute)}
        )
        assert response.status_code == 200, response.text
        return response.json()

    change("Running", 60)
    assert change("Paused", 70)["run_time"] == "00:10:00"
    change("Running", 120)
    assert staff.patch(
        f"/pick-lists/{pick_list_id}/update_request/{request_ids[0]}", json={"status": "Out"}
    ).status_code == 200

    assert change("Completed", 130)["run_time"] == "00:20:00"
    # a repeat of Complete adds nothing
    assert change("Completed", 500)["run_time"] == "00:20:00"


def test_report_shows_full_hours_past_a_day_and_rates_from_the_same_total(staff, session, warehouse, uploader):  # noqa: F811
    pick_list_id, _ = _pick_list(staff, session, warehouse, count=2)
    pick_list = session.get(PickList, pick_list_id)
    pick_list.assigned_user_id = uploader.id
    # as a stored interval: 3 days and 2 hours 5 minutes
    pick_list.run_time = timedelta(days=3, hours=2, minutes=5)
    session.commit()

    response = staff.get(f"/reporting/worker-efficiency/?user_id={uploader.id}&job_type=PickList")

    assert response.status_code == 200, response.text
    row = response.json()["items"][0]
    assert row["total_active_time"] == "74:05:00"
    assert row["items_per_hour"] == 0
