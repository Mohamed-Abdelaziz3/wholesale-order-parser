"""FastAPI application for the Wholesale Order Parser.

Concurrency note
----------------
Route handlers that touch the LLM provider or SQLite are declared with ``def``,
not ``async def``. Both are blocking calls; running them on the event loop
serialised the whole service to one in-flight request at a time. As plain
``def`` handlers FastAPI dispatches them to a worker thread pool, so a slow
provider call no longer stalls every other operator.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Optional

from dotenv import load_dotenv
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi import (
    Path as APIPath,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import database
from .catalog import CatalogError, catalog_template_csv, load_catalog, parse_catalog_bytes
from .documents import build_document_context
from .extractor import ExtractionUnavailable, GeminiExtractor
from .matcher import ProductMatcher
from .models import (
    ActorActionRequest,
    ApprovalRequest,
    ApprovedSnapshotItem,
    AuditEventResponse,
    CatalogProduct,
    CatalogUploadResult,
    CustomerDetailsRequest,
    DiscountRequest,
    ExportRequest,
    ItemReviewRequest,
    LoginRequest,
    ManualOrderItemRequest,
    MatchCandidate,
    OrderApproveResponse,
    OrderLineResult,
    OrderRequest,
    OrderResult,
    OrderSummary,
    PriceOverrideRequest,
    SessionInfo,
    ShopSettings,
    ShopSettingsRequest,
    TimeSavedEstimate,
)
from .routing import route_rr_k_outcome, rr_k_auto_accept_enabled
from .security import (
    SESSION_ISSUED_AT_KEY,
    SESSION_OPERATOR_KEY,
    LoginThrottle,
    account_key,
    announce,
    attachment_filename,
    bind_actor,
    client_key,
    csrf_violation,
    csv_safe,
    load_auth_config,
    require_operator,
    sanitize_logo_data_url,
    session_operator,
)
from .xlsx_export import build_order_workbook, export_date, filename_stem

# Keep local secrets and the runtime database outside synced project folders.
# Fall back to the conventional working-directory .env for deployments that
# provide their configuration through the project environment.
load_dotenv()
if os.name == "nt":
    load_dotenv(r"C:\ProgramData\wop\.env")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

APP_TITLE = "محلل طلبات الجملة"
# Deliberately not "فاتورة". This build produces an internal picking / delivery
# note, and nothing it emits may read as a document with tax standing.
APP_DESCRIPTION = "تحويل طلبات الواتساب المكتوبة بالعامية المصرية إلى طلبات مراجَعة ومعتمدة"
APP_VERSION = "3.0.0"

MANUAL_ENTRY_SECONDS_PER_ITEM = 45.0
MAX_CATALOG_UPLOAD_BYTES = 8 * 1024 * 1024

# Module-level extractor so tests can monkeypatch ``app.main.extractor`` before
# startup. Startup only creates one when none has been injected.
extractor: Any = None


# ---------------------------------------------------------------------------
# Catalog state
# ---------------------------------------------------------------------------


class CatalogState:
    """The working catalog plus its derived matcher, swapped atomically."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._products: list[CatalogProduct] = []
        self._lookup: dict[str, CatalogProduct] = {}
        self._matcher = ProductMatcher([])

    def install(self, products: list[CatalogProduct]) -> None:
        matcher = ProductMatcher(products)
        lookup = {p.product_id: p for p in products}
        with self._lock:
            self._products = products
            self._lookup = lookup
            self._matcher = matcher

    @property
    def products(self) -> list[CatalogProduct]:
        with self._lock:
            return self._products

    @property
    def lookup(self) -> dict[str, CatalogProduct]:
        with self._lock:
            return self._lookup

    @property
    def matcher(self) -> ProductMatcher:
        with self._lock:
            return self._matcher

    def __len__(self) -> int:
        with self._lock:
            return len(self._products)


def _catalog_state(request: Request) -> CatalogState:
    return request.app.state.catalog_state


def _request_db_path(request: Request) -> str:
    """Return the database explicitly bound to this application instance."""
    return request.app.state.database_path


def _seed_catalog_if_empty(db_path: str) -> None:
    """First run: install the bundled demo catalog so the app is never empty."""
    if not database.catalog_is_empty(db_path):
        return
    try:
        products = load_catalog()
    except (FileNotFoundError, CatalogError) as exc:
        logger.warning("No seed catalog installed: %s", exc)
        return
    database.replace_catalog(
        [p.model_dump() for p in products],
        db_path,
        source_name="catalog.csv (bundled demo)",
        actor="system:seed",
    )
    logger.info("Seeded catalog with %s demo products", len(products))


def _refresh_catalog(application: FastAPI) -> None:
    rows = database.load_catalog_rows(application.state.database_path)
    application.state.catalog_state.install([CatalogProduct(**row) for row in rows])
    logger.info("Working catalog: %s products", len(application.state.catalog_state))


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _build_time_estimate(order_data: dict, total_items: int) -> TimeSavedEstimate:
    """Report measured time only; never claim a saving that has not happened yet."""
    actual_ms = float(order_data.get("processing_time_ms") or 0.0)
    manual_ms = max(total_items, 1) * MANUAL_ENTRY_SECONDS_PER_ITEM * 1000.0

    created = _parse_iso(order_data.get("created_at"))
    approved = _parse_iso(order_data.get("approved_at"))
    cycle_seconds: Optional[float] = None
    saved_seconds: Optional[float] = None

    if created and approved and approved >= created:
        cycle_seconds = round((approved - created).total_seconds(), 1)
        saved_seconds = round(max(0.0, (manual_ms / 1000.0) - cycle_seconds), 1)

    return TimeSavedEstimate(
        actual_system_time_ms=round(actual_ms, 1),
        estimated_manual_time_ms=manual_ms,
        review_cycle_seconds=cycle_seconds,
        saved_seconds=saved_seconds,
        is_measured=cycle_seconds is not None,
    )


