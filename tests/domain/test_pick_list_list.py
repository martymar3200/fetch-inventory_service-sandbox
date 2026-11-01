"""
The pick list dashboard's filters and sorting, using the query params the screen sends.
"""
import uuid
from datetime import datetime, timezone

import pytest

from app.models.pick_lists import PickList
from app.models.users import User
from tests.domain.test_batch_upload_request import _requests_for, uploader, warehouse  # noqa: F401
from tests.domain.test_pick_list_rules import staff  # noqa: F401


def _job(client, session, warehouse, count=1, ladder=None):
    requests = []
    for _ in range(count):
        item = warehouse.tray_item(ladder)
        response = client.post("/requests/", json={
            "barcode_value": item.barcode.value,
            "external_request_id": uuid.uuid4().hex[:10],
        })
        assert response.status_code == 201, response.text
        requests.append(_requests_for(session, item)[-1].id)
    response = client.post("/pick-lists/", json={"request_ids": requests})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _user(session, first_name, last_name):
    user = User(first_name=first_name, last_name=last_name, email=f"{uuid.uuid4().hex[:10]}@t.test")
    session.add(user)
    session.commit()
    return user


def _ids(client, query, wanted):
    response = client.get(f"/pick-lists/?size=100&{query}")
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()["items"] if row["id"] in wanted]


def test_job_number_filter(staff, session, warehouse):  # noqa: F811
    wanted = _job(staff, session, warehouse)
    _job(staff, session, warehouse)

    body = staff.get(f"/pick-lists/?workflow_id={wanted}").json()

    assert [row["id"] for row in body["items"]] == [wanted]
    assert body["total"] == 1


def test_building_status_and_user_filters_take_several_values(staff, session, warehouse):  # noqa: F811
    in_a = _job(staff, session, warehouse, ladder=warehouse.ladder_a)
    in_b = _job(staff, session, warehouse, ladder=warehouse.ladder_b)
    wanted = {in_a, in_b}
    query = f"building_name={warehouse.building_a.name}&building_name={warehouse.building_b.name}"
    assert set(_ids(staff, query, wanted)) == wanted
    assert _ids(staff, f"building_name={warehouse.building_b.name}", wanted) == [in_b]

    assert staff.patch(f"/pick-lists/{in_a}", json={"status": "Running"}).status_code == 200
    assert _ids(staff, "status=Running", wanted) == [in_a]
    assert set(_ids(staff, "status=Running&status=Created", wanted)) == wanted

    tag = uuid.uuid4().hex[:8]
    first, second = _user(session, "Ann", tag), _user(session, "Bob", tag)
    assert staff.patch(f"/pick-lists/{in_a}", json={"assigned_user_id": first.id}).status_code == 200
    assert staff.patch(f"/pick-lists/{in_b}", json={"assigned_user_id": second.id}).status_code == 200
    assert _ids(staff, f"assigned_user=Ann {tag}", wanted) == [in_a]
    assert set(_ids(staff, f"assigned_user=Ann {tag}&assigned_user=Bob {tag}", wanted)) == wanted


@pytest.mark.parametrize("order,expected", [("asc", [1, 2, 3]), ("desc", [3, 2, 1])])
def test_sort_by_number_of_items(staff, session, warehouse, order, expected):  # noqa: F811
    # oldest job has the most items: sorting by first request date would get this wrong
    jobs = {3: _job(staff, session, warehouse, 3), 1: _job(staff, session, warehouse, 1), 2: _job(staff, session, warehouse, 2)}
    wanted = set(jobs.values())

    assert _ids(staff, f"sort_by=request_count&sort_order={order}", wanted) == [jobs[count] for count in expected]


@pytest.mark.parametrize("order,names", [("asc", ["Aaron", "Zed", None]), ("desc", ["Zed", "Aaron", None])])
def test_sort_by_assigned_user_is_by_name_with_unassigned_last(staff, session, warehouse, order, names):  # noqa: F811
    # Zed has the lower user ID: sorting by ID would get this wrong
    zed, aaron = _user(session, "Zed", "Last"), _user(session, "Aaron", "Last")
    unassigned = _job(staff, session, warehouse)
    with_aaron = _job(staff, session, warehouse)
    with_zed = _job(staff, session, warehouse)
    assert staff.patch(f"/pick-lists/{with_aaron}", json={"assigned_user_id": aaron.id}).status_code == 200
    assert staff.patch(f"/pick-lists/{with_zed}", json={"assigned_user_id": zed.id}).status_code == 200
    by_name = {"Aaron": with_aaron, "Zed": with_zed, None: unassigned}

    ids = _ids(staff, f"sort_by=assigned_user_id&sort_order={order}", set(by_name.values()))

    assert ids == [by_name[name] for name in names]


def test_sort_by_building_keeps_a_job_with_no_building(staff, session, warehouse):  # noqa: F811
    with_building = _job(staff, session, warehouse)
    now = datetime.now(timezone.utc)
    no_building = PickList(create_dt=now, update_dt=now, last_transition=now)
    session.add(no_building)
    session.commit()

    ids = _ids(staff, "sort_by=building_name", {with_building, no_building.id})

    assert ids == [with_building, no_building.id]


def test_paging_with_no_sort_is_stable(staff, session, warehouse):  # noqa: F811
    wanted = [_job(staff, session, warehouse) for _ in range(3)]

    assert _ids(staff, "", set(wanted)) == sorted(wanted)
