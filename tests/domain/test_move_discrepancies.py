"""
API-level tests for where POST /shelving-jobs/{id}/reassign-container-location
records a failed move or shelve.

- Move jobs (mode MoveShelf / MoveTrayItem) write a MoveDiscrepancy, which feeds
  the Shelving Move Discrepancy report, and never a ShelvingJobDiscrepancy.
- Other jobs (e.g. Direct) keep writing a ShelvingJobDiscrepancy as before.

A fresh Building/Ladder is created per test so shelves and positions are isolated.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_with_permissions
from app.main import app as fastapi_app
from app.models.aisles import Aisle
from app.models.barcode_types import BarcodeType
from app.models.barcodes import Barcode
from app.models.buildings import Building
from app.models.container_types import ContainerType
from app.models.items import Item
from app.models.ladders import Ladder
from app.models.modules import Module
from app.models.move_discrepancies import MoveDiscrepancy
from app.models.non_tray_items import NonTrayItem
from app.models.owners import Owner
from app.models.shelf_positions import ShelfPosition
from app.models.shelf_types import ShelfType
from app.models.shelves import Shelf
from app.models.shelving_job_discrepancies import ShelvingJobDiscrepancy
from app.models.shelving_jobs import ShelvingJob, ShelvingJobStatus, ShelvingMode, OriginStatus
from app.models.side_orientations import SideOrientation
from app.models.sides import Side
from app.models.size_class import SizeClass
from app.models.trays import Tray
from app.models.users import User


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
@pytest.fixture
def as_shelver(session):
    user = User(first_name="Move", last_name="Tester", email=f"{uuid.uuid4().hex[:10]}@t.test")
    session.add(user)
    session.commit()

    class P:
        def __init__(self, name):
            self.name = name

    class G:
        permissions = [P("can_create_and_execute_shelving_job"), P("can_access_shelving")]

    class MockUser:
        id = user.id
        first_name = "Move"
        last_name = "Tester"
        groups = [G()]

    prev = fastapi_app.dependency_overrides.get(get_current_user_with_permissions)
    fastapi_app.dependency_overrides[get_current_user_with_permissions] = lambda: MockUser()
    yield user
    if prev is not None:
        fastapi_app.dependency_overrides[get_current_user_with_permissions] = prev
    else:
        fastapi_app.dependency_overrides.pop(get_current_user_with_permissions, None)


# --------------------------------------------------------------------------- #
# Scenario builders
# --------------------------------------------------------------------------- #
def _build_location(session):
    building = Building(name=f"MoveBldg-{uuid.uuid4().hex[:8]}")
    session.add(building)
    session.commit()

    module = Module(module_number=f"M{uuid.uuid4().hex[:6]}", building_id=building.id)
    session.add(module)
    session.commit()

    aisle = Aisle(aisle_number=1, module_id=module.id)
    session.add(aisle)
    session.commit()

    orientation = session.query(SideOrientation).filter_by(name="Left").first()
    side = Side(side_orientation_id=orientation.id, aisle_id=aisle.id)
    session.add(side)
    session.commit()

    ladder = Ladder(ladder_number=1, side_id=side.id)
    session.add(ladder)
    session.commit()

    return building, ladder


def _barcode(session, type_name):
    barcode_type = session.query(BarcodeType).filter_by(name=type_name).first()
    barcode = Barcode(value=uuid.uuid4().hex[:20], type_id=barcode_type.id, withdrawn=False)
    session.add(barcode)
    session.commit()
    return barcode


def _container_type_id(session, type_name):
    return session.query(ContainerType).filter_by(type=type_name).first().id


def _size_classes(session):
    return session.query(SizeClass).filter(SizeClass.name != "Unassigned").order_by(SizeClass.id).limit(2).all()


def _owner(session):
    return session.query(Owner).filter(Owner.name != "Unassigned").order_by(Owner.id).first()


def _make_shelf(session, ladder, owner, size_class, *, shelf_number, num_positions=3, available_space=None):
    shelf_type = ShelfType(
        type=f"ST-{uuid.uuid4().hex[:8]}",
        size_class_id=size_class.id,
        max_capacity=num_positions,
    )
    session.add(shelf_type)
    session.commit()

    shelf = Shelf(
        shelf_number=shelf_number,
        ladder_id=ladder.id,
        shelf_type_id=shelf_type.id,
        owner_id=owner.id,
        barcode_id=_barcode(session, "Shelf").id,
        height=size_class.height + 5,
        width=36.00,
        depth=18.00,
        available_space=num_positions if available_space is None else available_space,
    )
    session.add(shelf)
    session.commit()

    positions = [
        ShelfPosition(shelf_id=shelf.id, position_number=i)
        for i in range(1, num_positions + 1)
    ]
    session.add_all(positions)
    session.commit()
    return shelf, positions


def _make_tray(session, owner, size_class, position=None, *, verified=True):
    tray = Tray(
        size_class_id=size_class.id,
        owner_id=owner.id,
        barcode_id=_barcode(session, "Tray").id,
        container_type_id=_container_type_id(session, "Tray"),
        shelf_position_id=position.id if position else None,
        scanned_for_accession=True,
        scanned_for_verification=verified,
    )
    session.add(tray)
    session.commit()
    return tray


def _make_non_tray(session, owner, size_class, position=None):
    nti = NonTrayItem(
        size_class_id=size_class.id,
        owner_id=owner.id,
        barcode_id=_barcode(session, "Item").id,
        container_type_id=_container_type_id(session, "Non-Tray"),
        shelf_position_id=position.id if position else None,
        scanned_for_accession=True,
        scanned_for_verification=True,
    )
    session.add(nti)
    session.commit()
    return nti


def _make_item(session, tray, *, verified=True):
    item = Item(
        tray_id=tray.id,
        owner_id=tray.owner_id,
        size_class_id=tray.size_class_id,
        barcode_id=_barcode(session, "Item").id,
        container_type_id=_container_type_id(session, "Tray"),
        scanned_for_accession=True,
        scanned_for_verification=verified,
    )
    session.add(item)
    session.commit()
    return item


def _make_job(session, building, user, mode):
    origin = OriginStatus.Direct if mode == ShelvingMode.Manual else OriginStatus.Move
    job = ShelvingJob(
        status=ShelvingJobStatus.Running,
        origin=origin,
        mode=mode,
        building_id=building.id,
        assigned_user_id=user.id,
        created_by_id=user.id,
    )
    session.add(job)
    session.commit()
    return job


def _post(client, job, payload):
    return client.post(
        f"/shelving-jobs/{job.id}/reassign-container-location",
        json={"job_id": job.id, "scanned_for_shelving": True, **payload},
    )


def _move_discrepancies(session, **filters):
    session.expire_all()
    query = select(MoveDiscrepancy)
    for column, value in filters.items():
        query = query.where(getattr(MoveDiscrepancy, column) == value)
    return session.execute(query).scalars().all()


def _shelving_discrepancies(session, job):
    session.expire_all()
    return session.execute(
        select(ShelvingJobDiscrepancy).where(ShelvingJobDiscrepancy.shelving_job_id == job.id)
    ).scalars().all()


@pytest.fixture
def scene(session, test_database, as_shelver):
    """Two shelves for one owner/size class; a tray sits on shelf A position 1."""
    owner = _owner(session)
    size_class = _size_classes(session)[0]
    building, ladder = _build_location(session)
    shelf_a, positions_a = _make_shelf(session, ladder, owner, size_class, shelf_number=1)
    shelf_b, positions_b = _make_shelf(session, ladder, owner, size_class, shelf_number=2)
    tray = _make_tray(session, owner, size_class, positions_a[0])
    return {
        "user": as_shelver,
        "owner": owner,
        "size_class": size_class,
        "building": building,
        "ladder": ladder,
        "shelf_a": shelf_a,
        "positions_a": positions_a,
        "shelf_b": shelf_b,
        "positions_b": positions_b,
        "tray": tray,
    }


def _shelf_barcode(shelf):
    return shelf.barcode.value


# --------------------------------------------------------------------------- #
# Shelf moves (MoveShelf)
# --------------------------------------------------------------------------- #
def test_move_to_occupied_position_records_move_discrepancy(client: TestClient, session: Session, scene):
    _make_tray(session, scene["owner"], scene["size_class"], scene["positions_b"][0])
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveShelf)

    resp = _post(client, job, {
        "container_barcode_value": scene["tray"].barcode.value,
        "shelf_barcode_value": _shelf_barcode(scene["shelf_b"]),
        "shelf_position_number": 1,
        "trayed": True,
    })

    assert resp.status_code >= 400
    assert "already occupied" in resp.text
    records = _move_discrepancies(session, tray_id=scene["tray"].id)
    assert len(records) == 1
    record = records[0]
    assert "already occupied" in record.error
    assert record.original_assigned_location == scene["positions_a"][0].location
    assert record.current_assigned_location == scene["positions_b"][0].location
    assert record.assigned_user_id == scene["user"].id
    assert record.container_type_id == scene["tray"].container_type_id
    assert _shelving_discrepancies(session, job) == []


def test_direct_job_still_records_shelving_discrepancy(client: TestClient, session: Session, scene):
    _make_tray(session, scene["owner"], scene["size_class"], scene["positions_b"][0])
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.Manual)

    resp = _post(client, job, {
        "container_barcode_value": scene["tray"].barcode.value,
        "shelf_barcode_value": _shelf_barcode(scene["shelf_b"]),
        "shelf_position_number": 1,
        "trayed": True,
    })

    assert resp.status_code >= 400
    records = _shelving_discrepancies(session, job)
    assert len(records) == 1
    assert records[0].tray_id == scene["tray"].id
    assert records[0].assigned_location == scene["positions_b"][0].location
    assert "already occupied" in records[0].error
    assert _move_discrepancies(session, tray_id=scene["tray"].id) == []


def test_move_not_verified_records_move_discrepancy(client: TestClient, session: Session, scene):
    tray = _make_tray(session, scene["owner"], scene["size_class"], scene["positions_a"][1], verified=False)
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveShelf)

    resp = _post(client, job, {
        "container_barcode_value": tray.barcode.value,
        "shelf_barcode_value": _shelf_barcode(scene["shelf_b"]),
        "shelf_position_number": 2,
        "trayed": True,
    })

    assert resp.status_code >= 400
    records = _move_discrepancies(session, tray_id=tray.id)
    assert len(records) == 1
    assert "has not been verified" in records[0].error
    assert records[0].original_assigned_location == scene["positions_a"][1].location
    assert records[0].current_assigned_location == scene["positions_b"][1].location
    assert _shelving_discrepancies(session, job) == []


def test_move_to_full_shelf_records_move_discrepancy(client: TestClient, session: Session, scene):
    full_shelf, _ = _make_shelf(
        session, scene["ladder"], scene["owner"], scene["size_class"],
        shelf_number=3, available_space=0,
    )
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveShelf)

    resp = _post(client, job, {
        "container_barcode_value": scene["tray"].barcode.value,
        "shelf_barcode_value": _shelf_barcode(full_shelf),
        "shelf_position_number": 1,
        "trayed": True,
    })

    assert resp.status_code >= 400
    records = _move_discrepancies(session, tray_id=scene["tray"].id)
    assert len(records) == 1
    assert "no available space" in records[0].error
    assert _shelving_discrepancies(session, job) == []


def test_move_size_class_mismatch_records_move_discrepancy(client: TestClient, session: Session, scene):
    other_size = _size_classes(session)[1]
    other_shelf, other_positions = _make_shelf(
        session, scene["ladder"], scene["owner"], other_size, shelf_number=3,
    )
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveShelf)

    resp = _post(client, job, {
        "container_barcode_value": scene["tray"].barcode.value,
        "shelf_barcode_value": _shelf_barcode(other_shelf),
        "shelf_position_number": 1,
        "trayed": True,
    })

    assert resp.status_code >= 400
    records = _move_discrepancies(session, tray_id=scene["tray"].id)
    assert len(records) == 1
    assert "Size Discrepancy" in records[0].error
    assert records[0].size_class_id == other_size.id
    assert records[0].current_assigned_location == other_positions[0].location
    assert _shelving_discrepancies(session, job) == []


def test_move_non_tray_to_occupied_position_records_move_discrepancy(client: TestClient, session: Session, scene):
    nti = _make_non_tray(session, scene["owner"], scene["size_class"], scene["positions_a"][2])
    _make_tray(session, scene["owner"], scene["size_class"], scene["positions_b"][0])
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveShelf)

    resp = _post(client, job, {
        "container_barcode_value": nti.barcode.value,
        "shelf_barcode_value": _shelf_barcode(scene["shelf_b"]),
        "shelf_position_number": 1,
        "trayed": False,
    })

    assert resp.status_code >= 400
    records = _move_discrepancies(session, non_tray_item_id=nti.id)
    assert len(records) == 1
    assert records[0].original_assigned_location == scene["positions_a"][2].location
    assert records[0].container_type_id == nti.container_type_id
    assert _shelving_discrepancies(session, job) == []


def test_move_missing_position_records_move_discrepancy(client: TestClient, session: Session, scene):
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveShelf)

    resp = _post(client, job, {
        "container_barcode_value": scene["tray"].barcode.value,
        "shelf_barcode_value": _shelf_barcode(scene["shelf_b"]),
        "shelf_position_number": 99,
        "trayed": True,
    })

    assert resp.status_code >= 400
    assert "does not exist" in resp.text
    records = _move_discrepancies(session, tray_id=scene["tray"].id)
    assert len(records) == 1
    assert "does not exist" in records[0].error
    assert records[0].current_assigned_location == f"{scene['shelf_b'].location}-99"


def test_direct_missing_position_records_nothing(client: TestClient, session: Session, scene):
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.Manual)

    resp = _post(client, job, {
        "container_barcode_value": scene["tray"].barcode.value,
        "shelf_barcode_value": _shelf_barcode(scene["shelf_b"]),
        "shelf_position_number": 99,
        "trayed": True,
    })

    assert resp.status_code >= 400
    assert _shelving_discrepancies(session, job) == []
    assert _move_discrepancies(session, tray_id=scene["tray"].id) == []


def test_move_unknown_shelf_records_move_discrepancy(client: TestClient, session: Session, scene):
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveShelf)

    resp = _post(client, job, {
        "container_barcode_value": scene["tray"].barcode.value,
        "shelf_barcode_value": "no-such-shelf",
        "shelf_position_number": 1,
        "trayed": True,
    })

    assert resp.status_code == 404
    records = _move_discrepancies(session, tray_id=scene["tray"].id)
    assert len(records) == 1
    assert "no-such-shelf" in records[0].error


def test_move_item_to_shelf_records_move_discrepancy(client: TestClient, session: Session, scene):
    item = _make_item(session, scene["tray"])
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveShelf)

    resp = _post(client, job, {
        "container_barcode_value": item.barcode.value,
        "shelf_barcode_value": _shelf_barcode(scene["shelf_b"]),
        "shelf_position_number": 1,
    })

    assert resp.status_code >= 400
    records = _move_discrepancies(session, item_id=item.id)
    assert len(records) == 1
    assert "cannot be assigned directly to a shelf position" in records[0].error
    assert records[0].tray_id == scene["tray"].id
    assert records[0].original_assigned_location == scene["positions_a"][0].location


def test_successful_shelf_move_records_nothing(client: TestClient, session: Session, scene):
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveShelf)

    resp = _post(client, job, {
        "container_barcode_value": scene["tray"].barcode.value,
        "shelf_barcode_value": _shelf_barcode(scene["shelf_b"]),
        "shelf_position_number": 2,
        "trayed": True,
    })

    assert resp.status_code == 200, resp.text
    session.expire_all()
    assert session.get(Tray, scene["tray"].id).shelf_position_id == scene["positions_b"][1].id
    assert _move_discrepancies(session, tray_id=scene["tray"].id) == []
    assert _shelving_discrepancies(session, job) == []


# --------------------------------------------------------------------------- #
# Item -> tray moves (MoveTrayItem)
# --------------------------------------------------------------------------- #
def test_item_to_tray_not_verified_records_move_discrepancy(client: TestClient, session: Session, scene):
    item = _make_item(session, scene["tray"], verified=False)
    dest_tray = _make_tray(session, scene["owner"], scene["size_class"], scene["positions_b"][0])
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveTrayItem)

    resp = _post(client, job, {
        "container_barcode_value": item.barcode.value,
        "destination_tray_barcode_value": dest_tray.barcode.value,
    })

    assert resp.status_code >= 400
    records = _move_discrepancies(session, item_id=item.id)
    assert len(records) == 1
    record = records[0]
    assert "accessioned or verified" in record.error
    assert record.tray_id == scene["tray"].id
    assert record.original_assigned_location == scene["positions_a"][0].location
    assert record.current_assigned_location == scene["positions_b"][0].location
    assert record.owner_id == dest_tray.owner_id


def test_item_to_missing_tray_records_move_discrepancy(client: TestClient, session: Session, scene):
    item = _make_item(session, scene["tray"])
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveTrayItem)

    resp = _post(client, job, {
        "container_barcode_value": item.barcode.value,
        "destination_tray_barcode_value": "no-such-tray",
    })

    assert resp.status_code == 404
    records = _move_discrepancies(session, item_id=item.id)
    assert len(records) == 1
    assert "no-such-tray" in records[0].error
    assert records[0].current_assigned_location is None


def test_tray_into_tray_is_rejected_with_clear_message(client: TestClient, session: Session, scene):
    dest_tray = _make_tray(session, scene["owner"], scene["size_class"], scene["positions_b"][0])
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveTrayItem)

    resp = _post(client, job, {
        "container_barcode_value": scene["tray"].barcode.value,
        "destination_tray_barcode_value": dest_tray.barcode.value,
    })

    assert resp.status_code >= 400
    assert "Only items can be moved into a tray" in resp.text
    records = _move_discrepancies(session, tray_id=scene["tray"].id)
    assert len(records) == 1
    assert "Only items can be moved into a tray" in records[0].error
    assert records[0].current_assigned_location == scene["positions_b"][0].location


def test_successful_item_to_tray_records_nothing(client: TestClient, session: Session, scene):
    item = _make_item(session, scene["tray"])
    dest_tray = _make_tray(session, scene["owner"], scene["size_class"], scene["positions_b"][0])
    job = _make_job(session, scene["building"], scene["user"], ShelvingMode.MoveTrayItem)

    resp = _post(client, job, {
        "container_barcode_value": item.barcode.value,
        "destination_tray_barcode_value": dest_tray.barcode.value,
    })

    assert resp.status_code == 200, resp.text
    session.expire_all()
    assert session.get(Item, item.id).tray_id == dest_tray.id
    assert _move_discrepancies(session, item_id=item.id) == []
