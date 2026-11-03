"""
PATCH /withdraw-jobs/{id} to Completed withdraws every item in the job, so the API must
refuse until the items have been pulled. Only the screen checked this, and a stale screen
(another user adding an item) could withdraw an item still sitting on the shelf.
"""
from app.models.items import Item, ItemStatus
from app.models.non_tray_items import NonTrayItem
from app.models.withdraw_jobs import WithdrawJob
from tests.domain.test_batch_upload_request import _requests_for, uploader, warehouse  # noqa: F401
from tests.domain.test_withdraw_job_updates import staff, _job, _out_job, _patch, _saved  # noqa: F401


def _complete(client, job_id):
    return client.patch(f"/withdraw-jobs/{job_id}", json={"status": "Completed"})


def _assert_refused(client, session, job_id, fragment):
    response = _complete(client, job_id)
    assert response.status_code == 400, response.text
    assert fragment in response.json()["detail"]
    assert _saved(session, job_id).status != "Completed"


def test_an_item_still_on_the_shelf_blocks_completion(staff, session, warehouse):  # noqa: F811
    pulled, on_shelf = warehouse.tray_item(), warehouse.tray_item()
    session.get(Item, pulled.id).status = ItemStatus.Out
    session.commit()
    job_id = _job(session, pulled, on_shelf)

    _assert_refused(staff, session, job_id, on_shelf.barcode.value)

    session.expire_all()
    assert session.get(Item, on_shelf.id).status == ItemStatus.In
    assert session.get(Item, pulled.id).status == ItemStatus.Out
    assert session.get(Item, on_shelf.id).barcode_id is not None


def test_a_non_tray_item_still_on_the_shelf_blocks_completion(staff, session, warehouse):  # noqa: F811
    non_tray = warehouse.non_tray()
    job_id = _job(session)
    job = session.get(WithdrawJob, job_id)
    job.non_tray_items.append(session.get(NonTrayItem, non_tray.id))
    session.commit()

    _assert_refused(staff, session, job_id, non_tray.barcode.value)
    session.expire_all()
    assert session.get(NonTrayItem, non_tray.id).status == "In"


def test_an_empty_job_can_not_be_completed(staff, session, warehouse):  # noqa: F811
    _assert_refused(staff, session, _job(session), "no items")


def test_an_open_pick_list_blocks_completion(staff, session, warehouse):  # noqa: F811
    item = warehouse.tray_item()
    job_id = _job(session, item)
    pick_list_id = _patch(staff, job_id, create_pick_list=True)["pick_list_id"]
    # even if the item shows Out, the pick list itself must be finished
    session.get(Item, item.id).status = ItemStatus.Out
    session.commit()

    _assert_refused(staff, session, job_id, f"Pick list {pick_list_id}")


def test_items_already_out_without_a_pick_list_can_be_withdrawn(staff, session, warehouse):  # noqa: F811
    item = warehouse.tray_item()
    job_id = _out_job(session, item)

    response = _complete(staff, job_id)

    assert response.status_code == 200, response.text
    session.expire_all()
    assert session.get(Item, item.id).status == ItemStatus.Withdrawn


def test_long_lists_of_unpulled_items_are_shortened(staff, session, warehouse):  # noqa: F811
    items = [warehouse.tray_item() for _ in range(12)]
    job_id = _job(session, *items)

    _assert_refused(staff, session, job_id, "and 2 more")
