"""
Completing a withdraw job must only take apart the trays it emptied, and must give their
shelf space back. It used to unshelve every tray in the job (even ones still holding items)
as soon as any tray was emptied, and it took a space away from the shelf instead of
freeing one.
"""
from app.models.barcodes import Barcode
from app.models.items import Item, ItemStatus
from app.models.shelf_positions import ShelfPosition
from app.models.shelves import Shelf
from app.models.trays import Tray
from app.models.withdraw_jobs import WithdrawJob
from tests.domain.test_batch_upload_request import uploader, warehouse  # noqa: F401
from tests.domain.test_move_discrepancies import _make_item
from tests.domain.test_withdraw_job_updates import staff  # noqa: F401


def _new_job(client):
    response = client.post("/withdraw-jobs/", json={})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _add(client, job_id, barcode):
    response = client.post(f"/withdraw-jobs/{job_id}/add_items", json={"barcode_value": barcode})
    assert response.status_code == 200, response.text


def _complete(client, session, job_id):
    # the job's items have been pulled; items left in a shared tray stay In
    session.expire_all()
    for item in session.get(WithdrawJob, job_id).items:
        item.status = ItemStatus.Out
    session.commit()
    response = client.patch(f"/withdraw-jobs/{job_id}", json={"status": "Completed"})
    assert response.status_code == 200, response.text


def _second_item(session, item):
    other = _make_item(session, session.get(Tray, item.tray_id))
    other.status = "In"
    session.commit()
    return other.id


def _shelf_id(session, tray_id):
    return session.get(ShelfPosition, session.get(Tray, tray_id).shelf_position_id).shelf_id


def _space(session, shelf_id):
    session.expire_all()
    return session.get(Shelf, shelf_id).available_space


def test_a_tray_that_still_holds_items_stays_shelved(staff, session, warehouse):  # noqa: F811
    lone = warehouse.tray_item()
    shared = warehouse.tray_item()
    staying_id = _second_item(session, shared)
    shared_tray_id = shared.tray_id
    position_id = session.get(Tray, shared_tray_id).shelf_position_id
    barcode_id = session.get(Tray, shared_tray_id).barcode_id
    job_id = _new_job(staff)
    _add(staff, job_id, lone.barcode.value)
    _add(staff, job_id, shared.barcode.value)

    _complete(staff, session, job_id)

    session.expire_all()
    tray = session.get(Tray, shared_tray_id)
    assert tray.shelf_position_id == position_id
    assert tray.barcode_id == barcode_id
    assert tray.withdrawal_dt is None
    assert session.get(Barcode, barcode_id).withdrawn is False
    staying = session.get(Item, staying_id)
    assert staying.status == ItemStatus.In
    assert staying.tray_id == shared_tray_id


def test_an_emptied_tray_is_withdrawn(staff, session, warehouse):  # noqa: F811
    lone = warehouse.tray_item()
    shared = warehouse.tray_item()
    _second_item(session, shared)
    tray_id = lone.tray_id
    barcode_id = session.get(Tray, tray_id).barcode_id
    job_id = _new_job(staff)
    _add(staff, job_id, lone.barcode.value)
    _add(staff, job_id, shared.barcode.value)

    _complete(staff, session, job_id)

    session.expire_all()
    tray = session.get(Tray, tray_id)
    assert tray.shelf_position_id is None
    assert tray.barcode_id is None
    assert tray.withdrawn_barcode_id == barcode_id
    assert tray.withdrawal_dt is not None
    assert session.get(Barcode, barcode_id).withdrawn is True


def test_withdrawing_a_tray_frees_its_shelf_space(staff, session, warehouse):  # noqa: F811
    item = warehouse.tray_item()
    shelf_id = _shelf_id(session, item.tray_id)
    before = _space(session, shelf_id)
    job_id = _new_job(staff)
    _add(staff, job_id, item.barcode.value)

    _complete(staff, session, job_id)

    assert _space(session, shelf_id) == before + 1


def test_a_tray_left_on_the_shelf_keeps_its_space_count(staff, session, warehouse):  # noqa: F811
    lone = warehouse.tray_item()
    shared = warehouse.tray_item()
    _second_item(session, shared)
    lone_shelf, shared_shelf = _shelf_id(session, lone.tray_id), _shelf_id(session, shared.tray_id)
    lone_before, shared_before = _space(session, lone_shelf), _space(session, shared_shelf)
    job_id = _new_job(staff)
    _add(staff, job_id, lone.barcode.value)
    _add(staff, job_id, shared.barcode.value)

    _complete(staff, session, job_id)

    assert _space(session, lone_shelf) == lone_before + 1
    assert _space(session, shared_shelf) == shared_before
