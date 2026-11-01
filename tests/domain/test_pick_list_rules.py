"""
The server enforces the pick list workflow. Offline replay, stale screens and staff sharing
a job can all send requests the screens would normally prevent.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select

from app.main import app as fastapi_app
from app.auth.dependencies import get_current_user_with_permissions
from app.models.item_retrieval_events import ItemRetrievalEvent
from app.models.items import Item, ItemStatus
from app.models.non_tray_items import NonTrayItem, NonTrayItemStatus
from app.models.pick_lists import PickList
from app.models.requests import Request
from app.models.withdraw_jobs import WithdrawJob
from tests.domain.test_batch_upload_request import _requests_for, uploader, warehouse  # noqa: F401


@pytest.fixture
def staff(client, test_database, uploader):
    class P:
        def __init__(self, name):
            self.name = name

    class G:
        permissions = [
            P("can_access_request"),
            P("can_create_and_submit_manual_requests"),
            P("can_access_picklist"),
            P("can_create_picklist_job"),
            P("can_delete_picklist_job"),
            P("process_pick_lists"),
            P("can_access_withdraw"),
        ]

    class MockUser:
        id = uploader.id
        first_name = "File"
        last_name = "Importer"
        groups = [G()]

    # the uploader fixture puts the previous user back afterwards
    fastapi_app.dependency_overrides[get_current_user_with_permissions] = lambda: MockUser()
    return client


def _new_request(client, session, item, external_id):
    response = client.post("/requests/", json={
        "barcode_value": item.barcode.value,
        "external_request_id": external_id,
    })
    assert response.status_code == 201, response.text
    return _requests_for(session, item)[-1].id


def _status(session, record):
    session.expire_all()
    return session.get(type(record), record.id).status


def test_withdrawal_pick_list_makes_already_requested_items_scannable(staff, session, warehouse):
    tray_item = warehouse.tray_item()
    non_tray_item = warehouse.non_tray()
    tray_request = _new_request(staff, session, tray_item, "WD-TRAY")
    non_tray_request = _new_request(staff, session, non_tray_item, "WD-NON-TRAY")

    now = datetime.now(timezone.utc)
    job = WithdrawJob(create_dt=now, update_dt=now, last_transition=now)
    job.items.append(session.get(Item, tray_item.id))
    job.non_tray_items.append(session.get(NonTrayItem, non_tray_item.id))
    session.add(job)
    session.commit()

    response = staff.patch(f"/withdraw-jobs/{job.id}", json={"create_pick_list": True})

    assert response.status_code == 200, response.text
    session.expire_all()
    pick_list_id = session.get(WithdrawJob, job.id).pick_list_id
    assert session.get(Request, tray_request).pick_list_id == pick_list_id
    assert session.get(Request, non_tray_request).pick_list_id == pick_list_id
    assert _status(session, tray_item) == ItemStatus.PickList
    assert _status(session, non_tray_item) == NonTrayItemStatus.PickList


def _new_pick_list(client, session, warehouse, count=1):
    items = [warehouse.tray_item() for _ in range(count)]
    request_ids = [_new_request(client, session, item, uuid.uuid4().hex[:10]) for item in items]
    response = client.post("/pick-lists/", json={"request_ids": request_ids})
    assert response.status_code == 201, response.text
    return response.json()["id"], request_ids, items


def _set_status(client, pick_list_id, status):
    return client.patch(f"/pick-lists/{pick_list_id}", json={"status": status})


def _scan(client, pick_list_id, request_id):
    return client.patch(f"/pick-lists/{pick_list_id}/update_request/{request_id}", json={"status": "Out"})


def _retrieval_events(session, pick_list_id):
    session.expire_all()
    return session.scalar(
        select(func.count(ItemRetrievalEvent.id)).where(ItemRetrievalEvent.pick_list_id == pick_list_id)
    )


def _completed_pick_list(client, session, warehouse):
    pick_list_id, request_ids, items = _new_pick_list(client, session, warehouse)
    assert _set_status(client, pick_list_id, "Running").status_code == 200
    assert _scan(client, pick_list_id, request_ids[0]).status_code == 200
    assert _set_status(client, pick_list_id, "Completed").status_code == 200
    return pick_list_id, request_ids, items


@pytest.mark.parametrize("path", [
    ["Running", "Paused", "Running"],
    ["Assigned", "Created"],
    ["Assigned", "Running"],
])
def test_allowed_status_changes(staff, session, warehouse, path):
    pick_list_id, _, _ = _new_pick_list(staff, session, warehouse)

    for status in path:
        response = _set_status(staff, pick_list_id, status)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == status


@pytest.mark.parametrize("path,blocked", [
    ([], "Paused"),
    ([], "Completed"),
    (["Assigned"], "Paused"),
    (["Assigned"], "Completed"),
    (["Running"], "Created"),
    (["Running"], "Assigned"),
    (["Running", "Paused"], "Completed"),
    (["Running", "Paused"], "Created"),
])
def test_blocked_status_changes(staff, session, warehouse, path, blocked):
    pick_list_id, _, _ = _new_pick_list(staff, session, warehouse)
    for status in path:
        assert _set_status(staff, pick_list_id, status).status_code == 200
    current = path[-1] if path else "Created"

    response = _set_status(staff, pick_list_id, blocked)

    assert response.status_code == 400
    assert f"'{current}' to '{blocked}'" in response.json()["detail"]
    assert staff.get(f"/pick-lists/{pick_list_id}").json()["status"] == current


def test_completed_job_can_not_reopen(staff, session, warehouse):
    pick_list_id, _, _ = _completed_pick_list(staff, session, warehouse)

    assert _set_status(staff, pick_list_id, "Running").status_code == 400
    assert staff.get(f"/pick-lists/{pick_list_id}").json()["status"] == "Completed"


def test_complete_rejected_while_items_are_unscanned(staff, session, warehouse):
    pick_list_id, request_ids, items = _new_pick_list(staff, session, warehouse, count=2)
    assert _set_status(staff, pick_list_id, "Running").status_code == 200
    assert _scan(staff, pick_list_id, request_ids[0]).status_code == 200

    response = _set_status(staff, pick_list_id, "Completed")

    assert response.status_code == 400
    assert "1 item(s) have not been scanned" in response.json()["detail"]
    session.expire_all()
    assert session.get(Request, request_ids[1]).fulfilled is False
    assert _status(session, items[1]) == ItemStatus.PickList
    assert staff.get(f"/pick-lists/{pick_list_id}").json()["status"] == "Running"
    assert _retrieval_events(session, pick_list_id) == 0

    assert _scan(staff, pick_list_id, request_ids[1]).status_code == 200
    assert _set_status(staff, pick_list_id, "Completed").status_code == 200
    assert _retrieval_events(session, pick_list_id) == 2


def test_complete_rejected_with_no_items(staff, session, warehouse):
    pick_list_id, request_ids, _ = _new_pick_list(staff, session, warehouse)
    assert _set_status(staff, pick_list_id, "Running").status_code == 200
    assert staff.delete(f"/pick-lists/{pick_list_id}/remove_request/{request_ids[0]}").status_code == 200

    response = _set_status(staff, pick_list_id, "Completed")

    assert response.status_code == 400
    assert "no items" in response.json()["detail"]


def test_repeated_complete_changes_nothing(staff, session, warehouse, monkeypatch):
    check_in = MagicMock()
    monkeypatch.setattr("app.ils.tasks.check_in_picklist_item_async", check_in)
    pick_list_id, _, _ = _completed_pick_list(staff, session, warehouse)
    assert _retrieval_events(session, pick_list_id) == 1
    assert check_in.call_count == 1

    response = _set_status(staff, pick_list_id, "Completed")

    assert response.status_code == 200
    assert response.json()["status"] == "Completed"
    assert _retrieval_events(session, pick_list_id) == 1
    assert check_in.call_count == 1


def test_scan_retrieves_the_item(staff, session, warehouse):
    pick_list_id, request_ids, items = _new_pick_list(staff, session, warehouse)
    assert _set_status(staff, pick_list_id, "Running").status_code == 200

    response = _scan(staff, pick_list_id, request_ids[0])

    assert response.status_code == 200
    session.expire_all()
    assert session.get(Request, request_ids[0]).fulfilled is True
    assert _status(session, items[0]) == ItemStatus.Out


def test_scan_of_non_tray_item(staff, session, warehouse):
    item = warehouse.non_tray()
    request_id = _new_request(staff, session, item, uuid.uuid4().hex[:10])
    pick_list_id = staff.post("/pick-lists/", json={"request_ids": [request_id]}).json()["id"]
    assert _set_status(staff, pick_list_id, "Running").status_code == 200

    assert _scan(staff, pick_list_id, request_id).status_code == 200
    assert _status(session, item) == NonTrayItemStatus.Out


def test_scan_without_status_retrieves_the_item(staff, session, warehouse):
    pick_list_id, request_ids, items = _new_pick_list(staff, session, warehouse)
    assert _set_status(staff, pick_list_id, "Running").status_code == 200

    response = staff.patch(f"/pick-lists/{pick_list_id}/update_request/{request_ids[0]}", json={})

    assert response.status_code == 200
    assert _status(session, items[0]) == ItemStatus.Out


@pytest.mark.parametrize("status", ["In", "Withdrawn", "Banana"])
def test_scan_rejects_other_statuses(staff, session, warehouse, status):
    pick_list_id, request_ids, items = _new_pick_list(staff, session, warehouse)
    assert _set_status(staff, pick_list_id, "Running").status_code == 200

    response = staff.patch(
        f"/pick-lists/{pick_list_id}/update_request/{request_ids[0]}", json={"status": status}
    )

    assert response.status_code == 422
    session.expire_all()
    assert session.get(Request, request_ids[0]).fulfilled is False
    assert _status(session, items[0]) == ItemStatus.PickList


@pytest.mark.parametrize("path", [[], ["Assigned"]])
def test_scan_rejected_before_the_job_starts(staff, session, warehouse, path):
    pick_list_id, request_ids, items = _new_pick_list(staff, session, warehouse)
    for status in path:
        assert _set_status(staff, pick_list_id, status).status_code == 200

    response = _scan(staff, pick_list_id, request_ids[0])

    assert response.status_code == 400
    session.expire_all()
    assert session.get(Request, request_ids[0]).fulfilled is False
    assert _status(session, items[0]) == ItemStatus.PickList


def test_scan_accepted_while_paused(staff, session, warehouse):
    pick_list_id, request_ids, items = _new_pick_list(staff, session, warehouse)
    assert _set_status(staff, pick_list_id, "Running").status_code == 200
    assert _set_status(staff, pick_list_id, "Paused").status_code == 200

    assert _scan(staff, pick_list_id, request_ids[0]).status_code == 200
    assert _status(session, items[0]) == ItemStatus.Out


def test_late_scan_does_not_undo_retrieved(staff, session, warehouse, monkeypatch):
    monkeypatch.setattr("app.routers.pick_lists.get_setting_value", lambda *args, **kwargs: "true")
    pick_list_id, request_ids, items = _completed_pick_list(staff, session, warehouse)
    assert _status(session, items[0]) == ItemStatus.Retrieved

    response = _scan(staff, pick_list_id, request_ids[0])

    assert response.status_code == 200
    assert _status(session, items[0]) == ItemStatus.Retrieved


def test_repeated_scan_changes_nothing(staff, session, warehouse):
    pick_list_id, request_ids, _ = _new_pick_list(staff, session, warehouse)
    assert _set_status(staff, pick_list_id, "Running").status_code == 200
    assert _scan(staff, pick_list_id, request_ids[0]).status_code == 200
    session.expire_all()
    first_update = session.get(Request, request_ids[0]).update_dt

    assert _scan(staff, pick_list_id, request_ids[0]).status_code == 200

    session.expire_all()
    assert session.get(Request, request_ids[0]).update_dt == first_update


def _pick_list_count(session):
    session.expire_all()
    return session.scalar(select(func.count(PickList.id)))


def test_create_with_missing_id_saves_nothing(staff, session, warehouse):
    item = warehouse.tray_item()
    request_id = _new_request(staff, session, item, uuid.uuid4().hex[:10])
    before = _pick_list_count(session)

    response = staff.post("/pick-lists/", json={"request_ids": [request_id, 99999999]})

    assert response.status_code == 400
    assert "99999999" in response.json()["detail"]
    assert _pick_list_count(session) == before
    assert session.get(Request, request_id).pick_list_id is None
    assert _status(session, item) == ItemStatus.Requested


def test_create_with_no_ids_is_rejected(staff, session, warehouse):
    assert staff.post("/pick-lists/", json={"request_ids": []}).status_code == 400


def test_create_with_a_repeated_id_works(staff, session, warehouse):
    item = warehouse.tray_item()
    request_id = _new_request(staff, session, item, uuid.uuid4().hex[:10])

    response = staff.post("/pick-lists/", json={"request_ids": [request_id, request_id]})

    assert response.status_code == 201
    assert response.json()["request_count"] == 1


def test_create_can_not_take_a_request_from_another_list(staff, session, warehouse):
    pick_list_id, request_ids, _ = _new_pick_list(staff, session, warehouse)

    response = staff.post("/pick-lists/", json={"request_ids": request_ids})

    assert response.status_code == 400
    assert str(request_ids[0]) in response.json()["detail"]
    session.expire_all()
    assert session.get(Request, request_ids[0]).pick_list_id == pick_list_id


def test_add_can_not_take_a_request_from_another_list(staff, session, warehouse):
    source_id, request_ids, _ = _new_pick_list(staff, session, warehouse)
    target_id, _, _ = _new_pick_list(staff, session, warehouse)

    response = staff.patch(f"/pick-lists/{target_id}/add_request", json={"request_ids": request_ids})

    assert response.status_code == 400
    session.expire_all()
    assert session.get(Request, request_ids[0]).pick_list_id == source_id


@pytest.mark.parametrize("shipping", ["false", "true"])
def test_finished_request_can_not_go_on_a_new_list(staff, session, warehouse, monkeypatch, shipping):
    monkeypatch.setattr("app.routers.pick_lists.get_setting_value", lambda *args, **kwargs: shipping)
    source_id, request_ids, items = _completed_pick_list(staff, session, warehouse)
    status_before = _status(session, items[0])

    response = staff.post("/pick-lists/", json={"request_ids": request_ids})

    assert response.status_code == 400
    session.expire_all()
    assert session.get(Request, request_ids[0]).pick_list_id == source_id
    assert _status(session, items[0]) == status_before


def test_deleted_request_can_not_go_on_a_list(staff, session, warehouse):
    item = warehouse.tray_item()
    request_id = _new_request(staff, session, item, uuid.uuid4().hex[:10])
    session.get(Request, request_id).deleted = True
    session.commit()

    assert staff.post("/pick-lists/", json={"request_ids": [request_id]}).status_code == 400


def test_create_rejects_mixed_buildings(staff, session, warehouse):
    request_ids = [
        _new_request(staff, session, warehouse.tray_item(warehouse.ladder_a), uuid.uuid4().hex[:10]),
        _new_request(staff, session, warehouse.tray_item(warehouse.ladder_b), uuid.uuid4().hex[:10]),
    ]
    before = _pick_list_count(session)

    response = staff.post("/pick-lists/", json={"request_ids": request_ids})

    assert response.status_code == 400
    assert "same building" in response.json()["detail"]
    assert _pick_list_count(session) == before


def test_add_rejects_a_request_from_another_building(staff, session, warehouse):
    pick_list_id, _, _ = _new_pick_list(staff, session, warehouse)
    request_id = _new_request(staff, session, warehouse.tray_item(warehouse.ladder_b), uuid.uuid4().hex[:10])

    response = staff.patch(f"/pick-lists/{pick_list_id}/add_request", json={"request_ids": [request_id]})

    assert response.status_code == 400
    assert "different building" in response.json()["detail"]
    session.expire_all()
    assert session.get(Request, request_id).pick_list_id is None


def test_add_a_free_request_from_the_same_building(staff, session, warehouse):
    pick_list_id, _, _ = _new_pick_list(staff, session, warehouse)
    item = warehouse.tray_item()
    request_id = _new_request(staff, session, item, uuid.uuid4().hex[:10])

    response = staff.patch(f"/pick-lists/{pick_list_id}/add_request", json={"request_ids": [request_id]})

    assert response.status_code == 200
    assert response.json()["request_count"] == 2
    assert _status(session, item) == ItemStatus.PickList


def test_add_with_only_a_missing_id_is_rejected(staff, session, warehouse):
    pick_list_id, _, _ = _new_pick_list(staff, session, warehouse)

    response = staff.patch(f"/pick-lists/{pick_list_id}/add_request", json={"request_ids": [99999999]})

    assert response.status_code == 400
    assert "99999999" in response.json()["detail"]


def test_add_to_a_missing_pick_list_is_not_found(staff, session, warehouse):
    assert staff.patch("/pick-lists/99999999/add_request", json={"request_ids": [1]}).status_code == 404


def test_remove_can_not_reach_another_list(staff, session, warehouse):
    other_id, _, _ = _new_pick_list(staff, session, warehouse)
    pick_list_id, request_ids, items = _new_pick_list(staff, session, warehouse)

    response = staff.delete(f"/pick-lists/{other_id}/remove_request/{request_ids[0]}")

    assert response.status_code == 404
    session.expire_all()
    assert session.get(Request, request_ids[0]).pick_list_id == pick_list_id
    assert _status(session, items[0]) == ItemStatus.PickList


def test_remove_rejected_once_scanned(staff, session, warehouse):
    pick_list_id, request_ids, items = _new_pick_list(staff, session, warehouse)
    assert _set_status(staff, pick_list_id, "Running").status_code == 200
    assert _scan(staff, pick_list_id, request_ids[0]).status_code == 200

    response = staff.delete(f"/pick-lists/{pick_list_id}/remove_request/{request_ids[0]}")

    assert response.status_code == 400
    assert _status(session, items[0]) == ItemStatus.Out


def test_remove_returns_an_unscanned_item_to_the_queue(staff, session, warehouse):
    pick_list_id, request_ids, items = _new_pick_list(staff, session, warehouse)

    response = staff.delete(f"/pick-lists/{pick_list_id}/remove_request/{request_ids[0]}")

    assert response.status_code == 200
    session.expire_all()
    assert session.get(Request, request_ids[0]).pick_list_id is None
    assert _status(session, items[0]) == ItemStatus.Requested


def test_delete_rejected_when_completed(staff, session, warehouse):
    pick_list_id, request_ids, items = _completed_pick_list(staff, session, warehouse)

    response = staff.delete(f"/pick-lists/{pick_list_id}")

    assert response.status_code == 400
    session.expire_all()
    assert session.get(PickList, pick_list_id) is not None
    assert session.get(Request, request_ids[0]).pick_list_id == pick_list_id
    assert _status(session, items[0]) == ItemStatus.Out


def test_delete_rejected_once_an_item_is_scanned(staff, session, warehouse):
    pick_list_id, request_ids, items = _new_pick_list(staff, session, warehouse, count=2)
    assert _set_status(staff, pick_list_id, "Running").status_code == 200
    assert _scan(staff, pick_list_id, request_ids[0]).status_code == 200

    response = staff.delete(f"/pick-lists/{pick_list_id}")

    assert response.status_code == 400
    assert "already been retrieved" in response.json()["detail"]
    assert _status(session, items[0]) == ItemStatus.Out
    assert _status(session, items[1]) == ItemStatus.PickList


def test_delete_returns_unscanned_items_to_the_queue(staff, session, warehouse):
    pick_list_id, request_ids, items = _new_pick_list(staff, session, warehouse)
    assert _set_status(staff, pick_list_id, "Running").status_code == 200

    assert staff.delete(f"/pick-lists/{pick_list_id}").status_code == 204

    session.expire_all()
    assert session.get(PickList, pick_list_id) is None
    assert session.get(Request, request_ids[0]).pick_list_id is None
    assert _status(session, items[0]) == ItemStatus.Requested
