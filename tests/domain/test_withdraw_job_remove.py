"""
DELETE /withdraw-jobs/{id}/remove_items must work for non-tray items and trays. Both
branches referred to an undefined name, so they raised NameError (a 500) every time.
"""
from datetime import datetime, timezone

from sqlalchemy import select

from app.models.item_withdrawals import ItemWithdrawal
from app.models.non_tray_Item_withdrawal import NonTrayItemWithdrawal
from app.models.non_tray_items import NonTrayItem
from app.models.tray_withdrawal import TrayWithdrawal
from app.models.trays import Tray
from app.models.withdraw_jobs import WithdrawJob
from tests.domain.test_batch_upload_request import uploader, warehouse  # noqa: F401
from tests.domain.test_withdraw_job_updates import staff  # noqa: F401


def _new_job(client):
    response = client.post("/withdraw-jobs/", json={})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _add(client, job_id, barcode):
    response = client.post(f"/withdraw-jobs/{job_id}/add_items", json={"barcode_value": barcode})
    assert response.status_code == 200, response.text


def _remove(client, job_id, barcode):
    return client.request("DELETE", f"/withdraw-jobs/{job_id}/remove_items", json={"barcode_value": barcode})


def _links(session, model, **where):
    session.expire_all()
    query = select(model)
    for column, value in where.items():
        query = query.where(getattr(model, column) == value)
    return session.scalars(query).all()


def test_removing_a_non_tray_item(staff, session, warehouse):  # noqa: F811
    non_tray = warehouse.non_tray()
    job_id = _new_job(staff)
    _add(staff, job_id, non_tray.barcode.value)

    response = _remove(staff, job_id, non_tray.barcode.value)

    assert response.status_code == 200, response.text
    assert response.json()["non_tray_items"] == []
    assert _links(session, NonTrayItemWithdrawal, withdraw_job_id=job_id) == []
    assert session.get(NonTrayItem, non_tray.id).status == "In"


def test_removing_a_tray(staff, session, warehouse):  # noqa: F811
    item = warehouse.tray_item()
    tray = session.get(Tray, item.tray_id)
    job_id = _new_job(staff)
    _add(staff, job_id, tray.barcode.value)

    response = _remove(staff, job_id, tray.barcode.value)

    assert response.status_code == 200, response.text
    assert response.json()["trays"] == []
    assert response.json()["items"] == []
    assert _links(session, TrayWithdrawal, withdraw_job_id=job_id) == []
    assert _links(session, ItemWithdrawal, withdraw_job_id=job_id) == []


def test_removing_from_one_job_leaves_other_jobs_alone(staff, session, warehouse):  # noqa: F811
    non_tray = warehouse.non_tray()
    other_job_id = _new_job(staff)
    _add(staff, other_job_id, non_tray.barcode.value)
    # an older link from a finished job, which add_items allows
    session.get(WithdrawJob, other_job_id).status = "Completed"
    session.commit()
    job_id = _new_job(staff)
    _add(staff, job_id, non_tray.barcode.value)

    assert _remove(staff, job_id, non_tray.barcode.value).status_code == 200

    assert len(_links(session, NonTrayItemWithdrawal, withdraw_job_id=other_job_id)) == 1
    assert _links(session, NonTrayItemWithdrawal, withdraw_job_id=job_id) == []
