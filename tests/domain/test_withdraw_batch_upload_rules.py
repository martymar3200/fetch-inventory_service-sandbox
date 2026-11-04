"""
A withdraw Bulk Upload row must follow the same rules as scanning that barcode into the
job. The upload used to report some problems but add the item anyway (unshelved items,
Requested non-tray items, non-tray items in another open job), returned a 500 for items
already in the job, ignored the template's Tray Barcode column, and gave line numbers
that didn't match the file.
"""
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import select

from app.models.batch_upload import BatchUpload
from app.models.item_withdrawals import ItemWithdrawal
from app.models.items import Item
from app.models.non_tray_Item_withdrawal import NonTrayItemWithdrawal
from app.models.non_tray_items import NonTrayItem
from app.models.tray_withdrawal import TrayWithdrawal
from app.models.trays import Tray
from app.models.withdraw_jobs import WithdrawJob
from tests.domain.test_batch_upload_request import uploader, warehouse  # noqa: F401
from tests.domain.test_move_discrepancies import _make_item
from tests.domain.test_withdraw_batch_upload import _job, _upload


def _file(rows, columns=("Item Barcode", "Tray Barcode")):
    return pd.DataFrame(rows, columns=list(columns)).to_csv(index=False).encode()


def _items_in(session, job_id):
    session.expire_all()
    return set(session.scalars(select(ItemWithdrawal.item_id).where(ItemWithdrawal.withdraw_job_id == job_id)))


def _non_trays_in(session, job_id):
    session.expire_all()
    return set(session.scalars(
        select(NonTrayItemWithdrawal.non_tray_item_id).where(NonTrayItemWithdrawal.withdraw_job_id == job_id)
    ))


def _trays_in(session, job_id):
    session.expire_all()
    return set(session.scalars(select(TrayWithdrawal.tray_id).where(TrayWithdrawal.withdraw_job_id == job_id)))


def _link(session, job_id, item=None, non_tray=None):
    """Put an entry in a job the way a scan does."""
    if item:
        session.add(ItemWithdrawal(item_id=item.id, withdraw_job_id=job_id))
        session.add(TrayWithdrawal(tray_id=item.tray_id, withdraw_job_id=job_id))
    if non_tray:
        session.add(NonTrayItemWithdrawal(non_tray_item_id=non_tray.id, withdraw_job_id=job_id))
    session.commit()


def _errors(response):
    return {error["line"]: error["error"] for error in response.json()["errors"]}


def test_an_unshelved_item_is_reported_and_not_added(client, test_database, session, uploader, warehouse):  # noqa: F811
    good, unshelved = warehouse.tray_item(), warehouse.tray_item()
    session.get(Tray, unshelved.tray_id).shelf_position_id = None
    session.commit()
    job_id = _job(session)

    response = _upload(client, job_id, _file([[good.barcode.value, ""], [unshelved.barcode.value, ""]]))

    assert response.status_code == 200, response.text
    assert "not shelved" in _errors(response)[3]
    assert _items_in(session, job_id) == {good.id}


def test_a_requested_non_tray_item_is_reported_and_not_added(client, test_database, session, uploader, warehouse):  # noqa: F811
    good, requested = warehouse.non_tray(), warehouse.non_tray()
    session.get(NonTrayItem, requested.id).status = "Requested"
    session.commit()
    job_id = _job(session)

    response = _upload(client, job_id, _file([[good.barcode.value, ""], [requested.barcode.value, ""]]))

    assert response.status_code == 200, response.text
    assert "Requested" in _errors(response)[3]
    assert _non_trays_in(session, job_id) == {good.id}


def test_entries_in_another_open_job_are_not_added(client, test_database, session, uploader, warehouse):  # noqa: F811
    item, non_tray, good = warehouse.tray_item(), warehouse.non_tray(), warehouse.tray_item()
    _link(session, _job(session), item=item, non_tray=non_tray)
    job_id = _job(session)

    response = _upload(client, job_id, _file([[item.barcode.value, ""], [non_tray.barcode.value, ""], [good.barcode.value, ""]]))

    assert response.status_code == 200, response.text
    errors = _errors(response)
    assert "another withdraw job" in errors[2]
    assert "another withdraw job" in errors[3]
    assert _items_in(session, job_id) == {good.id}
    assert _non_trays_in(session, job_id) == set()


def test_entries_in_a_completed_job_can_be_added_again(client, test_database, session, uploader, warehouse):  # noqa: F811
    non_tray = warehouse.non_tray()
    old_job = _job(session)
    _link(session, old_job, non_tray=non_tray)
    session.get(WithdrawJob, old_job).status = "Completed"
    session.commit()
    job_id = _job(session)

    response = _upload(client, job_id, _file([[non_tray.barcode.value, ""]]))

    assert response.status_code == 200, response.text
    assert _non_trays_in(session, job_id) == {non_tray.id}


