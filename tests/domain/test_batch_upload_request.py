"""
POST /batch-upload/request: Import Requests from File.

The file is the only input staff give: every other value (building, status) has to
come from the item, and a file has to follow the same rules as a manual request.
"""
import io
import uuid

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.dependencies import get_current_user_with_permissions
from app.main import app as fastapi_app
from app.models.audit_trails import AuditTrail
from app.models.batch_upload import BatchUpload
from app.models.delivery_locations import DeliveryLocation
from app.models.items import Item
from app.models.owner_delivery_locations import OwnerDeliveryLocation
from app.models.owners import Owner
from app.models.non_tray_items import NonTrayItem
from app.models.priorities import Priority
from app.models.request_types import RequestType
from app.models.requests import Request
from app.models.users import User
from tests.domain.test_move_discrepancies import (
    _make_item,
    _make_non_tray,
    _make_shelf,
    _make_tray,
    _owner,
    _size_classes,
)
from tests.domain.test_request_list import _location
from tests.domain.test_request_list_cost import _StatementCounter

URL = "/batch-upload/request"
COLUMNS = [
    "Item Barcode",
    "External Request ID",
    "Requestor Name",
    "Priority",
    "Request Type",
    "Delivery Location",
]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
def uploader(session):
    record = User(first_name="File", last_name="Importer", email=f"{uuid.uuid4().hex[:10]}@t.test")
    session.add(record)
    session.commit()

    class P:
        def __init__(self, name):
            self.name = name

    class G:
        permissions = [
            P("can_access_request"),
            P("can_perform_batch_uploads"),
            P("can_create_and_submit_manual_requests"),
            P("can_delete_request"),
            P("delete_requests"),
        ]

    class MockUser:
        id = record.id
        first_name = "File"
        last_name = "Importer"
        groups = [G()]

    prev = fastapi_app.dependency_overrides.get(get_current_user_with_permissions)
    fastapi_app.dependency_overrides[get_current_user_with_permissions] = lambda: MockUser()
    yield record
    if prev is not None:
        fastapi_app.dependency_overrides[get_current_user_with_permissions] = prev
    else:
        fastapi_app.dependency_overrides.pop(get_current_user_with_permissions, None)


@pytest.fixture
def warehouse(session, uploader):
    """Two buildings, with builders for shelved items in either one."""

    class Warehouse:
        pass

    data = Warehouse()
    data.owner = _owner(session)
    data.size_class = _size_classes(session)[0]
    data.building_a, data.ladder_a = _location(session, f"Alpha {uuid.uuid4().hex[:8]}")
    data.building_b, data.ladder_b = _location(session, f"Beta {uuid.uuid4().hex[:8]}")
    data.next_shelf = 0

    def position(ladder):
        data.next_shelf += 1
        return _make_shelf(session, ladder, data.owner, data.size_class, shelf_number=data.next_shelf)[1][0]

    def tray_item(ladder=None, owner=None):
        owner = owner or data.owner
        tray = _make_tray(session, owner, data.size_class, position(ladder or data.ladder_a))
        tray.scanned_for_shelving = True
        item = _make_item(session, tray)
        item.status = "In"
        session.commit()
        return item

    def non_tray(ladder=None):
        item = _make_non_tray(session, data.owner, data.size_class, position(ladder or data.ladder_a))
        item.scanned_for_shelving = True
        item.status = "In"
        session.commit()
        return item

    data.tray_item, data.non_tray = tray_item, non_tray
    return data


def _csv(rows, columns=COLUMNS):
    frame = pd.DataFrame(rows, columns=columns)
    return frame.to_csv(index=False).encode()


def _row(barcode, external_id=None, name="", priority="", request_type="", delivery=""):
    return [barcode, external_id or uuid.uuid4().hex[:12], name, priority, request_type, delivery]


def _upload(client, uploader, body, name="requests.csv", content_type="text/csv"):
    return client.post(
        URL,
        files={"file": (name, body, content_type)},
        data={"requested_by_id": uploader.id},
    )


def _requests_for(session, item):
    session.expire_all()
    column = Request.item_id if isinstance(item, Item) else Request.non_tray_item_id
    return session.scalars(select(Request).where(column == item.id)).all()


def _latest_batch(session):
    session.expire_all()
    return session.scalars(select(BatchUpload).order_by(BatchUpload.id.desc())).first()


