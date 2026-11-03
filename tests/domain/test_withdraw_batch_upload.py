"""
POST /batch-upload/withdraw-jobs/{id} (the withdraw screen's Bulk Upload) built
BatchUpload(type="Withdraw"), a column the model doesn't have, so every upload failed
with a 500 before reading a barcode.
"""
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import select

from app.models.batch_upload import BatchUpload
from app.models.item_withdrawals import ItemWithdrawal
from app.models.non_tray_Item_withdrawal import NonTrayItemWithdrawal
from app.models.withdraw_jobs import WithdrawJob
from tests.domain.test_batch_upload_request import uploader, warehouse  # noqa: F401


def _job(session):
    now = datetime.now(timezone.utc)
    job = WithdrawJob(create_dt=now, update_dt=now, last_transition=now)
    session.add(job)
    session.commit()
    return job.id


def _csv(item_barcodes):
    rows = [[barcode, ""] for barcode in item_barcodes]
    return pd.DataFrame(rows, columns=["Item Barcode", "Tray Barcode"]).to_csv(index=False).encode()


def _upload(client, job_id, body):
    return client.post(
        f"/batch-upload/withdraw-jobs/{job_id}",
        files={"file": ("withdraw.csv", body, "text/csv")},
    )


def test_bulk_upload_adds_items_to_the_job(client, test_database, session, uploader, warehouse):  # noqa: F811
    tray_item = warehouse.tray_item()
    non_tray = warehouse.non_tray()
    job_id = _job(session)

    response = _upload(client, job_id, _csv([tray_item.barcode.value, non_tray.barcode.value]))

    assert response.status_code == 200, response.text
    session.expire_all()
    assert session.scalars(select(ItemWithdrawal.item_id).where(ItemWithdrawal.withdraw_job_id == job_id)).all() == [tray_item.id]
    assert session.scalars(
        select(NonTrayItemWithdrawal.non_tray_item_id).where(NonTrayItemWithdrawal.withdraw_job_id == job_id)
    ).all() == [non_tray.id]
    batch = session.scalars(select(BatchUpload).where(BatchUpload.withdraw_job_id == job_id)).one()
    assert batch.status == "Completed"
    assert batch.file_name == "withdraw.csv"


def test_bulk_upload_reports_unknown_barcodes(client, test_database, session, uploader, warehouse):  # noqa: F811
    tray_item = warehouse.tray_item()
    job_id = _job(session)

    response = _upload(client, job_id, _csv([tray_item.barcode.value, "NO-SUCH-BARCODE"]))

    assert response.status_code == 200, response.text
    assert any("NO-SUCH-BARCODE" in error["error"] for error in response.json()["errors"])
    session.expire_all()
    assert session.scalars(select(ItemWithdrawal.item_id).where(ItemWithdrawal.withdraw_job_id == job_id)).all() == [tray_item.id]