def _catalog_product(
    lookup: dict[str, CatalogProduct], product_id: Any
) -> Optional[CatalogProduct]:
    if not product_id:
        return None
    product = lookup.get(product_id)
    if product is None:
        return None
    return CatalogProduct(
        product_id=product.product_id,
        product_name=product.product_name,
        aliases=product.aliases,
        unit=product.unit,
        price=product.price,
    )


def _format_order_response(order_data: dict, lookup: dict[str, CatalogProduct]) -> OrderResult:
    """Format a raw database order dict into an ``OrderResult``.

    Once an order is approved its lines are rendered from the immutable snapshot,
    not from the live catalog. Resolving through the catalog meant that replacing
    or repricing the catalog silently changed what an old approved order appeared
    to say — the export was still correct, but the screen was not, which quietly
    contradicts the guarantee the whole approval flow exists to make.
    """
    items: list[OrderLineResult] = []
    total_confirmed = 0
    subtotal = 0.0
    active_items = 0

    snapshot_by_item = {
        row["original_item_id"]: row for row in order_data.get("approved_items", [])
    }

    for item_row in order_data.get("items", []):
        frozen = snapshot_by_item.get(item_row.get("id"))
        catalog_price = (
            frozen.get("catalog_price") if frozen else item_row.get("catalog_price")
        )
        price_override = (
            frozen.get("price_override") if frozen else item_row.get("price_override")
        )
        price_overridden = bool(
            (frozen or item_row).get("price_overridden") or price_override is not None
        )
        is_manual_line = bool((frozen or item_row).get("is_manual_line")) or (
            (frozen or item_row).get("model_decision") == "MANUAL"
            or item_row.get("recommendation_decision") == "MANUAL"
        )

        if frozen:
            matched_prod = CatalogProduct(
                product_id=frozen["product_id"],
                product_name=frozen["product_name"],
                aliases=[],
                unit=frozen["unit"],
                price=float(frozen.get("price") or 0.0),
            )
        elif item_row.get("matched_product_id"):
            # Keep the reviewed catalog baseline stable while the order is
            # mutable. A later catalog refresh must not replace a human price.
            effective_price = (
                float(price_override)
                if price_override is not None
                else float(
                    item_row.get("matched_price")
                    if item_row.get("matched_price") is not None
                    else catalog_price
                    or 0.0
                )
            )
            matched_prod = CatalogProduct(
                product_id=item_row["matched_product_id"],
                product_name=item_row.get("matched_product_name") or "",
                aliases=[],
                unit=item_row.get("matched_unit") or item_row.get("extracted_unit") or "",
                price=effective_price,
            )
        else:
            matched_prod = None
        recommended_prod = _catalog_product(lookup, item_row.get("recommendation_product_id"))

        candidates = [
            MatchCandidate(
                product_id=c.get("product_id", ""),
                product_name=c.get("product_name", ""),
                score=max(0.0, min(1.0, float(c.get("score", 0.0) or 0.0))),
                unit=c.get("unit", "") or "",
                price=float(c.get("price", 0.0) or 0.0),
            )
            for c in item_row.get("candidates", [])
            if c.get("product_id")
        ]

        is_cancelled = item_row.get("status") == "cancelled_by_human"
        if not is_cancelled:
            active_items += 1
        if not is_cancelled and item_row.get("is_human_confirmed"):
            total_confirmed += 1

        # A recommendation is an estimate while review is pending. Cancelled
        # and confirmed-NOT_FOUND lines never contribute; a human price override
        # wins as soon as a line has been selected.
        if not is_cancelled and item_row.get("status") != "not_found_confirmed":
            display_price = (
                matched_prod.price if matched_prod else (recommended_prod.price if recommended_prod else None)
            )
            if display_price is not None:
                try:
                    subtotal += round(
                        float(display_price) * float(item_row.get("extracted_quantity") or 0), 2
                    )
                except (TypeError, ValueError):
                    pass

        items.append(
            OrderLineResult(
                id=item_row.get("id"),
                raw_text=item_row.get("raw_text", ""),
                extracted_product=item_row.get("extracted_product", ""),
                extracted_quantity=item_row.get("extracted_quantity", 1.0),
                extracted_unit=item_row.get("extracted_unit", "قطعة"),
                matched_product=matched_prod,
                recommended_product=recommended_prod,
                recommendation_decision=item_row.get("recommendation_decision"),
                routing_reason=(
                    "Model recommendations are advisory and require human confirmation"
                    if item_row.get("recommendation_decision")
                    else None
                ),
                confidence=item_row.get("confidence", 0.0),
                status=item_row.get("status", "needs_review"),
                reason=(
                    None
                    if item_row.get("status")
                    in {"human_selected", "not_found_confirmed", "cancelled_by_human"}
                    else "يحتاج مراجعة أو مطابقة"
                ),
                alternatives=candidates,
                candidates=candidates,
                is_manually_corrected=bool(item_row.get("is_manually_corrected")),
                is_human_confirmed=bool(item_row.get("is_human_confirmed")),
                human_decision=item_row.get("human_decision"),
                human_selected_sku=item_row.get("human_selected_sku"),
                human_actor=item_row.get("human_actor"),
                human_confirmed_at=item_row.get("human_confirmed_at"),
                review_action_id=item_row.get("review_action_id"),
                catalog_price=(float(catalog_price) if catalog_price is not None else None),
                price_override=(float(price_override) if price_override is not None else None),
                price_overridden=price_overridden,
                is_manual_line=is_manual_line,
            )
        )

    approved_snapshots = []
    for raw_snapshot in order_data.get("approved_items", []):
        snapshot_item = dict(raw_snapshot)
        snapshot_item["is_manual_line"] = bool(snapshot_item.get("is_manual_line")) or (
            snapshot_item.get("model_decision") == "MANUAL"
        )
        approved_snapshots.append(ApprovedSnapshotItem(**snapshot_item))

    subtotal = round(subtotal, 2)
    frozen_discount = order_data.get("approved_discount")
    discount = float(
        frozen_discount
        if order_data.get("status") == "approved" and frozen_discount is not None
        else order_data.get("discount") or 0.0
    )
    grand_total = round(max(0.0, subtotal - discount), 2)

    return OrderResult(
        order_id=order_data.get("id"),
        status=order_data.get("status", "analyzed"),
        original_message=order_data.get("original_text", ""),
        items=items,
        approved_items=approved_snapshots,
        # Read back from the stored analysis. This was hardcoded to [] , so a
        # customer instruction the extractor could not turn into a line —
        # "ومتنساش الفكة", "خليهم بعد المغرب" — was recorded in the audit trail
        # and then never shown to the one person who needed to act on it.
        unresolved=list(order_data.get("unresolved") or []),
        total_confirmed=total_confirmed,
        total_items=active_items,
        export_count=order_data.get("export_count", 0),
        last_exported_at=order_data.get("last_exported_at"),
        time_saved=_build_time_estimate(order_data, len(items)),
        customer_name=order_data.get("customer_name"),
        customer_phone=order_data.get("customer_phone"),
        customer_address=order_data.get("customer_address"),
        discount=discount,
        subtotal=subtotal,
        grand_total=grand_total,
    )