def _lookups(session):
    tag = uuid.uuid4().hex[:8]
    priority = Priority(value=f"Pri {tag}")
    request_type = RequestType(type=f"Type {tag}")
    delivery = DeliveryLocation(name=f"Desk {tag}", address="1 First St")
    session.add_all([priority, request_type, delivery])
    session.commit()
    return priority, request_type, delivery


# --------------------------------------------------------------------------- #
# A valid file
# --------------------------------------------------------------------------- #
def test_valid_file_creates_requests_for_tray_and_non_tray_items(
    client: TestClient, session, uploader, warehouse
):
    tray_item = warehouse.tray_item()
    non_tray = warehouse.non_tray()
    priority, request_type, delivery = _lookups(session)

    response = _upload(client, uploader, _csv([
        _row(tray_item.barcode.value, "EXT-1", "Ann", priority.value, request_type.type, delivery.name),
        _row(non_tray.barcode.value, "EXT-2", "Bob"),
    ]))

    assert response.status_code == 200, response.text
    batch = _latest_batch(session)
    assert batch.status == "Uploaded"
    assert batch.user_id == uploader.id

    first = _requests_for(session, tray_item)[0]
    assert first.batch_upload_id == batch.id
    assert first.external_request_id == "EXT-1"
    assert first.requestor_name == "Ann"
    assert first.priority_id == priority.id
    assert first.request_type_id == request_type.id
    assert first.delivery_location_id == delivery.id
    assert first.requested_by_id == uploader.id
    assert first.status == "New"

    second = _requests_for(session, non_tray)[0]
    assert second.priority_id is None and second.delivery_location_id is None

    session.refresh(tray_item)
    session.refresh(non_tray)
    assert tray_item.status == "Requested"
    assert non_tray.status == "Requested"


def test_xlsx_file_creates_requests(client: TestClient, session, uploader, warehouse):
    item = warehouse.tray_item()
    buffer = io.BytesIO()
    pd.DataFrame([_row(item.barcode.value)], columns=COLUMNS).to_excel(buffer, index=False)

    response = _upload(client, uploader, buffer.getvalue(), name="requests.xlsx", content_type=XLSX)

    assert response.status_code == 200, response.text
    assert len(_requests_for(session, item)) == 1


# --------------------------------------------------------------------------- #
# Building comes from each item
# --------------------------------------------------------------------------- #
def test_each_request_gets_the_building_of_its_own_item(
    client: TestClient, session, uploader, warehouse
):
    in_a = warehouse.tray_item(warehouse.ladder_a)
    in_b = warehouse.tray_item(warehouse.ladder_b)
    non_tray_in_b = warehouse.non_tray(warehouse.ladder_b)

    response = _upload(client, uploader, _csv([
        _row(in_a.barcode.value),
        _row(in_b.barcode.value),
        _row(non_tray_in_b.barcode.value),
    ]))

    assert response.status_code == 200, response.text
    assert _requests_for(session, in_a)[0].building_id == warehouse.building_a.id
    assert _requests_for(session, in_b)[0].building_id == warehouse.building_b.id
    assert _requests_for(session, non_tray_in_b)[0].building_id == warehouse.building_b.id


def test_statements_do_not_grow_with_rows(client: TestClient, session, uploader, warehouse):
    def in_their_own_buildings(count):
        ladders = [_location(session, f"Own {uuid.uuid4().hex[:8]}")[1] for _ in range(count)]
        return _csv([_row(warehouse.tray_item(ladder).barcode.value) for ladder in ladders])

    few_rows, many_rows = in_their_own_buildings(2), in_their_own_buildings(12)

    session.expire_all()
    with _StatementCounter(session) as few:
        assert _upload(client, uploader, few_rows).status_code == 200
    session.expire_all()
    with _StatementCounter(session) as many:
        assert _upload(client, uploader, many_rows).status_code == 200

    assert many.count <= few.count + 2, (few.count, many.count)


# --------------------------------------------------------------------------- #
# Bad rows reject the whole file
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad_row,message",
    [
        (lambda good: _row("NOT-A-BARCODE"), "not found"),
        (lambda good: _row(good, priority="No such priority"), "Priority not found"),
        (lambda good: _row(good, request_type="No such type"), "Request Type not found"),
        (lambda good: _row(good, delivery="No such location"), "Delivery Location not found"),
    ],
)
def test_bad_row_rejects_the_file_and_reports_its_line(
    client: TestClient, session, uploader, warehouse, bad_row, message
):
    good = warehouse.tray_item()
    other = warehouse.tray_item()

    response = _upload(client, uploader, _csv([_row(good.barcode.value), bad_row(other.barcode.value)]))

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/csv")
    assert message in response.text
    assert "\n3," in response.text  # the header is line 1, so the second row is line 3
    assert _latest_batch(session).status == "Failed"
    assert _requests_for(session, good) == []
    session.refresh(good)
    assert good.status == "In"


