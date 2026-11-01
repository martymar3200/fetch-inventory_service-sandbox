"""
Staff walk a pick list in the order it is returned, so they don't bounce between aisles.
Items sort by aisle, then ladder, then shelf (sort_priority over the number when set), with
retrieved items after the ones still to pick. Nothing here may change without a decision.
"""
import json
import uuid

import pytest
from sqlalchemy.orm import Session

from app.models.aisles import Aisle
from app.models.ladders import Ladder
from app.models.modules import Module
from app.models.pick_lists import PickList
from app.models.requests import Request
from app.models.side_orientations import SideOrientation
from app.models.sides import Side
from app.schemas.pick_lists import PickListDetailOutput
from tests.domain.test_batch_upload_request import _requests_for, uploader, warehouse  # noqa: F401
from tests.domain.test_move_discrepancies import (
    _make_item,
    _make_non_tray,
    _make_shelf,
    _make_tray,
    _owner,
    _size_classes,
)
from tests.domain.test_pick_list_rules import staff  # noqa: F401
from tests.domain.test_request_list import _location
from tests.domain.test_request_list_cost import _StatementCounter


class Floor:
    """Builds shelved items at chosen locations in one building."""

    def __init__(self, client, session):
        self.client = client
        self.session = session
        self.owner = _owner(session)
        self.size_class = _size_classes(session)[0]
        building, _ = _location(session, f"Order {uuid.uuid4().hex[:8]}", aisle_number=90)
        self.module = session.query(Module).filter_by(building_id=building.id).one()
        self.side_orientation = session.query(SideOrientation).filter_by(name="Left").first()
        self.aisles = {}
        self.ladders = {}
        self.shelf_numbers = {}

    def ladder(self, aisle_number, ladder_number, aisle_priority=None, ladder_priority=None):
        if aisle_number not in self.aisles:
            aisle = Aisle(aisle_number=aisle_number, module_id=self.module.id, sort_priority=aisle_priority)
            self.session.add(aisle)
            self.session.commit()
            side = Side(side_orientation_id=self.side_orientation.id, aisle_id=aisle.id)
            self.session.add(side)
            self.session.commit()
            self.aisles[aisle_number] = side
        key = (aisle_number, ladder_number)
        if key not in self.ladders:
            ladder = Ladder(
                ladder_number=ladder_number,
                side_id=self.aisles[aisle_number].id,
                sort_priority=ladder_priority,
            )
            self.session.add(ladder)
            self.session.commit()
            self.ladders[key] = ladder
        return self.ladders[key]

    def position(self, aisle, ladder, shelf, shelf_priority=None, **priorities):
        ladder_record = self.ladder(aisle, ladder, **priorities)
        shelf_record, positions = _make_shelf(
            self.session, ladder_record, self.owner, self.size_class, shelf_number=shelf
        )
        if shelf_priority is not None:
            shelf_record.sort_priority = shelf_priority
            self.session.commit()
        return positions[0]

    def tray_item(self, aisle, ladder, shelf, **priorities):
        tray = _make_tray(self.session, self.owner, self.size_class, self.position(aisle, ladder, shelf, **priorities))
        tray.scanned_for_shelving = True
        item = _make_item(self.session, tray)
        item.status = "In"
        self.session.commit()
        return item

    def non_tray_item(self, aisle, ladder, shelf, **priorities):
        item = _make_non_tray(
            self.session, self.owner, self.size_class, self.position(aisle, ladder, shelf, **priorities)
        )
        item.scanned_for_shelving = True
        item.status = "In"
        self.session.commit()
        return item

    def request(self, item):
        response = self.client.post("/requests/", json={
            "barcode_value": item.barcode.value,
            "external_request_id": uuid.uuid4().hex[:10],
        })
        assert response.status_code == 201, response.text
        return _requests_for(self.session, item)[-1].id

    def pick_list(self, items):
        response = self.client.post("/pick-lists/", json={"request_ids": [self.request(item) for item in items]})
        assert response.status_code == 201, response.text
        return response.json()["id"]


@pytest.fixture
def floor(staff, session):  # noqa: F811
    return Floor(staff, session)


