"""
Shipping job rules: how a job starts, its run time, and what can change once it is
Completed. Uses the real routes with the shipping module turned on.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.main import app as fastapi_app
from app.auth.dependencies import get_current_user_with_permissions
from app.models.audit_trails import AuditTrail
from app.models.delivery_locations import DeliveryLocation
from app.models.items import Item, ItemStatus
from app.models.requests import Request, RequestStatus
from app.models.shipping_bins import ShippingBin
from app.models.shipping_jobs import ShippingJob, ShippingJobStatus
from app.models.users import User
from tests.domain.test_batch_upload_request import uploader, warehouse  # noqa: F401


@pytest.fixture
def shipper(client, test_database, uploader, monkeypatch):
    monkeypatch.setattr("app.routers.shipping_jobs.get_setting_value", lambda *args, **kwargs: "true")

    class P:
        def __init__(self, name):
            self.name = name

    class G:
        permissions = [
            P("can_access_shipping"),
            P("create_shipping_jobs"),
            P("process_shipping_jobs"),
            P("delete_shipping_jobs"),
        ]

    class MockUser:
        id = uploader.id
        first_name = "File"
        last_name = "Importer"
        groups = [G()]

    # the uploader fixture puts the previous user back afterwards
    fastapi_app.dependency_overrides[get_current_user_with_permissions] = lambda: MockUser()
    return client


def _location(session):
    location = DeliveryLocation(name=f"Desk {uuid.uuid4().hex[:8]}", address="101 Independence Ave")
    session.add(location)
    session.commit()
    return location


def _ready_item(session, warehouse, location=None):
    """A Retrieved tray item with an open request, as a completed pick list leaves it."""
    location = location or _location(session)
    item = session.get(Item, warehouse.tray_item().id)
    item.status = ItemStatus.Retrieved
    session.add(Request(
        item_id=item.id,
        delivery_location_id=location.id,
        status=RequestStatus.Retrieved,
        external_request_id=uuid.uuid4().hex[:12],
    ))
    session.commit()
    return item


def _new_job(client, **body):
    response = client.post("/shipping-jobs/", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _bin_barcode():
    return f"BIN-{uuid.uuid4().hex[:8]}"


def _scan_bin(client, job_id, barcode=None):
    return client.post(f"/shipping-jobs/{job_id}/bins", params={"barcode": barcode or _bin_barcode()})


def _scan_item(client, job_id, bin_id, item):
    return client.post(
        f"/shipping-jobs/{job_id}/bins/{bin_id}/items", params={"item_barcode": item.barcode.value}
    )


def _job(session, job_id):
    session.expire_all()
    return session.get(ShippingJob, job_id)


def _events(session, job_id, event_type):
    session.expire_all()
    return session.scalars(
        select(AuditTrail.description).where(
            AuditTrail.job_type == "shipping_jobs",
            AuditTrail.job_id == str(job_id),
            AuditTrail.event_type == event_type,
        )
    ).all()


# --- Item 2: a scan starts the job; run time adds up ---

def test_bin_scan_starts_the_job_and_assigns_the_scanner(shipper, session, uploader):
    job_id = _new_job(shipper)

    response = _scan_bin(shipper, job_id)

    assert response.status_code == 200, response.text
    job = _job(session, job_id)
    assert job.status == ShippingJobStatus.Running
    assert job.assigned_user_id == uploader.id
    assert _events(session, job_id, "status_changed") == ["Status changed from Created to Running"]
    assert len(_events(session, job_id, "job_assigned")) == 1


def test_rescanning_a_bin_already_in_the_job_starts_it(shipper, session):
    job_id = _new_job(shipper)
    barcode = _bin_barcode()
    session.add(ShippingBin(shipping_job_id=job_id, barcode=barcode))
    session.commit()

    response = _scan_bin(shipper, job_id, barcode)

    assert response.status_code == 200, response.text
    assert _job(session, job_id).status == ShippingJobStatus.Running


def test_item_scan_starts_the_job(shipper, session, warehouse):
    job_id = _new_job(shipper)
    bin_record = ShippingBin(shipping_job_id=job_id, barcode=_bin_barcode())
    session.add(bin_record)
    session.commit()

    response = _scan_item(shipper, job_id, bin_record.id, _ready_item(session, warehouse))

    assert response.status_code == 200, response.text
    assert _job(session, job_id).status == ShippingJobStatus.Running


def test_scan_keeps_the_assigned_user(shipper, session, uploader):
    other = session.scalars(select(User).where(User.id != uploader.id)).first()
    if other is None:
        pytest.skip("needs a second user")
    job_id = _new_job(shipper, assigned_user_id=other.id)

    _scan_bin(shipper, job_id)

    job = _job(session, job_id)
    assert job.status == ShippingJobStatus.Running
    assert job.assigned_user_id == other.id
    assert _events(session, job_id, "job_assigned") == []


def test_rejected_item_scan_does_not_start_the_job(shipper, session, warehouse):
    job_id = _new_job(shipper)
    bin_record = ShippingBin(shipping_job_id=job_id, barcode=_bin_barcode())
    session.add(bin_record)
    session.commit()
    not_retrieved = session.get(Item, warehouse.tray_item().id)

    response = _scan_item(shipper, job_id, bin_record.id, not_retrieved)

    assert response.status_code == 422
    assert _job(session, job_id).status == ShippingJobStatus.Created


def _running_for(session, job_id, minutes):
    job = session.get(ShippingJob, job_id)
    job.status = ShippingJobStatus.Running
    job.last_transition = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    session.commit()


def test_pausing_adds_the_running_time(shipper, session):
    job_id = _new_job(shipper)
    _running_for(session, job_id, 30)

    response = shipper.patch(f"/shipping-jobs/{job_id}", json={"status": "Paused"})

    assert response.status_code == 200, response.text
    run_time = _job(session, job_id).run_time
    assert timedelta(minutes=29) < run_time < timedelta(minutes=31)


def test_completing_adds_the_running_time(shipper, session, warehouse):
    job_id = _new_job(shipper)
    bin_id = _scan_bin(shipper, job_id).json()["id"]
    assert _scan_item(shipper, job_id, bin_id, _ready_item(session, warehouse)).status_code == 200
    _running_for(session, job_id, 20)

    response = shipper.patch(f"/shipping-jobs/{job_id}/complete")

    assert response.status_code == 200, response.text
    job = _job(session, job_id)
    assert timedelta(minutes=19) < job.run_time < timedelta(minutes=21)
    assert job.completed_dt == job.last_transition


# --- Item 3: PATCH can't complete a job or change a completed one ---

def _completed_job(client, session, warehouse):
    job_id = _new_job(client)
    bin_id = _scan_bin(client, job_id).json()["id"]
    item = _ready_item(session, warehouse)
    assert _scan_item(client, job_id, bin_id, item).status_code == 200
    assert client.patch(f"/shipping-jobs/{job_id}/complete").status_code == 200
    return job_id, bin_id, item


def test_patch_can_not_complete_a_job(shipper, session, warehouse):
    job_id = _new_job(shipper)
    bin_id = _scan_bin(shipper, job_id).json()["id"]
    item = _ready_item(session, warehouse)
    assert _scan_item(shipper, job_id, bin_id, item).status_code == 200

    response = shipper.patch(f"/shipping-jobs/{job_id}", json={"status": "Completed"})

    assert response.status_code == 422
    assert _job(session, job_id).status == ShippingJobStatus.Running
    session.expire_all()
    assert session.get(Item, item.id).status == ItemStatus.Retrieved


@pytest.mark.parametrize("body", [{"status": "Running"}, {"status": "Paused"}, {"assigned_user_id": None}])
def test_completed_job_can_not_be_changed(shipper, session, warehouse, body):
    job_id, _, _ = _completed_job(shipper, session, warehouse)
    before = _job(session, job_id)
    assigned_before = before.assigned_user_id

    response = shipper.patch(f"/shipping-jobs/{job_id}", json=body)

    assert response.status_code == 422
    job = _job(session, job_id)
    assert job.status == ShippingJobStatus.Completed
    assert job.assigned_user_id == assigned_before


# --- Item 4: a completed job keeps its bin contents; open bins can't be cleared ---

def _manifest_barcodes(client, job_id, path="manifest"):
    response = client.get(f"/shipping-jobs/{job_id}/{path}" if path else f"/shipping-jobs/{job_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    return [
        (b["barcode"], [i["barcode"]["value"] for i in b["items"]], b["item_count"])
        for b in body["bins"]
    ], body["total_items"]


def _bin_barcode_of(session, bin_id):
    return session.get(ShippingBin, bin_id).barcode


@pytest.mark.parametrize("path", ["manifest", None])
def test_manifest_keeps_items_after_the_bin_is_cleared(shipper, session, warehouse, path):
    job_id, bin_id, item = _completed_job(shipper, session, warehouse)
    expected = ([(_bin_barcode_of(session, bin_id), [item.barcode.value], 1)], 1)

    response = shipper.post("/shipping-jobs/bins/clear", params={"barcode": _bin_barcode_of(session, bin_id)})

    assert response.status_code == 200, response.text
    session.expire_all()
    assert session.get(ShippingBin, bin_id).cleared_dt is not None
    assert session.get(Item, item.id).shipping_bin_id == bin_id
    assert _manifest_barcodes(shipper, job_id, path) == expected


def test_manifest_keeps_items_after_they_are_refiled(shipper, session, warehouse):
    job_id, bin_id, item = _completed_job(shipper, session, warehouse)
    # what a refile scan does to the item (refile_jobs.py)
    record = session.get(Item, item.id)
    record.status = ItemStatus.In
    record.shipping_bin_id = None
    session.commit()

    bins, total = _manifest_barcodes(shipper, job_id)

    assert bins == [(_bin_barcode_of(session, bin_id), [item.barcode.value], 1)]
    assert total == 1


def test_manifest_keeps_items_that_ship_again(shipper, session, warehouse):
    first_job, first_bin, item = _completed_job(shipper, session, warehouse)
    assert shipper.post(
        "/shipping-jobs/bins/clear", params={"barcode": _bin_barcode_of(session, first_bin)}
    ).status_code == 200
    # requested and retrieved again
    _ready_item_again = session.get(Item, item.id)
    _ready_item_again.status = ItemStatus.Retrieved
    session.add(Request(
        item_id=item.id,
        delivery_location_id=_location(session).id,
        status=RequestStatus.Retrieved,
        external_request_id=uuid.uuid4().hex[:12],
    ))
    session.commit()

    second_job = _new_job(shipper)
    second_bin = _scan_bin(shipper, second_job).json()["id"]
    assert _scan_item(shipper, second_job, second_bin, item).status_code == 200

    first_bins, _ = _manifest_barcodes(shipper, first_job)
    second_bins, _ = _manifest_barcodes(shipper, second_job)
    assert first_bins[0][1] == [item.barcode.value]
    assert second_bins[0][1] == [item.barcode.value]


def test_completing_records_tray_and_non_tray_items(shipper, session, warehouse):
    location = _location(session)
    job_id = _new_job(shipper)
    bin_id = _scan_bin(shipper, job_id).json()["id"]
    tray_item = _ready_item(session, warehouse, location)
    non_tray = warehouse.non_tray()
    from app.models.non_tray_items import NonTrayItem, NonTrayItemStatus
    record = session.get(NonTrayItem, non_tray.id)
    record.status = NonTrayItemStatus.Retrieved
    session.add(Request(
        non_tray_item_id=non_tray.id,
        delivery_location_id=location.id,
        status=RequestStatus.Retrieved,
        external_request_id=uuid.uuid4().hex[:12],
    ))
    session.commit()
    assert _scan_item(shipper, job_id, bin_id, tray_item).status_code == 200
    assert _scan_item(shipper, job_id, bin_id, record).status_code == 200

    response = shipper.patch(f"/shipping-jobs/{job_id}/complete")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_items"] == 2
    assert [i["id"] for i in body["bins"][0]["non_tray_items"]] == [non_tray.id]
    from app.models.shipping_bin_contents import ShippingBinContent
    session.expire_all()
    rows = session.scalars(select(ShippingBinContent).where(ShippingBinContent.shipping_bin_id == bin_id)).all()
    assert {(r.item_id, r.non_tray_item_id) for r in rows} == {(tray_item.id, None), (None, non_tray.id)}


def test_bin_with_items_in_an_open_job_can_not_be_cleared(shipper, session, warehouse):
    job_id = _new_job(shipper)
    bin_id = _scan_bin(shipper, job_id).json()["id"]
    item = _ready_item(session, warehouse)
    assert _scan_item(shipper, job_id, bin_id, item).status_code == 200
    barcode = _bin_barcode_of(session, bin_id)

    response = shipper.post("/shipping-jobs/bins/clear", params={"barcode": barcode})

    assert response.status_code == 422
    assert f"open Shipping Job {job_id}" in response.json()["detail"]
    session.expire_all()
    assert session.get(ShippingBin, bin_id).cleared_dt is None
    assert session.get(Item, item.id).shipping_bin_id == bin_id


def test_empty_bin_in_an_open_job_can_be_cleared(shipper, session):
    job_id = _new_job(shipper)
    barcode = _bin_barcode()
    bin_id = _scan_bin(shipper, job_id, barcode).json()["id"]

    response = shipper.post("/shipping-jobs/bins/clear", params={"barcode": barcode})

    assert response.status_code == 200, response.text
    session.expire_all()
    assert session.get(ShippingBin, bin_id) is None
    # the barcode is free for another job
    other_job = _new_job(shipper)
    assert _scan_bin(shipper, other_job, barcode).status_code == 200


# --- Item 5: scans and removals check the job ---

def test_bin_scan_rejected_on_a_completed_job(shipper, session, warehouse):
    job_id, _, _ = _completed_job(shipper, session, warehouse)
    barcode = _bin_barcode()

    response = _scan_bin(shipper, job_id, barcode)

    assert response.status_code == 422
    assert response.json()["detail"] == f"Shipping Job {job_id} is completed"
    session.expire_all()
    assert session.scalars(select(ShippingBin).where(ShippingBin.barcode == barcode)).first() is None


def test_item_check_rejected_on_a_completed_job(shipper, session, warehouse):
    job_id, _, _ = _completed_job(shipper, session, warehouse)
    item = _ready_item(session, warehouse)

    response = shipper.get(f"/shipping-jobs/{job_id}/items/check", params={"barcode": item.barcode.value})

    assert response.status_code == 422


def test_item_scan_rejected_on_a_completed_job(shipper, session, warehouse):
    job_id, bin_id, _ = _completed_job(shipper, session, warehouse)
    # same destination as the bin, so only the job's status can refuse it
    location = session.get(DeliveryLocation, session.get(ShippingBin, bin_id).delivery_location_id)
    item = _ready_item(session, warehouse, location)

    response = _scan_item(shipper, job_id, bin_id, item)

    assert response.status_code == 422
    session.expire_all()
    assert session.get(Item, item.id).shipping_bin_id is None


def test_remove_rejected_on_a_completed_job(shipper, session, warehouse):
    job_id, bin_id, item = _completed_job(shipper, session, warehouse)

    response = shipper.delete(f"/shipping-jobs/{job_id}/bins/{bin_id}/items/{item.id}")

    assert response.status_code == 422
    session.expire_all()
    assert session.get(Item, item.id).shipping_bin_id == bin_id


def test_remove_can_not_reach_another_jobs_bin(shipper, session, warehouse):
    owner_job = _new_job(shipper)
    bin_id = _scan_bin(shipper, owner_job).json()["id"]
    item = _ready_item(session, warehouse)
    assert _scan_item(shipper, owner_job, bin_id, item).status_code == 200
    other_job = _new_job(shipper)

    response = shipper.delete(f"/shipping-jobs/{other_job}/bins/{bin_id}/items/{item.id}")

    assert response.status_code == 404
    session.expire_all()
    assert session.get(Item, item.id).shipping_bin_id == bin_id


def test_remove_from_an_open_job_still_works(shipper, session, warehouse):
    job_id = _new_job(shipper)
    bin_id = _scan_bin(shipper, job_id).json()["id"]
    item = _ready_item(session, warehouse)
    assert _scan_item(shipper, job_id, bin_id, item).status_code == 200

    response = shipper.delete(f"/shipping-jobs/{job_id}/bins/{bin_id}/items/{item.id}")

    assert response.status_code == 200, response.text
    session.expire_all()
    assert session.get(Item, item.id).shipping_bin_id is None


# --- Item 6: the job list sorts and filters by what the dashboard sends ---

def _list_ids(client, **params):
    response = client.get("/shipping-jobs/", params={"size": 100, **params})
    assert response.status_code == 200, response.text
    return [job["id"] for job in response.json()["items"]]


def test_list_sorts_by_the_column_asked_for(shipper, session):
    ids = [_new_job(shipper) for _ in range(3)]

    ascending = [i for i in _list_ids(shipper, sort_by="id", sort_order="asc") if i in ids]
    descending = [i for i in _list_ids(shipper, sort_by="id", sort_order="desc") if i in ids]

    assert ascending == sorted(ids)
    assert descending == sorted(ids, reverse=True)


def test_list_rejects_an_unknown_sort_column(shipper):
    response = shipper.get("/shipping-jobs/", params={"sort_by": "nonsense"})

    assert response.status_code == 400


def test_list_default_order_is_newest_first(shipper, session):
    ids = [_new_job(shipper) for _ in range(3)]
    # same create_dt, so only the tie-breaker orders them
    now = datetime.now(timezone.utc)
    for job_id in ids:
        session.get(ShippingJob, job_id).create_dt = now
    session.commit()

    listed = [i for i in _list_ids(shipper) if i in ids]

    assert listed == sorted(ids, reverse=True)


def test_list_finds_a_job_by_number(shipper):
    ids = [_new_job(shipper) for _ in range(2)]

    assert _list_ids(shipper, workflow_id=ids[1]) == [ids[1]]


# --- Item 11: removing an item returns the bin; an empty job can't complete ---

def test_remove_returns_the_updated_bin(shipper, session, warehouse):
    location = _location(session)
    job_id = _new_job(shipper)
    bin_id = _scan_bin(shipper, job_id).json()["id"]
    first, second = _ready_item(session, warehouse, location), _ready_item(session, warehouse, location)
    assert _scan_item(shipper, job_id, bin_id, first).status_code == 200
    assert _scan_item(shipper, job_id, bin_id, second).status_code == 200

    response = shipper.delete(f"/shipping-jobs/{job_id}/bins/{bin_id}/items/{first.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == bin_id
    assert body["item_count"] == 1
    assert [i["id"] for i in body["items"]] == [second.id]
    assert body["delivery_location_id"] == location.id


def test_emptied_bin_takes_the_next_items_destination(shipper, session, warehouse):
    job_id = _new_job(shipper)
    bin_id = _scan_bin(shipper, job_id).json()["id"]
    first = _ready_item(session, warehouse)
    assert _scan_item(shipper, job_id, bin_id, first).status_code == 200

    response = shipper.delete(f"/shipping-jobs/{job_id}/bins/{bin_id}/items/{first.id}")

    assert response.status_code == 200, response.text
    assert response.json()["item_count"] == 0
    assert response.json()["delivery_location_id"] is None
    # an item for somewhere else now fits
    assert _scan_item(shipper, job_id, bin_id, _ready_item(session, warehouse)).status_code == 200


def test_job_with_no_items_can_not_complete(shipper, session):
    job_id = _new_job(shipper)
    _scan_bin(shipper, job_id)

    response = shipper.patch(f"/shipping-jobs/{job_id}/complete")

    assert response.status_code == 422
    assert response.json()["detail"] == f"Shipping Job {job_id} has no items to ship"
    assert _job(session, job_id).status == ShippingJobStatus.Running


# --- Item 12: completing removes bins that were never filled ---

def test_completing_removes_empty_bins_and_frees_their_barcodes(shipper, session, warehouse):
    job_id = _new_job(shipper)
    used_bin = _scan_bin(shipper, job_id).json()["id"]
    empty_barcode = _bin_barcode()
    empty_bin = _scan_bin(shipper, job_id, empty_barcode).json()["id"]
    assert _scan_item(shipper, job_id, used_bin, _ready_item(session, warehouse)).status_code == 200

    response = shipper.patch(f"/shipping-jobs/{job_id}/complete")

    assert response.status_code == 200, response.text
    assert [b["id"] for b in response.json()["bins"]] == [used_bin]
    session.expire_all()
    assert session.get(ShippingBin, empty_bin) is None
    assert session.get(ShippingBin, used_bin) is not None
    assert _scan_bin(shipper, _new_job(shipper), empty_barcode).status_code == 200


# --- Item 10: loading an item doesn't load its shipping bin ---

def test_loading_an_item_does_not_load_its_shipping_bin(shipper, session, warehouse):
    from sqlalchemy.orm import Session as OrmSession
    from tests.conftest import engine
    job_id = _new_job(shipper)
    bin_id = _scan_bin(shipper, job_id).json()["id"]
    item = _ready_item(session, warehouse)
    assert _scan_item(shipper, job_id, bin_id, item).status_code == 200

    with OrmSession(engine) as fresh:
        loaded = fresh.get(Item, item.id)
        # as "selectin" this loaded the bin, and with it every item in the bin
        assert "shipping_bin" not in loaded.__dict__
        # still there when asked for
        assert loaded.shipping_bin.id == bin_id