def _server_error(context: str, error: BaseException) -> HTTPException:
    """Log the detail, return a generic message.

    Echoing ``str(exc)`` to the client leaked provider URLs, file paths and
    driver internals to anyone who could reach the endpoint.
    """
    logger.error("%s: %s", context, error, exc_info=True)
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="حصل خطأ غير متوقع أثناء تنفيذ العملية. راجع سجل الخادم للتفاصيل.",
    )


# ---------------------------------------------------------------------------
# Public routes (no authentication)
# ---------------------------------------------------------------------------

public_router = APIRouter()
router = APIRouter()


@public_router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if session_operator(request):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request, name="login.html", context={"app_title": APP_TITLE}
    )


@public_router.post("/api/login", response_model=SessionInfo)
def login(request: Request, payload: LoginRequest):
    config = request.app.state.auth_config
    throttle = request.app.state.login_throttle
    operator = payload.operator.strip()
    # Two independent counters: the caller's address and the account being
    # guessed. Spoofing X-Forwarded-For no longer buys unlimited attempts,
    # because the account counter does not depend on the network address.
    keys = (client_key(request), account_key(operator))

    blocked = max(throttle.blocked_for(k) for k in keys)
    if blocked > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"محاولات كتيرة. حاول تاني بعد {int(blocked)} ثانية.",
        )

    if not config.verify(operator, payload.password):
        for k in keys:
            throttle.record_failure(k)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="اسم المستخدم أو كلمة المرور غير صحيحة.",
        )

    for k in keys:
        throttle.reset(k)
    request.session[SESSION_OPERATOR_KEY] = operator
    request.session[SESSION_ISSUED_AT_KEY] = time.time()
    logger.info("Operator signed in: %s", operator)
    return SessionInfo(operator=operator, authenticated=True)


@public_router.post("/api/logout", response_model=SessionInfo)
def logout(request: Request):
    request.session.clear()
    return SessionInfo(operator=None, authenticated=False)


@public_router.get("/api/session", response_model=SessionInfo)
def read_session(request: Request):
    operator = session_operator(request)
    return SessionInfo(operator=operator, authenticated=operator is not None)


@public_router.get("/api/health")
def health_check(request: Request):
    """Unauthenticated liveness probe. Exposes no order or catalog contents."""
    return {
        "status": "healthy",
        "version": APP_VERSION,
        "catalog_loaded": len(request.app.state.catalog_state),
        "extractor_available": extractor is not None,
        "database": "sqlite_wal",
        "auth_enforced": True,
        "rr_k_auto_accept_enabled": rr_k_auto_accept_enabled(),
    }


