"""
GET /batch-upload/: the filters, sorts and cost behind the Requests dashboard's Batch View
and the Batch Request Job screen.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.models.batch_upload import BatchUpload
from app.models.requests import Request
from app.models.users import User
from tests.domain.test_request_list import _ids, reader, scenario  # noqa: F401
from tests.domain.test_request_list_cost import _StatementCounter, _add_requests, _new_user

BATCH_URL = "/batch-upload/"


# --------------------------------------------------------------------------- #
@pytest.fixture
def batches(session, reader, scenario):
    tag = scenario.tag
    uploader_a = User(first_name="Ann", last_name=f"Upload{tag}", email=f"{uuid.uuid4().hex[:10]}@t.test")
    uploader_b = User(first_name="Zed", last_name=f"Upload{tag}", email=f"{uuid.uuid4().hex[:10]}@t.test")
    session.add(uploader_a)
    session.commit()
    session.add(uploader_b)
    session.commit()

    large = BatchUpload(file_name=f"{tag}-large.csv", file_type=f"text/csv-{tag}", user_id=uploader_b.id)
    session.add(large)
    session.commit()
    small = BatchUpload(file_name=f"{tag}-small.xlsx", file_type=f"sheet-{tag}", user_id=uploader_a.id)
    session.add(small)
    session.commit()

    scenario.alpha.batch_upload_id = large.id
    scenario.beta.batch_upload_id = large.id
    scenario.bare.batch_upload_id = small.id
    # A deleted request no longer counts towards its batch.
    scenario.done.batch_upload_id = small.id
    scenario.done.deleted = True
    session.commit()

    class Batches:
        pass

    data = Batches()
    data.tag, data.large, data.small = tag, large, small
    data.uploader_a, data.uploader_b = uploader_a, uploader_b
    return data


def _get_batches(client, **params):
    response = client.get(BATCH_URL, params={"batch_upload_type": "request", "size": 100, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_batch_filter_by_uploader_name_ignores_case(client: TestClient, batches):
    body = _get_batches(client, user_name_search=f"zed upload{batches.tag}")
    assert _ids(body) == [batches.large.id]


def test_batch_filter_by_partial_import_source(client: TestClient, batches):
    body = _get_batches(client, file_type_search=f"SHEET-{batches.tag}")
    assert _ids(body) == [batches.small.id]


def test_batch_exact_file_type_filter_still_works(client: TestClient, batches):
    body = _get_batches(client, file_type=f"sheet-{batches.tag}")
    assert _ids(body) == [batches.small.id]


def test_batch_rows_report_request_count_without_deleted_requests(client: TestClient, batches):
    rows = {row["id"]: row for row in _get_batches(client, user_name_search=batches.tag)["items"]}
    assert rows[batches.large.id]["request_count"] == 2
    assert rows[batches.small.id]["request_count"] == 1


def test_batch_rows_do_not_embed_their_requests(client: TestClient, batches):
    row = _get_batches(client, user_name_search=batches.tag)["items"][0]
    assert "requests" not in row


@pytest.mark.parametrize(
    "sort_order,expected", [("asc", ["small", "large"]), ("desc", ["large", "small"])]
)
def test_batch_sort_by_request_count(client: TestClient, batches, sort_order, expected):
    body = _get_batches(
        client, user_name_search=batches.tag, sort_by="request_count", sort_order=sort_order
    )
    assert _ids(body) == [getattr(batches, name).id for name in expected]


@pytest.mark.parametrize(
    "sort_order,expected", [("asc", ["small", "large"]), ("desc", ["large", "small"])]
)
def test_batch_sort_by_uploader_orders_by_name(client: TestClient, batches, sort_order, expected):
    # small was uploaded by Ann, large by Zed, whichever user id is lower.
    body = _get_batches(
        client, user_name_search=batches.tag, sort_by="user_id", sort_order=sort_order
    )
    assert _ids(body) == [getattr(batches, name).id for name in expected]


@pytest.mark.parametrize("sort_by", ["file_type", "status", "create_dt"])
def test_batch_sort_by_column_keeps_rows(client: TestClient, batches, sort_by):
    body = _get_batches(client, user_name_search=batches.tag, sort_by=sort_by)
    assert body["total"] == 2


@pytest.mark.parametrize("size", [250, 500])
def test_batch_list_accepts_every_page_size_the_table_offers(client: TestClient, batches, size):
    assert _get_batches(client, size=size)["size"] == size


def test_batch_list_statements_do_not_grow_with_rows(client: TestClient, batches, session):
    session.expire_all()
    with _StatementCounter(session) as few:
        _get_batches(client, user_name_search=batches.tag)

    for number in range(10):
        uploader = _new_user(session, f"Upload{batches.tag}")
        session.add(BatchUpload(file_name=f"{batches.tag}-{number}.csv", user_id=uploader.id))
        session.commit()
    session.expire_all()
    with _StatementCounter(session) as many:
        _get_batches(client, user_name_search=batches.tag)

    assert many.count <= few.count + 2, (few.count, many.count)


def test_batch_detail_statements_do_not_grow_with_requests(client: TestClient, batches, session):
    session.expire_all()
    with _StatementCounter(session) as few:
        response = client.get(f"{BATCH_URL}{batches.large.id}")
    assert response.status_code == 200
    assert len(response.json()["requests"]) == 2

    _add_requests(session, batches.tag, 10, batch_upload_id=batches.large.id)

    session.expire_all()
    with _StatementCounter(session) as many:
        response = client.get(f"{BATCH_URL}{batches.large.id}")
    assert len(response.json()["requests"]) == 12

    assert many.count <= few.count + 2, (few.count, many.count)


def test_request_batches_exclude_location_manager_uploads(client: TestClient, batches, session):
    # Location Manager uploads have neither a withdraw job nor an uploading user.
    location_upload = BatchUpload(file_name=f"{batches.tag}-shelves.csv", file_type=f"sheet-{batches.tag}")
    session.add(location_upload)
    session.commit()
    body = _get_batches(client, file_type_search=f"sheet-{batches.tag}")
    assert location_upload.id not in _ids(body)
    assert batches.small.id in _ids(body)
