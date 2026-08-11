
#!/usr/bin/env python3
"""Exercise a fresh single-merchant pilot deployment using synthetic data.

This script deliberately uses only the Python standard library. It is a
two-stage smoke test because restarting a Railway service is an operator action
that this script must not trigger unexpectedly:

    # PowerShell:
    $env:WOP_SMOKE_PASSWORD='...'
    python scripts/pilot_smoke_test.py run `
        --base-url https://SERVICE.up.railway.app `
        --operator pilot-smoke `
        --checkpoint pilot-smoke-checkpoint.json `
        --confirm-fresh-instance

Restart or redeploy the Railway service in its console, wait for its health
check to pass, then run:

    python scripts/pilot_smoke_test.py verify-persistence `
        --base-url https://SERVICE.up.railway.app `
        --operator pilot-smoke `
        --checkpoint pilot-smoke-checkpoint.json `
        --confirm-restarted

The run step replaces the catalog with a synthetic catalog. It refuses a
database that already has orders and requires an explicit acknowledgement; never
point it at a merchant deployment with real data. The script never sends or
prints a password, customer data, or real merchant catalog.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

SYNTHETIC_SKU = "PILOT-SMOKE-001"
SYNTHETIC_PRODUCT = "منتج اختبار تجريبي"
SYNTHETIC_UNIT = "قطعة"
SYNTHETIC_PRICE = Decimal("19.99")
SYNTHETIC_MESSAGE = "طلب اختبار فقط: 2 قطعة من منتج اختبار تجريبي"
PASSWORD_ENV = "WOP_SMOKE_PASSWORD"  # noqa: S105 - environment-variable name, not a secret
CHECKPOINT_VERSION = 1


class SmokeFailure(RuntimeError):
    """A failed assertion or HTTP interaction in a smoke-test step."""


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeFailure("Expected a JSON response, but did not receive one.") from exc


class ApiClient:
    """Small cookie-aware JSON/multipart client with no third-party package."""

    def __init__(self, base_url: str, *, timeout: float) -> None:
        self.base_url = validate_base_url(base_url)
        self.timeout = timeout
        jar = http.cookiejar.CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(jar))

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> ApiResponse:
        request_headers = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        # ``validate_base_url`` restricts this client to an HTTPS host before
        # this request object is constructed.
        request = Request(  # noqa: S310
            f"{self.base_url}{path}",
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return ApiResponse(
                    status=int(response.status),
                    body=response.read(),
                    headers={key.lower(): value for key, value in response.headers.items()},
                )
        except HTTPError as exc:
            return ApiResponse(
                status=int(exc.code),
                body=exc.read(),
                headers={key.lower(): value for key, value in exc.headers.items()},
            )
        except URLError as exc:
            raise SmokeFailure(f"Could not reach the service: {exc.reason}") from exc

    def json_request(self, method: str, path: str, payload: dict[str, Any]) -> ApiResponse:
        return self.request(
            method,
            path,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )


def validate_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise SmokeFailure("--base-url must be an absolute http(s) URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise SmokeFailure("--base-url must contain only scheme and host (no credentials or path).")
    if parsed.scheme != "https":
        raise SmokeFailure("Use HTTPS for this production smoke test.")
    return value


def require_password() -> str:
    password = os.getenv(PASSWORD_ENV, "")
    if not password:
        raise SmokeFailure(
            f"Set {PASSWORD_ENV} in the environment; do not put a password on the command line."
        )
    return password


def expect_status(response: ApiResponse, expected: int, step: str) -> None:
    if response.status == expected:
        return
    detail = ""
    try:
        data = response.json()
        if isinstance(data, dict) and isinstance(data.get("detail"), str):
            detail = f" Detail: {data['detail'][:200]}"
    except SmokeFailure:
        pass
    raise SmokeFailure(f"{step} returned HTTP {response.status}; expected {expected}.{detail}")


def expect_mapping(value: Any, step: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SmokeFailure(f"{step} returned an unexpected JSON shape.")
    return value


def make_multipart_catalog() -> tuple[bytes, str]:
    """Create a one-product synthetic UTF-8 CSV upload."""
    boundary = f"----wop-smoke-{uuid.uuid4().hex}"
    catalog = (
        "product_id,product_name,aliases,unit,price\n"
        f"{SYNTHETIC_SKU},{SYNTHETIC_PRODUCT},اختبار تجريبي|smoke test,"
        f"{SYNTHETIC_UNIT},{SYNTHETIC_PRICE}\n"
    ).encode("utf-8")
    parts = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="file"; filename="pilot-smoke-catalog.csv"\r\n',
        b"Content-Type: text/csv; charset=utf-8\r\n\r\n",
        catalog,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), boundary


def health_check(client: ApiClient) -> dict[str, Any]:
    response = client.request("GET", "/api/health")
    expect_status(response, 200, "Health check")
    data = expect_mapping(response.json(), "Health check")
    if data.get("status") != "healthy":
        raise SmokeFailure("Health endpoint did not report status='healthy'.")
    return data


def login(client: ApiClient, operator: str, password: str) -> None:
    response = client.json_request(
        "POST", "/api/login", {"operator": operator, "password": password}
    )
    expect_status(response, 200, "Authentication")
    data = expect_mapping(response.json(), "Authentication")
    if data.get("authenticated") is not True or data.get("operator") != operator:
        raise SmokeFailure("Authentication did not establish the requested operator session.")


def assert_fresh_orders(client: ApiClient) -> None:
    response = client.request("GET", "/api/orders?limit=1&offset=0")
    expect_status(response, 200, "Fresh-instance order check")
    rows = response.json()
    if not isinstance(rows, list):
        raise SmokeFailure("Fresh-instance order check returned an unexpected JSON shape.")
    if rows:
        raise SmokeFailure(
            "Refusing to replace the catalog: this deployment already contains orders. "
            "Use a separate fresh smoke-test deployment."
        )


def upload_synthetic_catalog(client: ApiClient) -> dict[str, Any]:
    body, boundary = make_multipart_catalog()
    response = client.request(
        "POST",
        "/api/catalog/upload",
        body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    expect_status(response, 200, "Synthetic catalog upload")
    data = expect_mapping(response.json(), "Synthetic catalog upload")
    if int(data.get("product_count", 0)) != 1:
        raise SmokeFailure("Synthetic catalog upload did not report exactly one product.")
    return data


def catalog_state(client: ApiClient) -> dict[str, Any]:
    query = urlencode({"q": SYNTHETIC_SKU, "limit": 5})
    response = client.request("GET", f"/api/catalog/search?{query}")
    expect_status(response, 200, "Catalog persistence check")
    rows = response.json()
    if not isinstance(rows, list):
        raise SmokeFailure("Catalog persistence check returned an unexpected JSON shape.")
    product = next((row for row in rows if row.get("product_id") == SYNTHETIC_SKU), None)
    if not isinstance(product, dict):
        raise SmokeFailure("Synthetic catalog SKU was not found.")
    try:
        price = Decimal(str(product.get("price")))
    except (InvalidOperation, ValueError) as exc:
        raise SmokeFailure("Synthetic catalog price was not a usable decimal value.") from exc
    if product.get("unit") != SYNTHETIC_UNIT or price != SYNTHETIC_PRICE:
        raise SmokeFailure("Synthetic catalog product changed unexpectedly.")
    return product


def process_synthetic_order(client: ApiClient, action_id: str) -> dict[str, Any]:
    response = client.json_request(
        "POST", "/api/process", {"message": SYNTHETIC_MESSAGE, "action_id": action_id}
    )
    expect_status(response, 200, "Synthetic order processing")
    data = expect_mapping(response.json(), "Synthetic order processing")
    items = data.get("items")
    if data.get("status") != "needs_review":
        raise SmokeFailure("Processed order was not routed to mandatory human review.")
    if not isinstance(data.get("order_id"), int) or not isinstance(items, list) or len(items) != 1:
        raise SmokeFailure("Synthetic order did not produce exactly one persistent review line.")
    return data


def verify_duplicate_process_retry(client: ApiClient, action_id: str, order_id: int) -> None:
    response = client.json_request(
        "POST", "/api/process", {"message": SYNTHETIC_MESSAGE, "action_id": action_id}
    )
    expect_status(response, 200, "Duplicate process retry")
    data = expect_mapping(response.json(), "Duplicate process retry")
    if data.get("order_id") != order_id:
        raise SmokeFailure("Duplicate process retry created or returned a different order ID.")


def review_order(client: ApiClient, *, order_id: int, item_id: int, operator: str) -> None:
    response = client.json_request(
        "PUT",
        f"/api/orders/{order_id}/items/{item_id}",
        {
            "actor": operator,
            "action_id": f"smoke-review-{uuid.uuid4()}",
            "final_decision": "SELECT",
            "selected_sku": SYNTHETIC_SKU,
            "quantity": 2,
            # This is intentionally the same deterministic unit as the catalog.
            # The smoke test does not exercise a conversion or a mismatch.
            "unit": SYNTHETIC_UNIT,
        },
    )
    expect_status(response, 200, "Human line review")
    data = expect_mapping(response.json(), "Human line review")
    matches = [item for item in data.get("items", []) if item.get("id") == item_id]
    if len(matches) != 1 or not matches[0].get("is_human_confirmed"):
        raise SmokeFailure("Human review did not produce a confirmed line.")
    if matches[0].get("human_selected_sku") != SYNTHETIC_SKU:
        raise SmokeFailure("Human review did not retain the selected synthetic SKU.")


def approve_order(client: ApiClient, *, order_id: int, operator: str) -> dict[str, Any]:
    response = client.json_request(
        "POST",
        f"/api/orders/{order_id}/approve",
        {"actor": operator, "action_id": f"smoke-approval-{uuid.uuid4()}"},
    )
    expect_status(response, 200, "Order approval")
    data = expect_mapping(response.json(), "Order approval")
    if data.get("status") != "approved" or not data.get("snapshot"):
        raise SmokeFailure("Order approval did not create an approved snapshot.")
    return data


def export_order(client: ApiClient, order_id: int) -> None:
    response = client.request("POST", f"/api/orders/{order_id}/export")
    expect_status(response, 200, "Approved-order export")
    if SYNTHETIC_SKU.encode("utf-8") not in response.body:
        raise SmokeFailure("Approved-order CSV export does not contain the synthetic SKU.")
    if "attachment" not in response.headers.get("content-disposition", "").lower():
        raise SmokeFailure("Approved-order export did not return an attachment response.")


def write_checkpoint(path: Path, data: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    if path.exists():
        raise SmokeFailure(f"Checkpoint already exists: {path}. Choose a new path.")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary_path = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def read_checkpoint(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure(f"Could not read checkpoint: {path}") from exc
    data = expect_mapping(data, "Checkpoint")
    if data.get("schema_version") != CHECKPOINT_VERSION:
        raise SmokeFailure("Checkpoint format is not supported by this script.")
    if not isinstance(data.get("order_id"), int) or data.get("sku") != SYNTHETIC_SKU:
        raise SmokeFailure("Checkpoint does not describe this synthetic smoke-test order.")
    return data


def run_smoke(args: argparse.Namespace) -> None:
    if not args.confirm_fresh_instance:
        raise SmokeFailure("run requires --confirm-fresh-instance because it replaces the catalog.")
    password = require_password()
    client = ApiClient(args.base_url, timeout=args.timeout)
    health = health_check(client)
    if health.get("extractor_available") is not True:
        raise SmokeFailure("Gemini extractor is unavailable; configure it before the processing smoke test.")
    login(client, args.operator, password)
    assert_fresh_orders(client)
    upload = upload_synthetic_catalog(client)
    catalog_state(client)

    process_action = f"smoke-process-{uuid.uuid4()}"
    processed = process_synthetic_order(client, process_action)
    order_id = processed["order_id"]
    verify_duplicate_process_retry(client, process_action, order_id)

    first_item = processed["items"][0]
    item_id = first_item.get("id") if isinstance(first_item, dict) else None
    if not isinstance(item_id, int):
        raise SmokeFailure("Processed review line did not have a persistent item ID.")
    review_order(client, order_id=order_id, item_id=item_id, operator=args.operator)
    approved = approve_order(client, order_id=order_id, operator=args.operator)
    export_order(client, order_id)

    checkpoint = {
        "schema_version": CHECKPOINT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "base_url": client.base_url,
        "order_id": order_id,
        "approved_at": approved.get("approved_at"),
        "sku": SYNTHETIC_SKU,
        "catalog_version": upload.get("version_id"),
        "catalog_content_sha256": upload.get("content_sha256"),
    }
    write_checkpoint(Path(args.checkpoint), checkpoint)
    print("PASS: health, authentication, catalog upload, review, approval, export, and duplicate retry.")
    print(f"Checkpoint written: {Path(args.checkpoint).expanduser().resolve()}")
    print("Restart/redeploy the service, wait for its Railway health check, then run verify-persistence.")


def verify_persistence(args: argparse.Namespace) -> None:
    if not args.confirm_restarted:
        raise SmokeFailure(
            "verify-persistence requires --confirm-restarted after an operator restart/redeploy."
        )
    checkpoint = read_checkpoint(Path(args.checkpoint))
    password = require_password()
    client = ApiClient(args.base_url, timeout=args.timeout)
    if checkpoint.get("base_url") != client.base_url:
        raise SmokeFailure("Checkpoint belongs to a different service URL.")
    health_check(client)
    login(client, args.operator, password)
    catalog_state(client)

    order_id = checkpoint["order_id"]
    response = client.request("GET", f"/api/orders/{order_id}")
    expect_status(response, 200, "Approved-order persistence check")
    order = expect_mapping(response.json(), "Approved-order persistence check")
    if order.get("status") != "approved":
        raise SmokeFailure("Approved order did not remain approved after restart.")
    snapshot = order.get("approved_items")
    if not isinstance(snapshot, list) or not any(
        isinstance(item, dict) and item.get("product_id") == SYNTHETIC_SKU for item in snapshot
    ):
        raise SmokeFailure("Approved snapshot did not retain the synthetic SKU after restart.")
    print("PASS: after the confirmed restart, health, catalog, and approved order persisted.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--base-url", required=True, help="HTTPS root URL of the Railway service")
        subparser.add_argument("--operator", required=True, help="Existing pilot operator username")
        subparser.add_argument(
            "--timeout", type=float, default=70.0,
            help="HTTP request timeout in seconds (default: 70)",
        )
        subparser.add_argument("--checkpoint", required=True, help="Path for the smoke-test checkpoint")

    run = subparsers.add_parser("run", help="Run against a fresh synthetic deployment")
    add_common(run)
    run.add_argument(
        "--confirm-fresh-instance",
        action="store_true",
        help="Acknowledge that this run replaces the catalog on a zero-order deployment",
    )

    verify = subparsers.add_parser(
        "verify-persistence", help="Verify catalog and order after an operator restart/redeploy"
    )
    add_common(verify)
    verify.add_argument(
        "--confirm-restarted",
        action="store_true",
        help="Acknowledge that the service was restarted/redeployed after the run step",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv or sys.argv[1:])
        if args.timeout <= 0:
            raise SmokeFailure("--timeout must be positive.")
        if args.command == "run":
            run_smoke(args)
        else:
            verify_persistence(args)
        return 0
    except SmokeFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
