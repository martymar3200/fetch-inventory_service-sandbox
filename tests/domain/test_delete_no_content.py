"""
A 204 can't carry a body. These deletes used to raise HTTPException(204, detail=...),
which the app's handler turned into a JSON body: uvicorn then raised RuntimeError and
reset the connection, so the next request on it failed.
"""
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.auth.dependencies import get_current_user_with_permissions
from tests.domain.test_batch_upload_request import _requests_for, uploader, warehouse  # noqa: F401


def test_delete_request_has_no_body(client: TestClient, session, uploader, warehouse):
    item = warehouse.tray_item()
    assert client.post("/requests/", json={
        "barcode_value": item.barcode.value,
        "external_request_id": "NO-BODY",
    }).status_code == 201
    request_id = _requests_for(session, item)[0].id

    response = client.delete(f"/requests/{request_id}")

    assert response.status_code == 204
    assert response.content == b""


def test_delete_pick_list_has_no_body(client: TestClient, session, uploader, warehouse):
    item = warehouse.tray_item()
    assert client.post("/requests/", json={
        "barcode_value": item.barcode.value,
        "external_request_id": "NO-BODY-2",
    }).status_code == 201
    request_id = _requests_for(session, item)[0].id

    class P:
        def __init__(self, name):
            self.name = name

    class G:
        permissions = [P("can_create_picklist_job"), P("can_delete_picklist_job"), P("can_access_picklist")]

    class MockUser:
        id = uploader.id
        first_name = "File"
        last_name = "Importer"
        groups = [G()]

    fastapi_app.dependency_overrides[get_current_user_with_permissions] = lambda: MockUser()
    created = client.post("/pick-lists/", json={"request_ids": [request_id], "created_by_id": uploader.id})
    assert created.status_code == 201, created.text

    response = client.delete(f"/pick-lists/{created.json()['id']}")

    assert response.status_code == 204
    assert response.content == b""