# ---------------------------------------------------------------------------
# Authenticated routes
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    operator = session_operator(request)
    if not operator:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "catalog_count": len(request.app.state.catalog_state),
            "extractor_available": extractor is not None,
            "operator": operator,
            "app_title": APP_TITLE,
        },
    )


@router.post("/api/process", response_model=OrderResult)
def process_order(
    order: OrderRequest,
    request: Request,
    operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """Extract, match, and persist advisory results for one raw order message."""
    if extractor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="خدمة الاستخراج غير متاحة. تأكد من ضبط GEMINI_API_KEY.",
        )

    state = _catalog_state(request)
    if len(state) == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="لا يوجد كتالوج منتجات. ارفع ملف الأصناف أولاً من زر «الكتالوج».",
        )

    message = order.message.strip()
    if not message:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="الرسالة فارغة")

    start_time = time.monotonic()

    try:
        extraction = extractor.extract(message)
    except ExtractionUnavailable as exc:
        logger.error("Extraction provider unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="خدمة الاستخراج غير متاحة مؤقتاً. حاول تاني بعد شوية.",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise _server_error("Extraction failed", exc) from exc

    matcher = state.matcher
    items_data = []
    for item in extraction.items:
        res = matcher.match_item(item)
        # The decision is driven by the matcher's status, not merely by whether a
        # product object exists. An ambiguous line now carries its best candidate
        # (so the reviewer does not have to hunt for it) while still being routed
        # as REVIEW rather than SELECT.
        if res.status == "not_found":
            recommendation_decision = "NOT_FOUND"
        elif res.status == "advisory_select" and res.matched_product:
            recommendation_decision = "SELECT"
        else:
            recommendation_decision = "REVIEW"
        route = route_rr_k_outcome(recommendation_decision, res.confidence)

        candidates = [
            {
                "product_id": alt.product_id,
                "product_name": alt.product_name,
                "score": alt.score,
                "unit": alt.unit,
                "price": alt.price,
            }
            for alt in res.alternatives
        ]

        recommendation_dict = None
        if res.matched_product:
            recommendation_dict = {
                "product_id": res.matched_product.product_id,
                "product_name": res.matched_product.product_name,
                "unit": res.matched_product.unit,
                "price": res.matched_product.price,
            }
            if not any(c["product_id"] == recommendation_dict["product_id"] for c in candidates):
                candidates.insert(0, {**recommendation_dict, "score": res.confidence})

        items_data.append(
            {
                "raw_text": res.raw_text,
                "extracted_product": res.extracted_product,
                "extracted_quantity": res.extracted_quantity,
                "extracted_unit": res.extracted_unit,
                "matched_product": None,
                "recommendation_product": recommendation_dict,
                "confidence": res.confidence,
                "status": route.review_status,
                "recommendation_decision": route.decision,
                "candidates": candidates,
                "is_manually_corrected": False,
            }
        )

    processing_time_ms = round((time.monotonic() - start_time) * 1000.0, 2)

    try:
        order_id = database.save_processed_order(
            original_text=message,
            items_data=items_data,
            unresolved_text=extraction.unresolved_text,
            processing_time_ms=processing_time_ms,
            db_path=db_path,
        )
        saved_order = database.get_order_by_id(order_id, db_path)
    except Exception as exc:  # noqa: BLE001
        raise _server_error("Failed to persist order", exc) from exc

    if not saved_order:
        raise _server_error("Saved order could not be reloaded", RuntimeError(str(order_id)))

    logger.info(
        "Order #%s analysed by %s: %s items in %sms",
        order_id,
        operator,
        len(items_data),
        processing_time_ms,
    )
    return _format_order_response(saved_order, state.lookup)


@router.get("/api/orders", response_model=list[OrderSummary])
def list_orders(
    limit: int = 25,
    offset: int = 0,
    search: str = "",
    _operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """Recent orders, newest first, for the history panel."""
    rows = database.list_orders(db_path, limit=limit, offset=offset, search=search)
    return [
        OrderSummary(
            order_id=row["id"],
            status=row["status"],
            original_text=(row["original_text"] or "")[:280],
            created_at=row["created_at"],
            approved_at=row["approved_at"],
            total_items=int(row["total_items"] or 0),
            confirmed_items=int(row["confirmed_items"] or 0),
            export_count=int(row["export_count"] or 0),
            last_exported_at=row["last_exported_at"],
            customer_name=row.get("customer_name"),
            customer_phone=row.get("customer_phone"),
            customer_address=row.get("customer_address"),
        )
        for row in rows
    ]


@router.get("/api/orders/{order_id}", response_model=OrderResult)
def get_order(
    request: Request,
    order_id: int = APIPath(..., ge=1, description="Order ID"),
    _operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    order_data = database.get_order_by_id(order_id, db_path)
    if not order_data:
        raise HTTPException(status_code=404, detail=f"الطلب رقم #{order_id} غير موجود")
    return _format_order_response(order_data, _catalog_state(request).lookup)


@router.put("/api/orders/{order_id}/customer", response_model=OrderResult)
def update_order_customer(
    request: Request,
    order_id: int,
    payload: CustomerDetailsRequest,
    operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """Attach customer contact data through an attributable human command."""
    actor = bind_actor(payload.actor, operator)
    try:
        updated = database.update_order_customer(
            order_id,
            actor=actor,
            action_id=payload.action_id,
            customer_name=payload.customer_name,
            customer_phone=payload.customer_phone,
            customer_address=payload.customer_address,
            db_path=db_path,
        )
        return _format_order_response(updated, _catalog_state(request).lookup)
    except ValueError as exc:
        logger.warning("Customer update rejected for order #%s: %s", order_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _server_error(f"Customer update failed for order #{order_id}", exc) from exc


@router.post("/api/orders/{order_id}/items", response_model=OrderResult)
def add_manual_order_item(
    request: Request,
    order_id: int,
    payload: ManualOrderItemRequest,
    operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """Add a catalog line with the same human evidence required for review."""
    actor = bind_actor(payload.actor, operator)
    try:
        updated = database.add_manual_order_item(
            order_id,
            actor=actor,
            action_id=payload.action_id,
            selected_sku=payload.selected_sku,
            quantity=payload.quantity,
            unit=payload.unit,
            catalog_lookup=_catalog_state(request).lookup,
            db_path=db_path,
        )
        return _format_order_response(updated, _catalog_state(request).lookup)
    except ValueError as exc:
        logger.warning("Manual line rejected for order #%s: %s", order_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _server_error(f"Manual line failed for order #{order_id}", exc) from exc


@router.delete("/api/orders/{order_id}/items/{item_id}", response_model=OrderResult)
def cancel_order_item(
    request: Request,
    order_id: int,
    item_id: int,
    payload: ActorActionRequest,
    operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """Soft-cancel a line while retaining the human decision and audit trail."""
    actor = bind_actor(payload.actor, operator)
    try:
        updated = database.cancel_order_item(
            order_id,
            item_id,
            actor=actor,
            action_id=payload.action_id,
            db_path=db_path,
        )
        return _format_order_response(updated, _catalog_state(request).lookup)
    except ValueError as exc:
        logger.warning("Cancellation rejected for order #%s item #%s: %s", order_id, item_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _server_error(f"Cancellation failed for order #{order_id} item #{item_id}", exc) from exc


@router.put("/api/orders/{order_id}/items/{item_id}/price", response_model=OrderResult)
def override_order_item_price(
    request: Request,
    order_id: int,
    item_id: int,
    payload: PriceOverrideRequest,
    operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """Record a pre-approval unit-price override with a durable audit event."""
    actor = bind_actor(payload.actor, operator)
    try:
        updated = database.override_order_item_price(
            order_id,
            item_id,
            actor=actor,
            action_id=payload.action_id,
            price=payload.price,
            db_path=db_path,
        )
        return _format_order_response(updated, _catalog_state(request).lookup)
    except ValueError as exc:
        logger.warning("Price override rejected for order #%s item #%s: %s", order_id, item_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _server_error(f"Price override failed for order #{order_id} item #{item_id}", exc) from exc


@router.put("/api/orders/{order_id}/discount", response_model=OrderResult)
def update_order_discount(
    request: Request,
    order_id: int,
    payload: DiscountRequest,
    operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """Set the absolute pre-approval discount through an audited command."""
    actor = bind_actor(payload.actor, operator)
    try:
        updated = database.update_order_discount(
            order_id,
            actor=actor,
            action_id=payload.action_id,
            discount=payload.discount,
            db_path=db_path,
        )
        return _format_order_response(updated, _catalog_state(request).lookup)
    except ValueError as exc:
        logger.warning("Discount update rejected for order #%s: %s", order_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _server_error(f"Discount update failed for order #{order_id}", exc) from exc


@router.post("/api/orders/{order_id}/items/{item_id}/review", response_model=OrderResult)
@router.put("/api/orders/{order_id}/items/{item_id}", response_model=OrderResult)
def update_item(
    request: Request,
    order_id: int,
    item_id: int,
    update_req: ItemReviewRequest,
    operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """Apply one complete human review command; partial updates fail validation."""
    actor = bind_actor(update_req.actor, operator)
    state = _catalog_state(request)
    try:
        updated_order = database.review_order_item(
            order_id=order_id,
            item_id=item_id,
            actor=actor,
            action_id=update_req.action_id,
            final_decision=update_req.final_decision,
            selected_sku=update_req.selected_sku,
            quantity=update_req.quantity,
            unit=update_req.unit,
            catalog_lookup=state.lookup,
            db_path=db_path,
        )
        return _format_order_response(updated_order, state.lookup)
    except ValueError as exc:
        logger.warning("Review rejected for order #%s item #%s: %s", order_id, item_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _server_error(f"Review failed for order #{order_id}", exc) from exc


@router.post(
    "/api/orders/{order_id}/items/{item_id}/confirm-not-found", response_model=OrderResult
)
def confirm_not_found_item(
    request: Request,
    order_id: int,
    item_id: int,
    review_req: ItemReviewRequest,
    operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """Compatibility route for an explicit human NOT_FOUND review command."""
    actor = bind_actor(review_req.actor, operator)
    state = _catalog_state(request)
    try:
        if review_req.final_decision != "NOT_FOUND":
            raise ValueError("confirm-not-found requires final_decision=NOT_FOUND")
        return _format_order_response(
            database.confirm_not_found(
                order_id,
                item_id,
                actor=actor,
                action_id=review_req.action_id,
                catalog_lookup=state.lookup,
                db_path=db_path,
            ),
            state.lookup,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _server_error(f"NOT_FOUND confirmation failed for order #{order_id}", exc) from exc


@router.post("/api/orders/{order_id}/approve", response_model=OrderApproveResponse)
def approve_order_endpoint(
    order_id: int,
    approval_req: ApprovalRequest,
    operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """Approve an order and create the immutable snapshot. Idempotent by action_id."""
    actor = bind_actor(approval_req.actor, operator)
    try:
        res = database.approve_order(
            order_id, actor=actor, action_id=approval_req.action_id, db_path=db_path
        )
        return OrderApproveResponse(
            order_id=res["order_id"],
            status=res["status"],
            message=res["message"],
            approved_at=res["approved_at"],
            snapshot=[ApprovedSnapshotItem(**item) for item in res["snapshot"]],
        )
    except ValueError as exc:
        logger.warning("Approval validation failed for order #%s: %s", order_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _server_error(f"Approval failed for order #{order_id}", exc) from exc


@router.post("/api/orders/{order_id}/export")
def export_order_csv(
    order_id: int,
    _operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """Export only the verified approved snapshot of an approved order."""
    try:
        export_data = database.record_export(order_id, db_path)
    except ValueError as exc:
        logger.warning("Export validation failed for order #%s: %s", order_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _server_error(f"Export failed for order #{order_id}", exc) from exc

    output = io.StringIO()
    output.write("﻿")  # BOM so Excel opens Arabic correctly
    writer = csv.writer(output)

    # Customer data is operator-entered, not trusted spreadsheet content.
    # Keep it in the header block and pass every cell through csv_safe.
    writer.writerow(["اسم العميل", csv_safe(export_data.get("customer_name", ""))])
    writer.writerow(["هاتف العميل", csv_safe(export_data.get("customer_phone", ""))])
    writer.writerow(["عنوان العميل", csv_safe(export_data.get("customer_address", ""))])

    # Header block. The order message used to be repeated verbatim on every
    # line, so a ten-line order carried ten copies of a twelve-line WhatsApp
    # message and the product columns were pushed off the screen. It belongs
    # here: stated once, above the table, where a human reads it.
    writer.writerow(["رقم الطلب (Order ID)", csv_safe(order_id)])
    writer.writerow(["تاريخ التصدير", csv_safe(export_data["last_exported_at"])])
    writer.writerow(["رسالة العميل", csv_safe(export_data["original_text"])])
    writer.writerow([])

    writer.writerow(
        [
            "#",
            "كود المنتج",
            "اسم المنتج",
            "الكمية",
            "الوحدة",
            "السعر الفردي",
            "الإجمالي",
            "Model confidence (advisory)",
            "Human confirmed",
            "Human actor",
            "تعديل يدوي (Corrected)",
            "Catalog unit price",
            "Price override",
        ]
    )

    order_total = 0.0
    for index, item in enumerate(export_data["snapshot"], start=1):
        price = float(item.get("price", 0.0) or 0.0)
        qty = float(item.get("quantity", 0.0) or 0.0)
        line_total = round(price * qty, 2)
        order_total += line_total
        writer.writerow(
            [
                index,
                csv_safe(item.get("product_id", "")),
                csv_safe(item.get("product_name", "")),
                csv_safe(qty),
                csv_safe(item.get("unit", "")),
                csv_safe(price),
                csv_safe(line_total),
                csv_safe(f"{float(item.get('confidence', 0.0) or 0.0):.0%}"),
                "yes" if item.get("is_human_confirmed") else "no",
                csv_safe(item.get("human_actor", "")),
                "نعم" if item.get("is_manually_corrected") else "لا",
                csv_safe(item.get("catalog_price", "")),
                csv_safe(item.get("price_override", "")),
            ]
        )

    discount = float(export_data.get("discount") or 0.0)
    try:
        grand_total = round(float(export_data.get("grand_total")), 2)
    except (TypeError, ValueError):
        grand_total = round(max(0.0, round(order_total, 2) - discount), 2)
    writer.writerow([])
    writer.writerow(["", "", "الإجمالي قبل الخصم", "", "", "", csv_safe(round(order_total, 2))])
    writer.writerow(["", "", "الخصم", "", "", "", csv_safe(discount)])
    writer.writerow(["", "", "الإجمالي النهائي", "", "", "", csv_safe(grand_total)])

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f"attachment; filename=approved_order_{order_id}.csv"},
    )


@router.post("/api/orders/{order_id}/export.xlsx")
def export_order_xlsx(
    order_id: int,
    _operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """The same approved snapshot as the CSV, formatted for a human to read.

    The CSV is the raw drop a warehouse system ingests; this is the file an
    operator hands to a distributor owner. Both call ``record_export``, so
    downloading both counts as two exports on the order's audit trail.
    """
    try:
        export_data = database.record_export(order_id, db_path)
    except ValueError as exc:
        logger.warning("Export validation failed for order #%s: %s", order_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _server_error(f"Export failed for order #{order_id}", exc) from exc

    settings = database.get_shop_settings(db_path)
    workbook = build_order_workbook(export_data, settings, order_id)
    stem = f"{filename_stem(export_data, settings, order_id)}-{export_date(export_data)}"

    # A downloaded order sheet is never a cacheable public asset.
    return Response(
        content=workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": attachment_filename(stem, "xlsx", f"order-{order_id}"),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/api/export")
def export_csv(
    export_req: ExportRequest,
    operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """Legacy route retained only for approved-snapshot exports."""
    if export_req.order_id:
        return export_order_csv(export_req.order_id, operator, db_path)
    raise HTTPException(
        status_code=400,
        detail=(
            "Raw-item export is disabled. Export requires an explicitly approved "
            "order snapshot."
        ),
    )


@router.get("/api/settings", response_model=ShopSettings)
def read_shop_settings(
    _operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """The merchant's own identity, as printed on the picking note."""
    return ShopSettings(**database.get_shop_settings(db_path))


@router.put("/api/settings", response_model=ShopSettings)
def write_shop_settings(
    payload: ShopSettingsRequest,
    operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """Update the single shop-settings row.

    Settings are per-deployment: this build serves one merchant, so there is no
    tenant key and none is inferred from the session.
    """
    try:
        logo = sanitize_logo_data_url(payload.logo_data_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    values = payload.model_dump()
    values["logo_data_url"] = logo
    try:
        saved = database.save_shop_settings(values, actor=operator, db_path=db_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _server_error("Saving shop settings failed", exc) from exc

    logger.info("Shop settings updated by %s", operator)
    return ShopSettings(**saved)


@router.get("/api/orders/{order_id}/document")
def download_order_document(
    order_id: int = APIPath(..., ge=1, description="Order ID"),
    inline: bool = False,
    _operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """Download the picking / delivery note for one approved order.

    An internal operations document, not a document with tax standing — see
    :mod:`app.documents`. It is rendered entirely from the approved snapshot,
    so repricing or replacing the catalog cannot change what an old document
    says, and it is byte-stable: downloading the same order twice produces the
    same file.

    Every value in it — product names, the customer's own message — is
    untrusted text, and the template escapes all of it.
    """
    try:
        document = database.load_approved_document(order_id, db_path)
    except LookupError as exc:
        raise HTTPException(
            status_code=404, detail=f"الطلب رقم #{order_id} غير موجود"
        ) from exc
    except ValueError as exc:
        logger.warning("Document refused for order #%s: %s", order_id, exc)
        raise HTTPException(
            status_code=400,
            detail="المستند بيتولّد للطلبات المعتمدة بس. اعتمد الطلب الأول.",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise _server_error(f"Document generation failed for order #{order_id}", exc) from exc

    settings = database.get_shop_settings(db_path)
    context = build_document_context(document, settings)
    html = templates.get_template("document.html").render(**context)

    # A downloaded operations document is never a cacheable public asset.
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

    if inline:
        # Rendered in a tab so the merchant can produce the WhatsApp image in
        # one click. That means same-origin HTML built from customer text, so
        # it is served under a policy that permits only this file's own script
        # and the pinned image library, and nothing else at all. Escaping in
        # the template is still the primary defence; this is the second one.
        # These are response headers, so the document bytes are unchanged.
        headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline' https://cdnjs.cloudflare.com; "
            "connect-src 'none'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'; object-src 'none'"
        )
        headers["Content-Disposition"] = "inline"
    else:
        headers["Content-Disposition"] = attachment_filename(
            f"order-{order_id}-{settings.get('shop_name') or ''}",
            "html",
            f"order-{order_id}",
        )

    return Response(content=html, media_type="text/html; charset=utf-8", headers=headers)


@router.get("/api/orders/{order_id}/audit", response_model=list[AuditEventResponse])
def get_audit_log(
    order_id: int,
    _operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    order_data = database.get_order_by_id(order_id, db_path)
    if not order_data:
        raise HTTPException(status_code=404, detail=f"الطلب رقم #{order_id} غير موجود")
    return [
        AuditEventResponse(
            id=ev["id"],
            order_id=ev["order_id"],
            event_type=ev["event_type"],
            details=ev.get("details"),
            actor=ev.get("actor"),
            action_id=ev.get("action_id"),
            timestamp=ev["timestamp"],
        )
        for ev in order_data.get("audit_events", [])
    ]


@router.get("/api/catalog")
def get_catalog(request: Request, _operator: str = Depends(require_operator)):
    return [
        {
            "product_id": p.product_id,
            "product_name": p.product_name,
            "aliases": p.aliases,
            "unit": p.unit,
            "price": p.price,
        }
        for p in _catalog_state(request).products
    ]


@router.get("/api/catalog/info")
def catalog_info(
    request: Request,
    _operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    return {
        "product_count": len(_catalog_state(request)),
        "version": database.current_catalog_version(db_path),
    }


@router.get("/api/catalog/search")
def search_catalog(
    request: Request,
    q: str = "",
    limit: int = 15,
    _operator: str = Depends(require_operator),
):
    """Server-side catalog lookup for the review combobox.

    The review page used to embed the entire catalog in a ``<select>`` for every
    order line. At 5,000 SKUs and 20 lines that was 100,000 DOM nodes and a
    multi-second freeze on each render. The browser now holds nothing but the
    shortlist for each line and asks here when the operator types.
    """
    query = (q or "").strip()
    limit = max(1, min(int(limit), 50))
    state = _catalog_state(request)
    if not query:
        return []

    upper = query.upper()
    by_code = [
        p for p in state.products if p.product_id.upper().startswith(upper)
    ][:limit]

    results = [
        {
            "product_id": p.product_id,
            "product_name": p.product_name,
            "unit": p.unit,
            "price": p.price,
            "score": 1.0,
        }
        for p in by_code
    ]
    seen = {p["product_id"] for p in results}

    for candidate in state.matcher.find_candidates(query, top_k=limit, min_score=0.2):
        if candidate.product_id in seen:
            continue
        results.append(
            {
                "product_id": candidate.product_id,
                "product_name": candidate.product_name,
                "unit": candidate.unit,
                "price": candidate.price,
                "score": candidate.score,
            }
        )
        if len(results) >= limit:
            break
    return results


@router.get("/api/catalog/template")
def catalog_template(_operator: str = Depends(require_operator)):
    """Download a ready-to-fill catalog template."""
    return Response(
        content=catalog_template_csv(),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": "attachment; filename=catalog_template.csv"},
    )


@router.post("/api/catalog/upload", response_model=CatalogUploadResult)
def upload_catalog(
    request: Request,
    file: Annotated[UploadFile, File()],
    operator: str = Depends(require_operator),
    db_path: str = Depends(_request_db_path),
):
    """Replace the working catalog from an uploaded CSV or XLSX file.

    This is the onboarding path for a real merchant: they export their product
    list from whatever system they already use and upload it as-is.
    """
    raw = file.file.read(MAX_CATALOG_UPLOAD_BYTES + 1)
    if len(raw) > MAX_CATALOG_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                "الملف أكبر من الحد المسموح "
                f"({MAX_CATALOG_UPLOAD_BYTES // (1024 * 1024)} ميجا)."
            ),
        )

    try:
        parsed = parse_catalog_bytes(raw, file.filename or "catalog.csv")
    except CatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _server_error("Catalog parsing failed", exc) from exc

    try:
        version = database.replace_catalog(
            [p.model_dump() for p in parsed.products],
            db_path,
            source_name=file.filename,
            actor=operator,
        )
        _refresh_catalog(request.app)
    except Exception as exc:  # noqa: BLE001
        raise _server_error("Catalog installation failed", exc) from exc

    logger.info(
        "Catalog replaced by %s: %s products from %s", operator, parsed.count, file.filename
    )
    return CatalogUploadResult(
        product_count=version["product_count"],
        version_id=version["version_id"],
        content_sha256=version["content_sha256"],
        installed_at=version["installed_at"],
        source_name=version["source_name"],
        detected_encoding=parsed.detected_encoding,
        source_format=parsed.source_format,
        warnings=parsed.warnings,
    )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(db_path: database.DatabasePath | None = None) -> FastAPI:
    """Build an application instance bound to exactly one database."""
    auth_config = load_auth_config()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        global extractor
        database.initialize_runtime_database(application.state.database_path)
        _seed_catalog_if_empty(application.state.database_path)
        _refresh_catalog(application)

        if extractor is None:
            try:
                extractor = GeminiExtractor()
                logger.info("Gemini extractor initialised")
            except ValueError as exc:
                logger.warning("Gemini extractor not configured: %s", exc)
            except Exception as exc:  # noqa: BLE001 - never let this kill startup
                logger.error("Gemini extractor unavailable: %s", exc)

        announce(auth_config)
        yield

    # /docs, /redoc and /openapi.json are unauthenticated by design in FastAPI.
    # They hand an anonymous caller the full route map and request schemas, so
    # they are off unless a developer explicitly asks for them.
    expose_docs = (os.getenv("EXPOSE_API_DOCS", "false") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    application = FastAPI(
        title=APP_TITLE,
        description=APP_DESCRIPTION,
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url="/docs" if expose_docs else None,
        redoc_url="/redoc" if expose_docs else None,
        openapi_url="/openapi.json" if expose_docs else None,
    )
    application.state.database_path = database.resolve_db_path(db_path)
    application.state.auth_config = auth_config
    application.state.catalog_state = CatalogState()
    application.state.login_throttle = LoginThrottle()

    @application.middleware("http")
    async def _reject_cross_site_writes(request: Request, call_next):
        origin = csrf_violation(request)
        if origin is not None:
            logger.warning(
                "Rejected cross-site %s %s from origin %s",
                request.method, request.url.path, origin,
            )
            response = JSONResponse(
                {"detail": "طلب من موقع خارجي مرفوض."},
                status_code=status.HTTP_403_FORBIDDEN,
            )
        else:
            response = await call_next(request)

        # Sensitive operational data must not be cached or embedded. The CSP
        # permits the app's existing inline UI and Google-hosted Cairo font,
        # while closing every other origin by default. Route-specific policies
        # (the downloadable document) remain stricter via setdefault.
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; "
            "img-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000"
            )
        return response

    application.add_middleware(
        SessionMiddleware,
        secret_key=auth_config.session_secret,
        session_cookie="wop_session",
        max_age=auth_config.session_max_age,
        same_site="lax",
        https_only=os.getenv("SESSION_HTTPS_ONLY", "false").strip().lower()
        in {"1", "true", "yes", "on"},
    )

    application.include_router(public_router)
    application.include_router(router)
    return application


app = create_app(None)
