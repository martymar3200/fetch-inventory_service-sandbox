"""
GET /requests/ sorting: every column the Request View table can sort by.
"""
import pytest
from fastapi.testclient import TestClient

from tests.domain.test_request_list import _get, _ids, reader, scenario  # noqa: F401


# --------------------------------------------------------------------------- #
SORT_COLUMNS = [
    "id",
    "request_type",
    "barcode_value",
    "external_request_id",
    "building_name",
    "requestor_name",
    "status",
    "priority",
    "media_type",
    "location",
    "delivery_location",
    "create_dt",
]


@pytest.mark.parametrize("sort_order", ["asc", "desc"])
@pytest.mark.parametrize("sort_by", SORT_COLUMNS)
def test_every_column_sort_keeps_every_row(client: TestClient, scenario, sort_by, sort_order):
    body = _get(client, scenario.tag, sort_by=sort_by, sort_order=sort_order)
    assert body["total"] == 4
    assert set(_ids(body)) == {
        scenario.alpha.id, scenario.beta.id, scenario.bare.id, scenario.done.id,
    }


@pytest.mark.parametrize(
    "sort_by", ["request_type", "building_name", "priority", "delivery_location"]
)
@pytest.mark.parametrize("sort_order", ["asc", "desc"])
def test_rows_without_a_lookup_value_sort_last(client: TestClient, scenario, sort_by, sort_order):
    body = _get(client, scenario.tag, sort_by=sort_by, sort_order=sort_order, queue=True)
    assert _ids(body)[-1] == scenario.bare.id


def test_sort_by_request_type_orders_by_name(client: TestClient, scenario):
    ascending = _ids(_get(client, scenario.tag, sort_by="request_type", queue=True))
    descending = _ids(_get(client, scenario.tag, sort_by="request_type", sort_order="desc", queue=True))
    assert ascending == [scenario.alpha.id, scenario.beta.id, scenario.bare.id]
    assert descending == [scenario.beta.id, scenario.alpha.id, scenario.bare.id]


def test_sort_by_location_orders_aisles_numerically(client: TestClient, scenario):
    # Aisle 2 comes before aisle 10; a plain text sort of the location puts 10 first.
    rows = _ids(_get(client, scenario.tag, sort_by="location", building_id=scenario.building_a.id))
    assert rows == [scenario.alpha.id, scenario.beta.id]


def test_rows_with_equal_sort_values_keep_a_stable_order(client: TestClient, scenario):
    # alpha and beta are in the same building, so the id breaks the tie.
    rows = _ids(_get(client, scenario.tag, sort_by="building_name", building_id=scenario.building_a.id))
    assert rows == sorted(rows)


def test_default_order_is_by_id(client: TestClient, scenario, session):
    # An update moves the row physically; it must not move in the list.
    scenario.alpha.requestor_name = f"Alice {scenario.tag}"
    scenario.alpha.external_request_id = f"Ext-A-{scenario.tag}-moved"
    session.commit()
    assert _ids(_get(client, scenario.tag)) == sorted(
        [scenario.alpha.id, scenario.beta.id, scenario.bare.id, scenario.done.id]
    )


@pytest.mark.parametrize(
    "sort_by,filter_param,value",
    [
        ("request_type", "request_type", "type"),
        ("priority", "priority", "priority"),
        ("delivery_location", "delivery_location", "delivery"),
        ("building_name", "building_name", "building"),
    ],
)
def test_sorting_and_filtering_on_the_same_column(client, scenario, sort_by, filter_param, value):
    alpha = scenario.alpha
    values = {
        "type": alpha.request_type.type,
        "priority": alpha.priority.value,
        "delivery": alpha.delivery_location.name,
        "building": alpha.building.name,
    }
    body = _get(client, scenario.tag, sort_by=sort_by, **{filter_param: values[value]})
    assert alpha.id in _ids(body)


def test_sorting_and_filtering_on_location(client: TestClient, scenario):
    body = _get(client, scenario.tag, sort_by="location", item_location="1-2-L-1-1")
    assert _ids(body) == [scenario.alpha.id]
