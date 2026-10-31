"""
GET /reporting/move-history/ and /download: the Move History report (T8).

Lists completed moves from Move jobs only, with where each container came from,
where it went and who moved it.
"""
import csv
import io
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_with_permissions
from app.main import app as fastapi_app
from app.models.shelving_job_containers import ShelvingJobContainer, ShelvingJobContainerStatus
from app.models.shelving_jobs import ShelvingMode
from app.models.users import User
from tests.domain.test_move_discrepancies import (
    _build_location,
    _make_item,
    _make_job,
    _make_non_tray,
    _make_shelf,
    _make_tray,
    _owner,
    _post,
    _shelf_barcode,
    _size_classes,
)

URL = "/reporting/move-history/"


@pytest.fixture
def user(session):
    record = User(first_name="Report", last_name="Mover", email=f"{uuid.uuid4().hex[:10]}@t.test")
    session.add(record)
    session.commit()

    class P:
        def __init__(self, name):
            self.name = name

    class G:
        permissions = [
            P("can_access_reports"),
            P("can_create_and_execute_shelving_job"),
            P("can_access_shelving"),
        ]

    class MockUser:
        id = record.id
        first_name = "Report"
        last_name = "Mover"
        groups = [G()]

    prev = fastapi_app.dependency_overrides.get(get_current_user_with_permissions)
    fastapi_app.dependency_overrides[get_current_user_with_permissions] = lambda: MockUser()
    yield record
    if prev is not None:
        fastapi_app.dependency_overrides[get_current_user_with_permissions] = prev
    else:
        fastapi_app.dependency_overrides.pop(get_current_user_with_permissions, None)


@pytest.fixture
def moves(client: TestClient, session: Session, test_database, user):
    """A tray, a non-tray item and an item moved, in a fresh location."""
    owner = _owner(session)
    size_class = _size_classes(session)[0]
    building, ladder = _build_location(session)
    shelf_a, positions_a = _make_shelf(session, ladder, owner, size_class, shelf_number=1)
    shelf_b, positions_b = _make_shelf(session, ladder, owner, size_class, shelf_number=2)
    tray = _make_tray(session, owner, size_class, positions_a[0])
    nti = _make_non_tray(session, owner, size_class, positions_a[1])
    item_source = _make_tray(session, owner, size_class, positions_a[2])
    item = _make_item(session, item_source)
    dest_tray = _make_tray(session, owner, size_class, positions_b[2])

    shelf_job = _make_job(session, building, user, ShelvingMode.MoveShelf)
    item_job = _make_job(session, building, user, ShelvingMode.MoveTrayItem)
    for container, number, trayed in ((tray, 1, True), (nti, 2, False)):
        resp = _post(client, shelf_job, {
            "container_barcode_value": container.barcode.value,
            "shelf_barcode_value": _shelf_barcode(shelf_b),
            "shelf_position_number": number,
            "trayed": trayed,
        })
        assert resp.status_code == 200, resp.text
    resp = _post(client, item_job, {
        "container_barcode_value": item.barcode.value,
        "destination_tray_barcode_value": dest_tray.barcode.value,
    })
    assert resp.status_code == 200, resp.text

    return {
        "user": user,
        "building": building,
        "tray": tray,
        "nti": nti,
        "item": item,
        "item_source": item_source,
        "dest_tray": dest_tray,
        "shelf_job": shelf_job,
        "item_job": item_job,
        "positions_a": positions_a,
        "positions_b": positions_b,
        "owner": owner,
    }


