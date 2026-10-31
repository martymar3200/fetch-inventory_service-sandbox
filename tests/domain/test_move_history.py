"""
Move jobs record every move (T7).

Each successful move in a Move job is its own shelving_job_containers row with
where the container came from (original_shelf_position_id / original_location,
or source_tray_* for item -> tray moves) and who moved it, plus a
CONTAINER_MOVED audit event. Direct to Shelf keeps one row per container and
logs CONTAINER_SHELVED, as before.
"""
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit_trails import AuditTrail
from app.models.shelving_job_containers import ShelvingJobContainer
from app.models.shelving_jobs import ShelvingMode
from app.services.audit_service import AuditEventType
from tests.domain.test_move_discrepancies import (  # noqa: F401 (fixtures)
    _make_item,
    _make_job,
    _make_non_tray,
    _make_tray,
    _post,
    _shelf_barcode,
    as_shelver,
    scene,
)


def _rows(session, job):
    session.expire_all()
    return session.execute(
        select(ShelvingJobContainer)
        .where(ShelvingJobContainer.shelving_job_id == job.id)
        .order_by(ShelvingJobContainer.id)
    ).scalars().all()


def _events(session, job):
    session.expire_all()
    return session.execute(
        select(AuditTrail)
        .where(AuditTrail.job_type == "shelving_jobs")
        .where(AuditTrail.job_id == str(job.id))
        .order_by(AuditTrail.id)
    ).scalars().all()


def _shelf_move(client, job, container, shelf, position_number, trayed=True):
    resp = _post(client, job, {
        "container_barcode_value": container.barcode.value,
        "shelf_barcode_value": _shelf_barcode(shelf),
        "shelf_position_number": position_number,
        "trayed": trayed,
    })
    assert resp.status_code == 200, resp.text
    return resp


def test_tray_move_records_from_and_who(client: TestClient, session: Session, scene):
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveShelf)

    _shelf_move(client, job, scene["tray"], scene["shelf_b"], 1)

    rows = _rows(session, job)
    assert len(rows) == 1
    row = rows[0]
    assert row.tray_id == scene["tray"].id
    assert row.original_shelf_position_id == scene["positions_a"][0].id
    assert row.original_location == scene["positions_a"][0].location
    assert row.actual_shelf_position_id == scene["positions_b"][0].id
    assert row.moved_by_user_id == scene["user"].id
    assert row.status == "Shelved"

    events = _events(session, job)
    assert [e.event_type for e in events] == [AuditEventType.CONTAINER_MOVED]
    assert events[0].entity_type == "trays"
    assert events[0].entity_id == str(scene["tray"].id)
    assert scene["positions_a"][0].location in events[0].description
    assert scene["positions_b"][0].location in events[0].description


def test_moving_same_tray_twice_keeps_both_moves(client: TestClient, session: Session, scene):
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveShelf)

    _shelf_move(client, job, scene["tray"], scene["shelf_b"], 1)
    _shelf_move(client, job, scene["tray"], scene["shelf_b"], 2)

    rows = _rows(session, job)
    assert len(rows) == 2
    first, second = rows
    assert first.original_location == scene["positions_a"][0].location
    assert first.actual_shelf_position_id == scene["positions_b"][0].id
    assert second.original_location == scene["positions_b"][0].location
    assert second.actual_shelf_position_id == scene["positions_b"][1].id
    assert len(_events(session, job)) == 2


def test_non_tray_move_records_from(client: TestClient, session: Session, scene):
    nti = _make_non_tray(session, scene["owner"], scene["size_class"], scene["positions_a"][1])
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveShelf)

    _shelf_move(client, job, nti, scene["shelf_b"], 3, trayed=False)

    rows = _rows(session, job)
    assert len(rows) == 1
    assert rows[0].non_tray_item_id == nti.id
    assert rows[0].original_location == scene["positions_a"][1].location
    events = _events(session, job)
    assert events[0].event_type == AuditEventType.CONTAINER_MOVED
    assert events[0].entity_type == "non_tray_items"


def test_move_of_unshelved_container_has_no_from(client: TestClient, session: Session, scene):
    loose_tray = _make_tray(session, scene["owner"], scene["size_class"])
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveShelf)

    _shelf_move(client, job, loose_tray, scene["shelf_b"], 1)

    row = _rows(session, job)[0]
    assert row.original_shelf_position_id is None
    assert row.original_location is None
    assert "no location" in _events(session, job)[0].description


def test_item_to_tray_move_records_source_tray(client: TestClient, session: Session, scene):
    item = _make_item(session, scene["tray"])
    dest_tray = _make_tray(session, scene["owner"], scene["size_class"], scene["positions_b"][0])
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveTrayItem)

    resp = _post(client, job, {
        "container_barcode_value": item.barcode.value,
        "destination_tray_barcode_value": dest_tray.barcode.value,
    })
    assert resp.status_code == 200, resp.text

    rows = _rows(session, job)
    assert len(rows) == 1
    row = rows[0]
    assert row.item_id == item.id
    assert row.destination_tray_id == dest_tray.id
    assert row.source_tray_id == scene["tray"].id
    assert row.source_tray_barcode == scene["tray"].barcode.value
    assert row.original_shelf_position_id == scene["positions_a"][0].id
    assert row.original_location == scene["positions_a"][0].location
    assert row.moved_by_user_id == scene["user"].id

    events = _events(session, job)
    assert [e.event_type for e in events] == [AuditEventType.CONTAINER_MOVED]
    assert events[0].entity_type == "items"
    assert scene["tray"].barcode.value in events[0].description
    assert dest_tray.barcode.value in events[0].description


def test_direct_shelving_is_unchanged(client: TestClient, session: Session, scene):
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.Manual)

    _shelf_move(client, job, scene["tray"], scene["shelf_b"], 1)
    _shelf_move(client, job, scene["tray"], scene["shelf_b"], 2)

    rows = _rows(session, job)
    assert len(rows) == 1
    assert rows[0].actual_shelf_position_id == scene["positions_b"][1].id
    assert rows[0].original_shelf_position_id is None
    assert rows[0].moved_by_user_id is None
    assert {e.event_type for e in _events(session, job)} == {AuditEventType.CONTAINER_SHELVED}


def test_job_detail_returns_move_history(client: TestClient, session: Session, scene):
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveShelf)
    _shelf_move(client, job, scene["tray"], scene["shelf_b"], 1)

    resp = client.get(f"/shelving-jobs/{job.id}")

    assert resp.status_code == 200, resp.text
    containers = resp.json()["shelving_job_containers"]
    assert len(containers) == 1
    assert containers[0]["original_location"] == scene["positions_a"][0].location
    assert containers[0]["moved_by_user_id"] == scene["user"].id