def test_an_item_already_in_the_job_is_skipped_not_a_500(client, test_database, session, uploader, warehouse):  # noqa: F811
    scanned = warehouse.tray_item()
    same_tray = _make_item(session, session.get(Tray, scanned.tray_id))
    same_tray.status = "In"
    session.commit()
    job_id = _job(session)
    _link(session, job_id, item=scanned)

    response = _upload(client, job_id, _file([[scanned.barcode.value, ""], [same_tray.barcode.value, ""]]))

    assert response.status_code == 200, response.text
    assert "already in this withdraw job" in _errors(response)[2]
    assert _items_in(session, job_id) == {scanned.id, same_tray.id}
    assert _trays_in(session, job_id) == {scanned.tray_id}


def test_a_barcode_listed_twice_is_added_once(client, test_database, session, uploader, warehouse):  # noqa: F811
    item = warehouse.tray_item()
    job_id = _job(session)

    response = _upload(client, job_id, _file([[item.barcode.value, ""], [item.barcode.value, ""]]))

    assert response.status_code == 200, response.text
    assert "line 2" in _errors(response)[3]
    assert _items_in(session, job_id) == {item.id}


def test_a_tray_barcode_adds_the_tray_and_its_items(client, test_database, session, uploader, warehouse):  # noqa: F811
    first = warehouse.tray_item()
    tray = session.get(Tray, first.tray_id)
    second = _make_item(session, tray)
    second.status = "In"
    session.commit()
    job_id = _job(session)

    response = _upload(client, job_id, _file([["", tray.barcode.value]]))

    assert response.status_code == 200, response.text
    assert _items_in(session, job_id) == {first.id, second.id}
    assert _trays_in(session, job_id) == {tray.id}


def test_a_file_with_only_a_tray_barcode_column(client, test_database, session, uploader, warehouse):  # noqa: F811
    item = warehouse.tray_item()
    tray_barcode = session.get(Tray, item.tray_id).barcode.value
    job_id = _job(session)

    response = _upload(client, job_id, _file([[tray_barcode]], columns=["Tray Barcode"]))

    assert response.status_code == 200, response.text
    assert _items_in(session, job_id) == {item.id}


def test_a_tray_with_a_requested_item_is_refused_whole(client, test_database, session, uploader, warehouse):  # noqa: F811
    first = warehouse.tray_item()
    tray = session.get(Tray, first.tray_id)
    requested = _make_item(session, tray)
    requested.status = "Requested"
    session.commit()
    good = warehouse.tray_item()
    job_id = _job(session)

    response = _upload(client, job_id, _file([["", tray.barcode.value], [good.barcode.value, ""]]))

    assert response.status_code == 200, response.text
    assert "Requested" in _errors(response)[2]
    assert _items_in(session, job_id) == {good.id}
    assert _trays_in(session, job_id) == {good.tray_id}


def test_line_numbers_match_the_file(client, test_database, session, uploader, warehouse):  # noqa: F811
    good = warehouse.tray_item()
    job_id = _job(session)
    # line 1 is the header; line 3 is blank
    rows = [[good.barcode.value, ""], ["", ""], ["NOPE-1", ""], ["", "NOPE-2"]]

    response = _upload(client, job_id, _file(rows))

    assert response.status_code == 200, response.text
    errors = _errors(response)
    assert set(errors) == {4, 5}
    assert "NOPE-1" in errors[4] and "NOPE-2" in errors[5]


def test_nothing_valid_fails_the_upload_with_every_reason(client, test_database, session, uploader, warehouse):  # noqa: F811
    job_id = _job(session)

    response = _upload(client, job_id, _file([["NOPE-1", ""], ["NOPE-2", ""]]))

    assert response.status_code == 400, response.text
    assert set(_errors(response)) == {2, 3}
    session.expire_all()
    assert session.scalars(select(BatchUpload).where(BatchUpload.withdraw_job_id == job_id)).one().status == "Failed"


def test_a_completed_job_takes_no_uploads(client, test_database, session, uploader, warehouse):  # noqa: F811
    item = warehouse.tray_item()
    job_id = _job(session)
    session.get(WithdrawJob, job_id).status = "Completed"
    session.commit()

    response = _upload(client, job_id, _file([[item.barcode.value, ""]]))

    assert response.status_code == 400, response.text
    assert _items_in(session, job_id) == set()
    assert session.get(Item, item.id).status == "In"
