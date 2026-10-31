"""
DELETE /shelving-jobs/{id} only deletes jobs that were never started (T5).

- Allowed: Created, Assigned, or Cancelled, with nothing shelved or moved.
- Refused: Running, Paused, Completed, and any job with shelved or moved containers.
- A missing job returns 404.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_with_permissions
from app.main import app as fastapi_app
from app.models.shelving_job_containers import ShelvingJobContainer, ShelvingJobContainerStatus
from app.models.shelving_jobs import ShelvingJob, ShelvingJobStatus, ShelvingMode
from app.models.users import User
from tests.domain.test_move_discrepancies import _build_location, _make_job, _owner, _size_classes, _make_tray


@pytest.fixture
def as_job_manager(session):
    user = User(first_name="Delete", last_name="Tester", email=f"{uuid.uuid4().hex[:10]}@t.test")
    session.add(user)
    session.commit()

    class P:
        def __init__(self, name):
            self.name = name

    class G:
        permissions = [P("delete_shelving_jobs"), P("can_access_shelving")]

    class MockUser:
        id = user.id
        first_name = "Delete"
        last_name = "Tester"
        groups = [G()]

    prev = fastapi_app.dependency_overrides.get(get_current_user_with_permissions)
    fastapi_app.dependency_overrides[get_current_user_with_permissions] = lambda: MockUser()
    yield user
    if prev is not None:
        fastapi_app.dependency_overrides[get_current_user_with_permissions] = prev
    else:
        fastapi_app.dependency_overrides.pop(get_current_user_with_permissions, None)


@pytest.fixture
def make_job(session, test_database, as_job_manager):
    building, _ = _build_location(session)

    def _make(status, mode=ShelvingMode.Manual):
        job = _make_job(session, building, as_job_manager, mode)
        job.status = status
        session.add(job)
        session.commit()
        return job

    return _make


def _job_exists(session, job_id):
    session.expire_all()
    return session.get(ShelvingJob, job_id) is not None


@pytest.mark.parametrize("status", [
    ShelvingJobStatus.Created,
    ShelvingJobStatus.Assigned,
    ShelvingJobStatus.Cancelled,
])
def test_never_started_job_can_be_deleted(client: TestClient, session: Session, make_job, status):
    job = make_job(status)

    resp = client.delete(f"/shelving-jobs/{job.id}")

    assert resp.status_code == 204, resp.text
    assert not _job_exists(session, job.id)


@pytest.mark.parametrize("status", [
    ShelvingJobStatus.Running,
    ShelvingJobStatus.Paused,
    ShelvingJobStatus.Completed,
])
def test_started_job_cannot_be_deleted(client: TestClient, session: Session, make_job, status):
    job = make_job(status)

    resp = client.delete(f"/shelving-jobs/{job.id}")

    assert resp.status_code == 422, resp.text
    assert "can't be deleted" in resp.text
    assert _job_exists(session, job.id)


def test_cancelled_job_with_a_move_cannot_be_deleted(client: TestClient, session: Session, make_job):
    job = make_job(ShelvingJobStatus.Cancelled, mode=ShelvingMode.MoveShelf)
    tray = _make_tray(session, _owner(session), _size_classes(session)[0])
    session.add(ShelvingJobContainer(
        shelving_job_id=job.id, tray_id=tray.id, status=ShelvingJobContainerStatus.SHELVED
    ))
    session.commit()

    resp = client.delete(f"/shelving-jobs/{job.id}")

    assert resp.status_code == 422, resp.text
    assert "shelved or moved" in resp.text
    assert _job_exists(session, job.id)


def test_cancelled_job_with_a_shelved_tray_cannot_be_deleted(client: TestClient, session: Session, make_job):
    job = make_job(ShelvingJobStatus.Cancelled)
    tray = _make_tray(session, _owner(session), _size_classes(session)[0])
    tray.shelving_job_id = job.id
    session.add(tray)
    session.commit()

    resp = client.delete(f"/shelving-jobs/{job.id}")

    assert resp.status_code == 422, resp.text
    session.expire_all()
    assert tray.shelving_job_id == job.id


def test_created_list_job_with_pending_containers_can_be_deleted(client: TestClient, session: Session, make_job):
    job = make_job(ShelvingJobStatus.Created)
    tray = _make_tray(session, _owner(session), _size_classes(session)[0])
    session.add(ShelvingJobContainer(
        shelving_job_id=job.id, tray_id=tray.id, status=ShelvingJobContainerStatus.PENDING
    ))
    session.commit()

    resp = client.delete(f"/shelving-jobs/{job.id}")

    assert resp.status_code == 204, resp.text
    assert not _job_exists(session, job.id)


def test_missing_job_returns_404(client: TestClient, as_job_manager):
    resp = client.delete("/shelving-jobs/999999999")

    assert resp.status_code == 404, resp.text
