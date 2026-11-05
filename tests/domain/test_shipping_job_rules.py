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