def _barcodes(rows):
    return [(row["item"] or row["non_tray_item"])["barcode"]["value"] for row in rows]


def _values(items):
    return [item.barcode.value for item in items]


def _walking_order(floor):
    """Items in the order staff should reach them, tray and non-tray mixed."""
    return [
        floor.tray_item(1, 1, 1),
        floor.non_tray_item(1, 1, 2),
        floor.tray_item(1, 2, 1),
        floor.non_tray_item(2, 1, 1),
        floor.tray_item(2, 1, 3),
        floor.tray_item(2, 3, 1),
        floor.non_tray_item(10, 1, 1),
        floor.tray_item(11, 1, 1),
    ]


def test_items_come_back_in_walking_order_whatever_order_they_were_added(floor, staff):  # noqa: F811
    in_order = _walking_order(floor)
    # numeric, not text: aisle 10 is after aisle 2
    added = [in_order[i] for i in (5, 0, 7, 2, 6, 1, 4, 3)]

    pick_list_id = floor.pick_list(added)

    assert _barcodes(staff.get(f"/pick-lists/{pick_list_id}").json()["requests"]) == _values(in_order)


def test_sort_priority_overrides_the_number(floor, staff):  # noqa: F811
    aisle_by_number = floor.tray_item(50, 1, 1)
    aisle_priority_first = floor.tray_item(60, 1, 1, aisle_priority=5)
    aisle_priority_second = floor.tray_item(70, 1, 1, aisle_priority=6)
    ladder_later = floor.tray_item(80, 1, 1, ladder_priority=9)
    ladder_sooner = floor.tray_item(80, 2, 1, ladder_priority=8)
    shelf_later = floor.tray_item(81, 1, 1, shelf_priority=8)
    shelf_sooner = floor.tray_item(81, 1, 2, shelf_priority=7)

    pick_list_id = floor.pick_list([
        aisle_by_number, aisle_priority_first, aisle_priority_second,
        ladder_later, ladder_sooner, shelf_later, shelf_sooner,
    ])

    assert _barcodes(staff.get(f"/pick-lists/{pick_list_id}").json()["requests"]) == _values([
        aisle_priority_first, aisle_priority_second, aisle_by_number,
        ladder_sooner, ladder_later, shelf_sooner, shelf_later,
    ])


def test_scanning_keeps_the_rest_in_walking_order(floor, staff, session):  # noqa: F811
    in_order = _walking_order(floor)
    pick_list_id = floor.pick_list(in_order)
    assert staff.patch(f"/pick-lists/{pick_list_id}", json={"status": "Running"}).status_code == 200
    rows = staff.get(f"/pick-lists/{pick_list_id}").json()["requests"]

    # out of order, the way a colleague's scans or a missed item can land
    for index in (3, 0, 6):
        response = staff.patch(
            f"/pick-lists/{pick_list_id}/update_request/{rows[index]['id']}", json={"status": "Out"}
        )
        assert response.status_code == 200, response.text

    after = staff.get(f"/pick-lists/{pick_list_id}").json()["requests"]
    assert _barcodes(after) == _values([in_order[i] for i in (1, 2, 4, 5, 7, 0, 3, 6)])
    assert [row["fulfilled"] for row in after] == [False] * 5 + [True] * 3


def test_item_with_no_shelf_location_goes_last(floor, staff, session):  # noqa: F811
    shelved = floor.tray_item(1, 1, 1)
    pick_list_id = floor.pick_list([shelved])
    unshelved = _make_non_tray(session, floor.owner, floor.size_class)
    session.add(Request(non_tray_item_id=unshelved.id, pick_list_id=pick_list_id))
    session.commit()

    rows = staff.get(f"/pick-lists/{pick_list_id}").json()["requests"]

    assert _barcodes(rows) == _values([shelved, unshelved])


def _build_job(floor, count):
    return floor.pick_list([
        floor.non_tray_item(index + 1, 1, 1) if index % 3 == 0 else floor.tray_item(index + 1, 1, 1)
        for index in range(count)
    ])


