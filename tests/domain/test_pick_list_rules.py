"""
The server enforces the pick list workflow. Offline replay, stale screens and staff sharing
a job can all send requests the screens would normally prevent.
"""
from datetime import datetime, timezone

import pytest

from app.main import app as fastapi_app
from app.auth.dependencies import get_current_user_with_permissions
from app.models.items import Item, ItemStatus
from app.models.non_tray_items import NonTrayItem, NonTrayItemStatus
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
