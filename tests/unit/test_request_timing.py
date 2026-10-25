"""
Request timing in JWTMiddleware: Server-Timing header, slow-request warnings,
and the optional log-everything mode.
"""
from unittest.mock import patch

import pytest

from app.config.config import get_settings


@pytest.fixture
def timing_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "SLOW_REQUEST_THRESHOLD_MS", 500)
    monkeypatch.setattr(settings, "LOG_ALL_REQUEST_TIMES", False)
    return settings


def _logged(mock_method):
    return [call.args[0] for call in mock_method.call_args_list]


def test_response_has_server_timing_header(client, timing_settings):
    response = client.get("/status/")

    assert response.status_code == 200
    assert response.headers["Server-Timing"].startswith("app;dur=")
    duration = float(response.headers["Server-Timing"].split("=")[1])
    assert duration >= 0


def test_fast_request_not_logged_by_default(client, timing_settings):
    with patch("app.middleware.inventory_logger") as log:
        client.get("/status/")

    assert not any(e.get("event") == "slow_request" for e in _logged(log.warning))
    assert not any(e.get("event") == "request_timing" for e in _logged(log.info))


def test_slow_request_logged_as_warning(client, timing_settings, monkeypatch):
    monkeypatch.setattr(timing_settings, "SLOW_REQUEST_THRESHOLD_MS", 0)

    with patch("app.middleware.inventory_logger") as log:
        client.get("/status/")

    entries = [e for e in _logged(log.warning) if e.get("event") == "slow_request"]
    assert len(entries) == 1
    assert entries[0]["route"] == "/status/"
    assert entries[0]["method"] == "GET"
    assert entries[0]["response_status"] == 200
    assert entries[0]["duration_ms"] >= 0


def test_log_all_logs_fast_requests_as_info(client, timing_settings, monkeypatch):
    monkeypatch.setattr(timing_settings, "LOG_ALL_REQUEST_TIMES", True)

    with patch("app.middleware.inventory_logger") as log:
        client.get("/status/")

    entries = [e for e in _logged(log.info) if e.get("event") == "request_timing"]
    assert len(entries) == 1
    assert entries[0]["route"] == "/status/"


def test_route_pattern_logged_not_raw_path(client, timing_settings, monkeypatch):
    monkeypatch.setattr(timing_settings, "SLOW_REQUEST_THRESHOLD_MS", 0)

    with patch("app.middleware.inventory_logger") as log:
        client.get("/barcodes/value/12345678901")
        client.get("/no-such-route")

    routes = [e["route"] for e in _logged(log.warning) if e.get("event") == "slow_request"]
    assert "/barcodes/value/{value}" in routes
    assert "unmatched" in routes
    assert not any("12345678901" in r for r in routes)
