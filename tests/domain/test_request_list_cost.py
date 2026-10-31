"""
GET /requests/ cost: the number of SQL statements must not grow with the rows returned.
"""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import event

from app.models.requests import Request
from app.models.users import User
from tests.domain.test_move_discrepancies import (
    _make_item,
    _make_shelf,
    _make_tray,
    _owner,
    _size_classes,
)
from tests.domain.test_request_list import _get, _location, reader, scenario  # noqa: F401


# --------------------------------------------------------------------------- #
class _StatementCounter:
    def __init__(self, session):
        self.engine = session.get_bind()
        self.count = 0

    def _on_execute(self, *_args):
        self.count += 1

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *_args):
        event.remove(self.engine, "before_cursor_execute", self._on_execute)


def _new_user(session, last_name):
    user = User(first_name="More", last_name=last_name, email=f"{uuid.uuid4().hex[:10]}@t.test")
    session.add(user)
    session.commit()
    return user


def _add_requests(session, tag, count, batch_upload_id=None):
    # Each request has its own requesting user and shelf location: the cases that
    # expose a per-row lookup.
    owner = _owner(session)
    size_class = _size_classes(session)[0]
    for _ in range(count):
        _, ladder = _location(session, f"Extra {uuid.uuid4().hex[:10]}")
        position = _make_shelf(session, ladder, owner, size_class, shelf_number=1)[1][0]
        item = _make_item(session, _make_tray(session, owner, size_class, position))
        session.add(Request(
            item_id=item.id,
            requested_by_id=_new_user(session, tag).id,
            requestor_name=f"Extra {tag}",
            batch_upload_id=batch_upload_id,
        ))
        session.commit()


def test_request_list_statements_do_not_grow_with_rows(client: TestClient, scenario, session):
    session.expire_all()
    with _StatementCounter(session) as few:
        assert _get(client, scenario.tag)["total"] == 4

    _add_requests(session, scenario.tag, 10)
    session.expire_all()
    with _StatementCounter(session) as many:
        assert _get(client, scenario.tag)["total"] == 14

    assert many.count <= few.count + 2, (few.count, many.count)