def test_missing_external_request_id_is_reported(client: TestClient, session, uploader, warehouse):
    item = warehouse.tray_item()
    frame_row = [item.barcode.value, "", "", "", "", ""]

    response = _upload(client, uploader, _csv([frame_row]))

    assert response.status_code == 400
    assert "External Request ID is required" in response.text


def test_duplicate_barcode_is_reported(client: TestClient, session, uploader, warehouse):
    item = warehouse.tray_item()

    response = _upload(client, uploader, _csv([_row(item.barcode.value), _row(item.barcode.value)]))

    assert response.status_code == 400
    assert "Duplicate Item Barcode" in response.text


def test_item_that_is_not_shelved_is_reported(client: TestClient, session, uploader, warehouse):
    tray = _make_tray(session, warehouse.owner, warehouse.size_class, None)
    item = _make_item(session, tray)
    item.status = "In"
    session.commit()

    response = _upload(client, uploader, _csv([_row(item.barcode.value)]))

    assert response.status_code == 400
    assert "is not shelved" in response.text


def test_item_with_an_active_request_is_reported(client: TestClient, session, uploader, warehouse):
    item = warehouse.tray_item()
    assert _upload(client, uploader, _csv([_row(item.barcode.value)])).status_code == 200

    response = _upload(client, uploader, _csv([_row(item.barcode.value)]))

    assert response.status_code == 400
    assert "already requested" in response.text


# --------------------------------------------------------------------------- #
# Owner delivery location limits
# --------------------------------------------------------------------------- #
@pytest.fixture
def limited_owner(session):
    tag = uuid.uuid4().hex[:8]
    existing = _owner(session)
    owner = Owner(name=f"Limited {tag}", owner_tier_id=existing.owner_tier_id)
    allowed = DeliveryLocation(name=f"Allowed {tag}", address="1 First St")
    blocked = DeliveryLocation(name=f"Blocked {tag}", address="2 Second St")
    session.add(owner)
    session.commit()
    session.add_all([allowed, blocked])
    session.commit()
    session.add(OwnerDeliveryLocation(owner_id=owner.id, delivery_location_id=allowed.id))
    session.commit()
    return owner, allowed, blocked


def test_blocked_delivery_location_is_reported(client, session, uploader, warehouse, limited_owner):
    owner, _, blocked = limited_owner
    item = warehouse.tray_item(owner=owner)

    response = _upload(client, uploader, _csv([_row(item.barcode.value, delivery=blocked.name)]))

    assert response.status_code == 400
    assert f"Delivery location is not allowed for items owned by '{owner.name}'" in response.text
    assert _requests_for(session, item) == []


def test_manual_request_uses_the_same_delivery_location_rule(
    client, session, uploader, warehouse, limited_owner
):
    owner, _, blocked = limited_owner
    item = warehouse.tray_item(owner=owner)

    response = client.post("/requests/", json={
        "barcode_value": item.barcode.value,
        "external_request_id": "M-1",
        "delivery_location_id": blocked.id,
    })

    assert response.status_code == 400
    assert f"Delivery location is not allowed for items owned by '{owner.name}'" in response.json()["detail"]


def test_allowed_delivery_location_passes(client, session, uploader, warehouse, limited_owner):
    owner, allowed, _ = limited_owner
    item = warehouse.tray_item(owner=owner)

    response = _upload(client, uploader, _csv([_row(item.barcode.value, delivery=allowed.name)]))

    assert response.status_code == 200, response.text


def test_blank_delivery_location_passes_for_a_limited_owner(
    client, session, uploader, warehouse, limited_owner
):
    owner, _, _ = limited_owner
    item = warehouse.tray_item(owner=owner)

    response = _upload(client, uploader, _csv([_row(item.barcode.value)]))

    assert response.status_code == 200, response.text


def test_owner_without_limits_can_use_any_location(client, session, uploader, warehouse, limited_owner):
    _, _, any_location = limited_owner
    item = warehouse.tray_item()

    response = _upload(client, uploader, _csv([_row(item.barcode.value, delivery=any_location.name)]))

    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# Deleted requests
