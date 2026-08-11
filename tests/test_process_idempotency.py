"""Durable, concurrent-safe idempotency tests for ``POST /api/process``."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app import database
from app.extractor import ExtractionUnavailable
from app.main import create_app
from app.models import ExtractedItem, ExtractionResult
from tests.helpers import login, process_payload

MESSAGE = "ابعتلي 6 فلاش ليمون"
ACTOR = "idempotency-reviewer"


class CountingExtractor:
    def __init__(self, *, block: bool = False, fail_first: bool = False) -> None:
        self.calls = 0
        self._lock = Lock()
        self.block = block
        self.fail_first = fail_first
        self.started = Event()
        self.release = Event()

    def extract(self, _message: str) -> ExtractionResult:
        with self._lock:
            self.calls += 1
            call_number = self.calls
        self.started.set()
        if self.fail_first and call_number == 1:
            raise ExtractionUnavailable("simulated transient extractor outage")
        if self.block:
            assert self.release.wait(timeout=10), "test extractor was not released"
        return ExtractionResult(
            items=[
                ExtractedItem(
                    raw_text="6 فلاش ليمون",
                    product_description="فلاش ليمون",
                    quantity=6.0,
                    unit="قطعة",
                )
            ]
        )


@pytest.fixture
def process_app(tmp_path, monkeypatch):
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    extractor = CountingExtractor()
    monkeypatch.setattr(main_module, "extractor", extractor)
    return create_app(db_path), db_path, extractor


def _orders_count(db_path) -> int:
    with database.session(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0])


def _post_as_fresh_browser(app, payload: dict[str, str]) -> tuple[int, dict]:
    client = TestClient(app)
    try:
        login(client, ACTOR)
        response = client.post("/api/process", json=payload)
        return response.status_code, response.json()
    finally:
        client.close()


def test_process_replay_returns_the_original_order_without_second_extraction(process_app):
    app, db_path, extractor = process_app
    payload = process_payload(MESSAGE, "process-replay")
    with TestClient(app) as client:
        login(client, ACTOR)
        first = client.post("/api/process", json=payload)
        second = client.post("/api/process", json=payload)

    assert first.status_code == second.status_code == 200
    assert first.json()["order_id"] == second.json()["order_id"]
    assert extractor.calls == 1
    assert _orders_count(db_path) == 1
    with database.session(db_path) as conn:
        request = conn.execute(
            "SELECT state, order_id, attempt_count FROM process_requests WHERE action_id=?",
            (payload["action_id"],),
        ).fetchone()
    assert tuple(request) == ("succeeded", first.json()["order_id"], 1)


def test_concurrent_same_key_creates_exactly_one_order(process_app, monkeypatch):
    app, db_path, extractor = process_app
    extractor.block = True
    monkeypatch.setattr(main_module, "PROCESS_REQUEST_WAIT_SECONDS", 10.0)
    payload = process_payload(MESSAGE, "process-concurrent")

    with TestClient(app):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(_post_as_fresh_browser, app, payload)
            assert extractor.started.wait(timeout=5), "first request did not reach extractor"
            second = pool.submit(_post_as_fresh_browser, app, payload)
            extractor.release.set()
            results = [first.result(timeout=15), second.result(timeout=15)]

    assert [status for status, _body in results] == [200, 200]
    assert results[0][1]["order_id"] == results[1][1]["order_id"]
    assert extractor.calls == 1
    assert _orders_count(db_path) == 1


def test_same_key_with_different_payload_fails_closed(process_app):
    app, db_path, extractor = process_app
    key = "process-conflict"
    with TestClient(app) as client:
        login(client, ACTOR)
        created = client.post("/api/process", json=process_payload(MESSAGE, key))
        conflict = client.post("/api/process", json=process_payload("رسالة مختلفة", key))

    assert created.status_code == 200
    assert conflict.status_code == 409
    assert extractor.calls == 1
    assert _orders_count(db_path) == 1


def test_completed_process_key_survives_application_restart(process_app, monkeypatch):
    app, db_path, _extractor = process_app
    payload = process_payload(MESSAGE, "process-restart")
    with TestClient(app) as client:
        login(client, ACTOR)
        created = client.post("/api/process", json=payload)
    assert created.status_code == 200

    class MustNotExtract:
        def extract(self, _message: str):  # pragma: no cover - assertion below proves it is unused
            raise AssertionError("replay must not invoke Gemini")

    monkeypatch.setattr(main_module, "extractor", MustNotExtract())
    restarted = create_app(db_path)
    with TestClient(restarted) as client:
        login(client, ACTOR)
        replay = client.post("/api/process", json=payload)

    assert replay.status_code == 200
    assert replay.json()["order_id"] == created.json()["order_id"]
    assert _orders_count(db_path) == 1


def test_extractor_failure_can_retry_the_same_key_without_creating_duplicates(process_app):
    app, db_path, extractor = process_app
    extractor.fail_first = True
    payload = process_payload(MESSAGE, "process-retry-after-timeout")
    with TestClient(app) as client:
        login(client, ACTOR)
        failed = client.post("/api/process", json=payload)
        succeeded = client.post("/api/process", json=payload)

    assert failed.status_code == 503
    assert succeeded.status_code == 200
    assert extractor.calls == 2
    assert _orders_count(db_path) == 1
    with database.session(db_path) as conn:
        request = conn.execute(
            "SELECT state, attempt_count, order_id FROM process_requests WHERE action_id=?",
            (payload["action_id"],),
        ).fetchone()
    assert tuple(request) == ("succeeded", 2, succeeded.json()["order_id"])


def test_process_request_requires_a_client_generated_action_id(process_app):
    app, _db_path, _extractor = process_app
    with TestClient(app) as client:
        login(client, ACTOR)
        response = client.post("/api/process", json={"message": MESSAGE})
    assert response.status_code == 422
