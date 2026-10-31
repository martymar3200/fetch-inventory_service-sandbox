"""
A move must never change an item's status (T4).

- Move jobs (mode MoveShelf) don't re-link the moved container: it keeps the job
  that first shelved it.
- complete_shelving_job only sets items that are waiting to be shelved
  (Verified / Accessioned) to In and checks them in to the ILS. Items that are
  Out, Requested, etc. keep their status.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.tasks as tasks
from app.models.items import ItemStatus
from app.models.non_tray_items import NonTrayItemStatus
from app.models.shelving_jobs import ShelvingMode
from app.tasks import complete_shelving_job
from tests.domain.test_move_discrepancies import (  # noqa: F401 (fixtures)
    _make_item,
    _make_job,
    _make_non_tray,
    _post,
    _shelf_barcode,
    as_shelver,
    scene,
)

AUDIT_INFO = {"name": "Move Tester", "id": "0"}


@pytest.fixture
def ils_check_ins(monkeypatch):
    """Record ILS check-ins instead of calling the ILS."""
    calls = []
    monkeypatch.setattr(
        tasks,
        "check_in_shelved_item_async",
        lambda barcode_value, **kwargs: calls.append(barcode_value),
    )
    return calls


def _set_status(session, record, status):
    record.status = status
    session.add(record)
    session.commit()


def _shelve_tray(session, scene, job):
    scene["tray"].shelving_job_id = job.id
    session.add(scene["tray"])
    session.commit()


def test_completing_move_job_keeps_item_status(client: TestClient, session: Session, scene, ils_check_ins):
    direct_job = _make_job(session, scene["building"], scene["user"], ShelvingMode.Manual)
    _shelve_tray(session, scene, direct_job)
    out_item = _make_item(session, scene["tray"])
    in_item = _make_item(session, scene["tray"])
    _set_status(session, out_item, ItemStatus.Out)
    _set_status(session, in_item, ItemStatus.In)

    move_job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveShelf)
    resp = _post(client, move_job, {
        "container_barcode_value": scene["tray"].barcode.value,
        "shelf_barcode_value": _shelf_barcode(scene["shelf_b"]),
        "shelf_position_number": 1,
        "trayed": True,
    })
    assert resp.status_code == 200, resp.text

    complete_shelving_job(move_job.id, audit_info=AUDIT_INFO)

    session.expire_all()
    assert scene["tray"].shelf_position_id == scene["positions_b"][0].id
    assert scene["tray"].shelving_job_id == direct_job.id
    assert out_item.status == ItemStatus.Out
    assert in_item.status == ItemStatus.In
    assert ils_check_ins == []


def test_move_keeps_non_tray_item_shelving_job(client: TestClient, session: Session, scene):
    direct_job = _make_job(session, scene["building"], scene["user"], ShelvingMode.Manual)
    nti = _make_non_tray(session, scene["owner"], scene["size_class"], scene["positions_a"][1])
    nti.shelving_job_id = direct_job.id
    session.add(nti)
    session.commit()

    move_job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveShelf)
    resp = _post(client, move_job, {
        "container_barcode_value": nti.barcode.value,
        "shelf_barcode_value": _shelf_barcode(scene["shelf_b"]),
        "shelf_position_number": 2,
        "trayed": False,
    })
    assert resp.status_code == 200, resp.text

    session.expire_all()
    assert nti.shelf_position_id == scene["positions_b"][1].id
    assert nti.shelving_job_id == direct_job.id


def test_direct_job_still_links_and_shelves_verified_items(client: TestClient, session: Session, scene, ils_check_ins):
    item = _make_item(session, scene["tray"])
    _set_status(session, item, ItemStatus.Verified)

    direct_job = _make_job(session, scene["building"], scene["user"], ShelvingMode.Manual)
    resp = _post(client, direct_job, {
        "container_barcode_value": scene["tray"].barcode.value,
        "shelf_barcode_value": _shelf_barcode(scene["shelf_b"]),
        "shelf_position_number": 1,
        "trayed": True,
    })
    assert resp.status_code == 200, resp.text

    complete_shelving_job(direct_job.id, audit_info=AUDIT_INFO)

    session.expire_all()
    assert scene["tray"].shelving_job_id == direct_job.id
    assert item.status == ItemStatus.In
    assert ils_check_ins == [item.barcode.value]


def test_completion_only_shelves_items_waiting_to_be_shelved(session: Session, scene, ils_check_ins):
    direct_job = _make_job(session, scene["building"], scene["user"], ShelvingMode.Manual)
    _shelve_tray(session, scene, direct_job)
    statuses = {
        ItemStatus.Verified: ItemStatus.In,
        ItemStatus.Accessioned: ItemStatus.In,
        ItemStatus.Out: ItemStatus.Out,
        ItemStatus.Requested: ItemStatus.Requested,
        ItemStatus.PickList: ItemStatus.PickList,
        ItemStatus.Retrieved: ItemStatus.Retrieved,
    }
    items = {}
    for before in statuses:
        items[before] = _make_item(session, scene["tray"])
        _set_status(session, items[before], before)

    complete_shelving_job(direct_job.id, audit_info=AUDIT_INFO)

    session.expire_all()
    for before, expected in statuses.items():
        assert items[before].status == expected, before
    assert sorted(ils_check_ins) == sorted(
        [items[ItemStatus.Verified].barcode.value, items[ItemStatus.Accessioned].barcode.value]
    )


def test_completion_keeps_requested_non_tray_item(session: Session, scene, ils_check_ins):
    direct_job = _make_job(session, scene["building"], scene["user"], ShelvingMode.Manual)
    requested = _make_non_tray(session, scene["owner"], scene["size_class"], scene["positions_a"][1])
    verified = _make_non_tray(session, scene["owner"], scene["size_class"], scene["positions_a"][2])
    for nti, status in ((requested, NonTrayItemStatus.Requested), (verified, NonTrayItemStatus.Verified)):
        nti.shelving_job_id = direct_job.id
        _set_status(session, nti, status)

    complete_shelving_job(direct_job.id, audit_info=AUDIT_INFO)

    session.expire_all()
    assert requested.status == NonTrayItemStatus.Requested
    assert verified.status == NonTrayItemStatus.In
    assert ils_check_ins == [verified.barcode.value]