def _by_id(rows):
    return {row["id"]: row for row in rows}


def test_response_holds_the_same_data_as_loading_records_one_at_a_time(floor, staff, session):  # noqa: F811
    pick_list_id = _build_job(floor, 6)

    response = staff.get(f"/pick-lists/{pick_list_id}").json()

    # A separate session with nothing loaded falls back on each model's default loading
    with Session(session.get_bind()) as fresh:
        pick_list = fresh.get(PickList, pick_list_id)
        expected = json.loads(PickListDetailOutput.model_validate(pick_list, from_attributes=True).model_dump_json())

    assert _by_id(response.pop("requests")) == _by_id(expected.pop("requests"))
    assert response == expected


@pytest.mark.parametrize("call", ["open", "start"])
def test_cost_does_not_grow_with_the_size_of_the_job(staff, session, call):  # noqa: F811
    def measure(pick_list_id):
        session.expire_all()
        with _StatementCounter(session) as counter:
            if call == "open":
                response = staff.get(f"/pick-lists/{pick_list_id}")
            else:
                response = staff.patch(f"/pick-lists/{pick_list_id}", json={"status": "Running"})
        assert response.status_code == 200, response.text
        return counter.count

    small = measure(_build_job(Floor(staff, session), 3))
    large = measure(_build_job(Floor(staff, session), 30))

    assert large == small
    assert small < 60


def _start_and_rows(staff, pick_list_id):  # noqa: F811
    assert staff.patch(f"/pick-lists/{pick_list_id}", json={"status": "Running"}).status_code == 200
    return staff.get(f"/pick-lists/{pick_list_id}").json()["requests"]


def _scan(staff, pick_list_id, request_id):  # noqa: F811
    response = staff.patch(f"/pick-lists/{pick_list_id}/update_request/{request_id}", json={"status": "Out"})
    assert response.status_code == 200, response.text
    return response.json()


def test_scan_response_reports_every_request_including_a_colleagues_scan(floor, staff):  # noqa: F811
    pick_list_id = _build_job(floor, 3)
    rows = _start_and_rows(staff, pick_list_id)

    _scan(staff, pick_list_id, rows[0]["id"])  # a colleague's scan
    result = _scan(staff, pick_list_id, rows[1]["id"])

    assert result["id"] == pick_list_id
    assert result["status"] == "Running"
    assert {row["id"]: (row["fulfilled"], row["item_status"]) for row in result["requests"]} == {
        rows[0]["id"]: (True, "Out"),
        rows[1]["id"]: (True, "Out"),
        rows[2]["id"]: (False, "PickList"),
    }


def test_scan_response_shows_a_colleague_pausing_the_job(floor, staff):  # noqa: F811
    pick_list_id = _build_job(floor, 2)
    rows = _start_and_rows(staff, pick_list_id)
    assert staff.patch(f"/pick-lists/{pick_list_id}", json={"status": "Paused"}).status_code == 200

    assert _scan(staff, pick_list_id, rows[0]["id"])["status"] == "Paused"


def test_repeated_scan_returns_the_same_compact_result(floor, staff):  # noqa: F811
    pick_list_id = _build_job(floor, 2)
    rows = _start_and_rows(staff, pick_list_id)
    first = _scan(staff, pick_list_id, rows[0]["id"])

    second = _scan(staff, pick_list_id, rows[0]["id"])

    assert _by_id(second["requests"]) == _by_id(first["requests"])


def test_scan_response_stays_small_and_cheap_on_a_large_job(staff, session):  # noqa: F811
    def measure(count):
        pick_list_id = _build_job(Floor(staff, session), count)
        rows = _start_and_rows(staff, pick_list_id)
        session.expire_all()
        with _StatementCounter(session) as counter:
            response = staff.patch(
                f"/pick-lists/{pick_list_id}/update_request/{rows[0]['id']}", json={"status": "Out"}
            )
        assert response.status_code == 200, response.text
        return counter.count, len(response.content)

    small_statements, _ = measure(3)
    large_statements, large_size = measure(30)

    assert large_statements == small_statements
    assert large_size < 30 * 100