# --------------------------------------------------------------------------- #
def test_item_can_be_imported_again_after_its_request_is_deleted(
    client: TestClient, session, uploader, warehouse
):
    item = warehouse.tray_item()
    assert _upload(client, uploader, _csv([_row(item.barcode.value)])).status_code == 200
    request_id = _requests_for(session, item)[0].id
    assert client.delete(f"/requests/{request_id}").status_code == 204

    response = _upload(client, uploader, _csv([_row(item.barcode.value)]))

    assert response.status_code == 200, response.text
    session.expire_all()
    session.refresh(item)
    assert item.status == "Requested"


def test_deleting_an_old_batch_leaves_a_newer_requests_item_alone(
    client: TestClient, session, uploader, warehouse
):
    item = warehouse.tray_item()
    assert _upload(client, uploader, _csv([_row(item.barcode.value)])).status_code == 200
    old_batch = _latest_batch(session)
    request_id = _requests_for(session, item)[0].id
    assert client.delete(f"/requests/{request_id}").status_code == 204
    assert client.post("/requests/", json={
        "barcode_value": item.barcode.value,
        "external_request_id": "NEWER",
    }).status_code == 201

    assert client.delete(f"/batch-upload/{old_batch.id}").status_code == 204

    session.expire_all()
    session.refresh(item)
    assert item.status == "Requested"


def test_deleting_a_batch_returns_its_active_items_to_in(client: TestClient, session, uploader, warehouse):
    item = warehouse.tray_item()
    non_tray = warehouse.non_tray()
    assert _upload(client, uploader, _csv([
        _row(item.barcode.value),
        _row(non_tray.barcode.value),
    ])).status_code == 200

    assert client.delete(f"/batch-upload/{_latest_batch(session).id}").status_code == 204

    session.expire_all()
    session.refresh(item)
    session.refresh(non_tray)
    assert item.status == "In"
    assert non_tray.status == "In"


# --------------------------------------------------------------------------- #
# Bad files get a clear message and leave no batch behind
# --------------------------------------------------------------------------- #
HEADER = ",".join(COLUMNS) + "\n"


@pytest.mark.parametrize(
    "body,name,content_type,message",
    [
        (b"hello", "notes.txt", "text/plain", "Unsupported file type"),
        (b"", "empty.csv", "text/csv", "The file is empty"),
        (b"not really a spreadsheet", "broken.xlsx", XLSX, "could not be read"),
        (b"External Request ID\nE1\n", "no_barcode.csv", "text/csv", "Item Barcode"),
        (b"Item Barcode\nB1\n", "no_external_id.csv", "text/csv", "External Request ID"),
        (HEADER.encode(), "headers_only.csv", "text/csv", "no requests to import"),
        ((HEADER + ",,,,,\n").encode(), "blank_template.csv", "text/csv", "no requests to import"),
    ],
)
def test_bad_file_gets_a_clear_message_and_no_batch(
    client: TestClient, session, uploader, body, name, content_type, message
):
    before = session.query(BatchUpload).count()

    response = _upload(client, uploader, body, name=name, content_type=content_type)

    assert response.status_code == 400
    assert message in response.json()["detail"]
    session.expire_all()
    assert session.query(BatchUpload).count() == before


def test_file_over_the_row_limit_is_rejected(client: TestClient, session, uploader, monkeypatch):
    monkeypatch.setattr("app.routers.batch_upload.MAX_REQUEST_FILE_ROWS", 2)

    response = _upload(client, uploader, _csv([_row(f"B{number}") for number in range(3)]))

    assert response.status_code == 400
    assert "2" in response.json()["detail"] and "3" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# What staff's files really look like
# --------------------------------------------------------------------------- #
def test_excel_plain_csv_keeps_accented_names(client: TestClient, session, uploader, warehouse):
    item = warehouse.tray_item()
    body = (HEADER + f"{item.barcode.value},E1,José,,,\n").encode("cp1252")

    response = _upload(client, uploader, body)

    assert response.status_code == 200, response.text
    assert _requests_for(session, item)[0].requestor_name == "José"


def test_excel_utf8_csv_is_accepted(client: TestClient, session, uploader, warehouse):
    item = warehouse.tray_item()
    body = ("﻿" + HEADER + f"{item.barcode.value},E1,José,,,\n").encode("utf-8")

    response = _upload(client, uploader, body)

    assert response.status_code == 200, response.text
    assert _requests_for(session, item)[0].requestor_name == "José"