def _report(client, **params):
    resp = client.get(URL, params={"size": 100, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def test_tray_move_row(client: TestClient, moves):
    rows = _report(client, barcode_value=moves["tray"].barcode.value)

    assert len(rows) == 1
    row = rows[0]
    assert row["container_type"] == "Tray"
    assert row["barcode_value"] == moves["tray"].barcode.value
    assert row["original_location"] == moves["positions_a"][0].location
    assert row["destination_location"] == moves["positions_b"][0].location
    assert row["destination_tray_barcode"] is None
    assert row["source_tray_barcode"] is None
    assert row["shelving_job_id"] == moves["shelf_job"].id
    assert row["moved_by"] == "Report Mover"
    assert row["owner"] == moves["owner"].name
    assert row["shelved_dt"] is not None


def test_item_move_row(client: TestClient, moves):
    rows = _report(client, barcode_value=moves["item"].barcode.value)

    assert len(rows) == 1
    row = rows[0]
    assert row["container_type"] == "Item"
    assert row["source_tray_barcode"] == moves["item_source"].barcode.value
    assert row["original_location"] == moves["positions_a"][2].location
    assert row["destination_tray_barcode"] == moves["dest_tray"].barcode.value
    assert row["destination_location"] == moves["positions_b"][2].location


def test_filters(client: TestClient, session: Session, moves):
    job_rows = _report(client, shelving_job_id=moves["shelf_job"].id)
    assert {r["barcode_value"] for r in job_rows} == {moves["tray"].barcode.value, moves["nti"].barcode.value}

    item_job_rows = _report(client, shelving_job_id=moves["item_job"].id, container_type="Item")
    assert [r["barcode_value"] for r in item_job_rows] == [moves["item"].barcode.value]
    assert _report(client, shelving_job_id=moves["item_job"].id, container_type="Tray") == []
    non_tray = _report(client, shelving_job_id=moves["shelf_job"].id, container_type="Non-Tray")
    assert [r["barcode_value"] for r in non_tray] == [moves["nti"].barcode.value]

    by_user = _report(client, moved_by_user_id=moves["user"].id)
    assert len(by_user) == 3
    assert _report(client, moved_by_user_id=999999999) == []

    by_owner = _report(client, owner_id=moves["owner"].id, shelving_job_id=moves["shelf_job"].id)
    assert len(by_owner) == 2
    assert _report(client, owner_id=999999999) == []

    assert _report(client, shelving_job_id=moves["shelf_job"].id, from_dt="2999-01-01T00:00:00Z") == []
    assert _report(client, shelving_job_id=moves["shelf_job"].id, to_dt="2000-01-01T00:00:00Z") == []
    assert len(_report(client, shelving_job_id=moves["shelf_job"].id, from_dt="2000-01-01T00:00:00Z")) == 2


def test_only_completed_moves_from_move_jobs(client: TestClient, session: Session, moves):
    direct_job = _make_job(session, moves["building"], moves["user"], ShelvingMode.Manual)
    tray = _make_tray(session, moves["owner"], _size_classes(session)[0])
    session.add(ShelvingJobContainer(
        shelving_job_id=direct_job.id, tray_id=tray.id, status=ShelvingJobContainerStatus.SHELVED
    ))
    move_job = _make_job(session, moves["building"], moves["user"], ShelvingMode.MoveShelf)
    session.add(ShelvingJobContainer(
        shelving_job_id=move_job.id, tray_id=tray.id, status=ShelvingJobContainerStatus.PENDING
    ))
    session.commit()

    assert _report(client, barcode_value=tray.barcode.value) == []


def test_move_made_before_history_columns_has_blank_from(client: TestClient, session: Session, moves):
    move_job = _make_job(session, moves["building"], moves["user"], ShelvingMode.MoveShelf)
    tray = _make_tray(session, moves["owner"], _size_classes(session)[0])
    session.add(ShelvingJobContainer(
        shelving_job_id=move_job.id,
        tray_id=tray.id,
        actual_shelf_position_id=moves["positions_b"][1].id,
        status=ShelvingJobContainerStatus.SHELVED,
    ))
    session.commit()

    rows = _report(client, barcode_value=tray.barcode.value)

    assert len(rows) == 1
    assert rows[0]["original_location"] is None
    assert rows[0]["moved_by"] is None
    assert rows[0]["destination_location"] == moves["positions_b"][1].location


def test_sorting(client: TestClient, moves):
    asc_rows = _report(client, shelving_job_id=moves["shelf_job"].id, sort_by="shelved_dt", sort_order="asc")
    desc_rows = _report(client, shelving_job_id=moves["shelf_job"].id, sort_by="shelved_dt", sort_order="desc")

    assert [r["id"] for r in asc_rows] == list(reversed([r["id"] for r in desc_rows]))


def test_csv_download(client: TestClient, moves):
    resp = client.get(f"{URL}download", params={"shelving_job_id": moves["item_job"].id})

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == 1
    row = rows[0]
    assert row["barcode"] == moves["item"].barcode.value
    assert row["container_type"] == "Item"
    assert row["from_tray"] == moves["item_source"].barcode.value
    assert row["from_location"] == moves["positions_a"][2].location
    assert row["to_tray"] == moves["dest_tray"].barcode.value
    assert row["to_location"] == moves["positions_b"][2].location
    assert row["job_number"] == str(moves["item_job"].id)
    assert row["moved_by"] == "Report Mover"
    assert row["owner"] == moves["owner"].name


def test_requires_report_permission(client: TestClient, session: Session, test_database):
    class G:
        permissions = []

    class MockUser:
        id = 1
        first_name = "No"
        last_name = "Access"
        groups = [G()]

    prev = fastapi_app.dependency_overrides.get(get_current_user_with_permissions)
    fastapi_app.dependency_overrides[get_current_user_with_permissions] = lambda: MockUser()
    try:
        assert client.get(URL).status_code == 403
        assert client.get(f"{URL}download").status_code == 403
    finally:
        if prev is not None:
            fastapi_app.dependency_overrides[get_current_user_with_permissions] = prev
        else:
            fastapi_app.dependency_overrides.pop(get_current_user_with_permissions, None)
