"""
GET /requests/ and GET /batch-upload/: the filters and sorts behind the Requests
dashboard's Request View and Batch View tables, and the cost of serving them.

Every request created here carries a per-run tag in requestor_name, so filtering on
the tag isolates these rows from the rest of the shared test database.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_user_with_permissions
from app.main import app as fastapi_app
from app.models.aisles import Aisle
from app.models.buildings import Building
from app.models.delivery_locations import DeliveryLocation
from app.models.ladders import Ladder
from app.models.media_types import MediaType
from app.models.modules import Module
from app.models.priorities import Priority
from app.models.request_types import RequestType
from app.models.requests import Request, RequestStatus
from app.models.side_orientations import SideOrientation
from app.models.sides import Side
from app.models.users import User
from tests.domain.test_move_discrepancies import (
    _make_item,
    _make_non_tray,
    _make_shelf,
    _make_tray,
    _owner,
    _size_classes,
)

URL = "/requests/"


@pytest.fixture
def reader(session):
    record = User(first_name="Request", last_name="Reader", email=f"{uuid.uuid4().hex[:10]}@t.test")
    session.add(record)
    session.commit()

    class P:
        def __init__(self, name):
            self.name = name

    class G:
        permissions = [P("can_access_request"), P("can_perform_batch_uploads")]

    class MockUser:
        id = record.id
        first_name = "Request"
        last_name = "Reader"
        groups = [G()]

    prev = fastapi_app.dependency_overrides.get(get_current_user_with_permissions)
    fastapi_app.dependency_overrides[get_current_user_with_permissions] = lambda: MockUser()
    yield record
    if prev is not None:
        fastapi_app.dependency_overrides[get_current_user_with_permissions] = prev
    else:
        fastapi_app.dependency_overrides.pop(get_current_user_with_permissions, None)


def _location(session, building_name, *, aisle_number=1, orientation="Left"):
    building = Building(name=building_name)
    session.add(building)
    session.commit()
    module = Module(module_number="1", building_id=building.id)
    session.add(module)
    session.commit()
    return building, _ladder(session, module, aisle_number, orientation)


def _ladder(session, module, aisle_number, orientation="Left"):
    aisle = Aisle(aisle_number=aisle_number, module_id=module.id)
    session.add(aisle)
    session.commit()
    side_orientation = session.query(SideOrientation).filter_by(name=orientation).first()
    side = Side(side_orientation_id=side_orientation.id, aisle_id=aisle.id)
    session.add(side)
    session.commit()
    ladder = Ladder(ladder_number=1, side_id=side.id)
    session.add(ladder)
    session.commit()
    return ladder


@pytest.fixture
def scenario(session, reader):
    """
    Four requests, with the nullable lookups left empty on `bare`:

    alpha  tray item   building A, aisle 2,  type/priority/delivery set
    beta   tray item   building A, aisle 10, type/priority/delivery set
    bare   non-tray    no building, type, priority or delivery location
    done   tray item   building B, Completed and fulfilled
    """
    tag = uuid.uuid4().hex[:10]
    owner = _owner(session)
    size_class = _size_classes(session)[0]

    building_a, ladder_a2 = _location(session, f"Alpha {tag}", aisle_number=2)
    module_a = session.query(Module).filter_by(building_id=building_a.id).one()
    ladder_a10 = _ladder(session, module_a, 10)
    building_b, ladder_b = _location(session, f"Beta {tag}", orientation="Right")

    def position_on(ladder, shelf_number=1):
        return _make_shelf(session, ladder, owner, size_class, shelf_number=shelf_number)[1][0]

    def tray_item(position, media_type=None):
        item = _make_item(session, _make_tray(session, owner, size_class, position))
        item.media_type_id = media_type.id if media_type else None
        session.commit()
        return item

    type_a = RequestType(type=f"AType {tag}")
    type_b = RequestType(type=f"BType {tag}")
    priority_a = Priority(value=f"APri {tag}")
    priority_b = Priority(value=f"BPri {tag}")
    delivery_a = DeliveryLocation(name=f"Reading & Annex {tag}", address="1 First St")
    delivery_b = DeliveryLocation(name=f"Stacks {tag}", address="2 Second St")
    media = MediaType(name=f"Media {tag}")
    session.add_all([type_a, type_b, priority_a, priority_b, delivery_a, delivery_b, media])
    session.commit()

    def add_request(**fields):
        request = Request(**fields)
        session.add(request)
        session.commit()
        return request

    alpha_item = tray_item(position_on(ladder_a2), media)
    alpha = add_request(
        item_id=alpha_item.id,
        building_id=building_a.id,
        request_type_id=type_a.id,
        priority_id=priority_a.id,
        delivery_location_id=delivery_a.id,
        requestor_name=f"Alice {tag}",
        external_request_id=f"Ext-A-{tag}",
    )
    beta_item = tray_item(position_on(ladder_a10))
    beta = add_request(
        item_id=beta_item.id,
        building_id=building_a.id,
        request_type_id=type_b.id,
        priority_id=priority_b.id,
        delivery_location_id=delivery_b.id,
        requestor_name=f"Bob {tag}",
        external_request_id=f"Ext-B-{tag}",
    )
    bare_item = _make_non_tray(session, owner, size_class, position_on(ladder_a2, shelf_number=2))
    bare = add_request(non_tray_item_id=bare_item.id, requestor_name=f"Cara {tag}")
    done_item = tray_item(position_on(ladder_b))
    done = add_request(
        item_id=done_item.id,
        building_id=building_b.id,
        request_type_id=type_a.id,
        requestor_name=f"Dee {tag}",
        status=RequestStatus.Completed,
        fulfilled=True,
    )

    class Scenario:
        pass

    data = Scenario()
    data.tag = tag
    data.alpha, data.beta, data.bare, data.done = alpha, beta, bare, done
    data.building_a, data.building_b = building_a, building_b
    data.alpha_barcode = alpha_item.barcode.value
    data.delivery_a = delivery_a
    return data


def _get(client, tag, **params):
    response = client.get(URL, params={"requestor_name": tag, "size": 100, **params})
    assert response.status_code == 200, response.text
    return response.json()


def _ids(body):
    return [row["id"] for row in body["items"]]


# --------------------------------------------------------------------------- #
# Request View filters
# --------------------------------------------------------------------------- #
def test_filter_by_request_id_returns_only_that_request(client: TestClient, scenario):
    body = _get(client, scenario.tag, id=scenario.alpha.id)
    assert _ids(body) == [scenario.alpha.id]


def test_filter_by_several_buildings_returns_all_of_them(client: TestClient, scenario):
    body = _get(client, scenario.tag, building_id=[scenario.building_a.id, scenario.building_b.id])
    assert set(_ids(body)) == {scenario.alpha.id, scenario.beta.id, scenario.done.id}


def test_filter_by_single_building_still_works(client: TestClient, scenario):
    body = _get(client, scenario.tag, building_id=scenario.building_b.id)
    assert _ids(body) == [scenario.done.id]


def test_requestor_name_search_ignores_case(client: TestClient, scenario):
    assert _ids(_get(client, f"ALICE {scenario.tag}")) == [scenario.alpha.id]


def test_external_request_id_search_ignores_case(client: TestClient, scenario):
    body = _get(client, scenario.tag, external_request_id=f"ext-a-{scenario.tag}")
    assert _ids(body) == [scenario.alpha.id]


def test_text_search_treats_like_wildcards_as_literal_characters(client: TestClient, scenario):
    assert _get(client, f"A_ice {scenario.tag}")["total"] == 0
    assert _get(client, f"%{scenario.tag}")["total"] == 0


def test_filter_by_displayed_location_finds_the_request(client: TestClient, scenario):
    # The location is shown as module-aisle-side-ladder-shelf-position.
    body = _get(client, scenario.tag, item_location="1-2-L-1-1-1")
    assert _ids(body) == [scenario.alpha.id]


def test_filter_by_delivery_location_with_ampersand(client: TestClient, scenario):
    body = _get(client, scenario.tag, delivery_location=scenario.delivery_a.name)
    assert _ids(body) == [scenario.alpha.id]


def test_filter_by_barcode_finds_the_request(client: TestClient, scenario):
    body = _get(client, scenario.tag, barcode_value=scenario.alpha_barcode)
    assert _ids(body) == [scenario.alpha.id]