def test_missing_optional_columns_count_as_blank(client: TestClient, session, uploader, warehouse):
    item = warehouse.tray_item()
    body = f"Item Barcode,External Request ID\n{item.barcode.value},E1\n".encode()

    response = _upload(client, uploader, body)

    assert response.status_code == 200, response.text
    assert _requests_for(session, item)[0].priority_id is None


def test_spaces_around_values_and_headers_are_trimmed(client: TestClient, session, uploader, warehouse):
    item = warehouse.tray_item()
    body = f" Item Barcode , External Request ID \n {item.barcode.value} ,  E1  \n".encode()

    response = _upload(client, uploader, body)

    assert response.status_code == 200, response.text
    assert _requests_for(session, item)[0].external_request_id == "E1"


def test_blank_requestor_name_is_saved_as_null(client: TestClient, session, uploader, warehouse):
    item = warehouse.tray_item()

    assert _upload(client, uploader, _csv([_row(item.barcode.value)])).status_code == 200

    assert _requests_for(session, item)[0].requestor_name is None


def test_a_name_like_na_stays_a_name(client: TestClient, session, uploader, warehouse):
    item = warehouse.tray_item()
    body = (HEADER + f"{item.barcode.value},E1,NA,,,\n").encode()

    assert _upload(client, uploader, body).status_code == 200

    assert _requests_for(session, item)[0].requestor_name == "NA"


def test_error_report_line_numbers_count_blank_lines(client: TestClient, session, uploader, warehouse):
    item = warehouse.tray_item()
    body = (HEADER + f"\n{item.barcode.value},E1,,,,\n\nNOT-A-BARCODE,E2,,,,\n").encode()

    response = _upload(client, uploader, body)

    assert response.status_code == 400
    assert "\n5,NOT-A-BARCODE," in response.text.replace("\r\n", "\n")


@pytest.mark.parametrize(
    "row_fields,message",
    [
        ({"name": "N" * 51}, "Requestor Name is longer than 50 characters"),
        ({"external_id": "E" * 256}, "External Request ID is longer than 255 characters"),
    ],
)
def test_values_too_long_are_row_errors(client, session, uploader, warehouse, row_fields, message):
    item = warehouse.tray_item()

    response = _upload(client, uploader, _csv([_row(item.barcode.value, **row_fields)]))

    assert response.status_code == 400
    assert message in response.text


# --------------------------------------------------------------------------- #
# Unexpected failures
# --------------------------------------------------------------------------- #
def test_unexpected_error_fails_the_batch_without_exposing_details(
    client: TestClient, session, uploader, warehouse, monkeypatch
):
    def fail(*_args):
        raise RuntimeError("INSERT INTO requests secret-value")

    monkeypatch.setattr("app.routers.batch_upload.process_request_data", fail)
    item = warehouse.tray_item()

    response = _upload(client, uploader, _csv([_row(item.barcode.value)]))

    assert response.status_code == 500
    assert "secret-value" not in response.text
    assert "INSERT" not in response.text
    assert response.json()["detail"] == "The file could not be imported. Please try again or contact support."
    assert _latest_batch(session).status == "Failed"
    assert _requests_for(session, item) == []


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
def _created_events(session, entity_type, entity_id):
    session.expire_all()
    return session.scalars(select(AuditTrail).where(
        AuditTrail.event_type == "request_created",
        AuditTrail.entity_type == entity_type,
        AuditTrail.entity_id == str(entity_id),
    )).all()


def test_import_logs_a_request_created_event_per_item(client: TestClient, session, uploader, warehouse):
    # Tray and non-tray together: the case where pandas turns the ids into floats
    item = warehouse.tray_item()
    non_tray = warehouse.non_tray()

    assert _upload(client, uploader, _csv([
        _row(item.barcode.value),
        _row(non_tray.barcode.value),
    ])).status_code == 200

    batch = _latest_batch(session)
    item_events = _created_events(session, "items", item.id)
    assert [event.description for event in item_events] == [f"Request created from batch upload {batch.id}"]
    assert len(_created_events(session, "non_tray_items", non_tray.id)) == 1


def test_rejected_file_logs_no_events(client: TestClient, session, uploader, warehouse):
    item = warehouse.tray_item()

    assert _upload(client, uploader, _csv([_row(item.barcode.value), _row("NOPE")])).status_code == 400

    assert _created_events(session, "items", item.id) == []
