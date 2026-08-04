"""Pydantic models for structured order data."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class CatalogProduct(BaseModel):
    """A product in the catalog."""
    product_id: str
    product_name: str
    aliases: list[str] = Field(default_factory=list)
    unit: str
    price: float


class ExtractedItem(BaseModel):
    """An item extracted from a raw order message by the LLM."""
    raw_text: str = Field(description="Original text segment for this item")
    product_description: str = Field(description="Cleaned product description")
    quantity: float = Field(description="Quantity requested")
    unit: str = Field(description="Unit of measure")


class ExtractionResult(BaseModel):
    """Result of LLM extraction from an order message."""
    items: list[ExtractedItem] = Field(default_factory=list)
    unresolved_text: list[str] = Field(
        default_factory=list,
        description="Parts of the message that could not be parsed"
    )


class MatchCandidate(BaseModel):
    """A candidate product match from the catalog."""
    product_id: str
    product_name: str
    score: float = Field(ge=0.0, le=1.0, description="Match confidence 0-1")
    unit: str
    price: float


class OrderLineResult(BaseModel):
    """Result for a single order line item."""
    id: Optional[int] = None
    raw_text: str = Field(description="Original text for this item")
    extracted_product: str = Field(description="What the system understood")
    extracted_quantity: float
    extracted_unit: str
    matched_product: Optional[CatalogProduct] = None
    recommended_product: Optional[CatalogProduct] = None
    recommendation_decision: Optional[str] = Field(
        default=None,
        description="Advisory model outcome; it is never an approved SKU",
    )
    routing_reason: Optional[str] = None
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Advisory matcher confidence; human review never rewrites it",
    )
    status: str = Field(
        description="needs_review | human_selected | not_found_confirmed | cancelled_by_human"
    )
    reason: Optional[str] = Field(
        default=None,
        description="Explanation when confidence is low"
    )
    alternatives: list[MatchCandidate] = Field(default_factory=list)
    candidates: list[MatchCandidate] = Field(default_factory=list)
    is_manually_corrected: bool = False
    is_human_confirmed: bool = False
    human_decision: Optional[str] = None
    human_selected_sku: Optional[str] = None
    human_actor: Optional[str] = None
    human_confirmed_at: Optional[str] = None
    review_action_id: Optional[str] = None
    # ``matched_product.price`` is always the effective price displayed to the
    # operator. These fields retain the catalog baseline and any per-order
    # override so a human can see exactly what changed before approval.
    catalog_price: Optional[float] = None
    price_override: Optional[float] = None
    price_overridden: bool = False
    is_manual_line: bool = False


class TimeSavedEstimate(BaseModel):
    """Timing facts for an order.

    Before approval only ``actual_system_time_ms`` is a measurement: no time has
    actually been saved yet, so ``saved_seconds`` stays ``None``. After approval
    the full analysis-to-approval cycle is measured and compared against a
    clearly labelled manual-entry baseline.
    """
    actual_system_time_ms: float
    estimated_manual_time_ms: float
    review_cycle_seconds: Optional[float] = Field(
        default=None,
        description="Measured wall-clock from analysis to approval; None until approved",
    )
    saved_seconds: Optional[float] = Field(
        default=None,
        description="Measured saving; None until the order is approved",
    )
    is_measured: bool = Field(
        default=False,
        description="True only once a real end-to-end cycle has been measured",
    )
    disclaimer: str = Field(
        default="خط الأساس اليدوي تقديري (45 ثانية لكل صنف). زمن الدورة الفعلي مقيس."
    )


class CatalogUploadResult(BaseModel):
    """Outcome of replacing the working catalog."""
    product_count: int
    version_id: int
    content_sha256: str
    installed_at: str
    source_name: Optional[str] = None
    detected_encoding: Optional[str] = None
    source_format: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


class OrderSummary(BaseModel):
    """One row in the order-history panel."""
    order_id: int
    status: str
    original_text: str
    created_at: str
    approved_at: Optional[str] = None
    total_items: int = 0
    confirmed_items: int = 0
    export_count: int = 0
    last_exported_at: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_address: Optional[str] = None


class LoginRequest(BaseModel):
    """Operator sign-in."""
    operator: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=512)


class SessionInfo(BaseModel):
    """Who the current session is acting as."""
    operator: Optional[str] = None
    authenticated: bool = False


class ApprovedSnapshotItem(BaseModel):
    """An immutable item in an approved order snapshot."""
    id: Optional[int] = None
    order_id: int
    original_item_id: int
    product_id: str
    product_name: str
    quantity: float
    unit: str
    price: float
    confidence: float
    model_recommendation_id: Optional[str] = None
    model_decision: Optional[str] = None
    human_decision: Optional[str] = None
    human_selected_sku: Optional[str] = None
    human_actor: Optional[str] = None
    human_confirmed_at: Optional[str] = None
    review_action_id: Optional[str] = None
    approval_actor: Optional[str] = None
    approval_action_id: Optional[str] = None
    provenance_verified: bool = False
    is_manually_corrected: bool = False
    is_human_confirmed: bool = False
    approved_at: str
    catalog_price: Optional[float] = None
    price_override: Optional[float] = None
    price_overridden: bool = False
    is_manual_line: bool = False


class OrderResult(BaseModel):
    """Complete result of processing or loading an order."""
    order_id: Optional[int] = None
    status: str = "analyzed"  # 'analyzed' | 'needs_review' | 'approved'
    original_message: str
    items: list[OrderLineResult] = Field(default_factory=list)
    approved_items: list[ApprovedSnapshotItem] = Field(default_factory=list)
    unresolved: list[str] = Field(
        default_factory=list,
        description="Message parts that couldn't be parsed"
    )
    total_confirmed: int = 0
    total_items: int = 0
    export_count: int = 0
    last_exported_at: Optional[str] = None
    time_saved: Optional[TimeSavedEstimate] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_address: Optional[str] = None
    discount: float = 0.0
    subtotal: float = 0.0
    grand_total: float = 0.0


class OrderRequest(BaseModel):
    """Incoming order processing request."""
    message: str = Field(
        min_length=1,
        max_length=20000,
        description=(
            "The raw order message. Bounded because the full text is stored "
            "verbatim; the extractor separately truncates what it sends to the "
            "provider, which does not limit what the database accepts."
        ),
    )


class ItemReviewRequest(BaseModel):
    """One explicit, attributable, idempotent human line-review command."""
    actor: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    final_decision: Literal["SELECT", "NOT_FOUND"]
    selected_sku: Optional[str] = None
    quantity: Optional[float] = Field(default=None, gt=0)
    unit: Optional[str] = None


class ActorActionRequest(BaseModel):
    """A session-bound, idempotent command without additional fields."""

    actor: str = Field(min_length=1, max_length=120)
    action_id: str = Field(min_length=1, max_length=200)


class CustomerDetailsRequest(ActorActionRequest):
    """Set the customer data attached to a mutable order."""

    customer_name: Optional[str] = Field(default=None, max_length=200)
    customer_phone: Optional[str] = Field(default=None, max_length=80)
    customer_address: Optional[str] = Field(default=None, max_length=500)


class ManualOrderItemRequest(ActorActionRequest):
    """A human-created order line, selected from the catalog search."""

    selected_sku: str = Field(min_length=1, max_length=200)
    quantity: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=80)


class PriceOverrideRequest(ActorActionRequest):
    """A pre-approval unit-price override for an already selected line."""

    price: float = Field(ge=0)


class DiscountRequest(ActorActionRequest):
    """A pre-approval absolute order discount."""

    discount: float = Field(ge=0)


class ApprovalRequest(BaseModel):
    """One explicit, attributable, idempotent order-approval command."""
    actor: str = Field(min_length=1)
    action_id: str = Field(min_length=1)


class OrderApproveResponse(BaseModel):
    """Response payload after order approval."""
    order_id: int
    status: str = "approved"
    message: str = "Ready for warehouse"
    approved_at: str
    snapshot: list[ApprovedSnapshotItem] = Field(default_factory=list)


class AuditEventResponse(BaseModel):
    """Audit event details."""
    id: int
    order_id: int
    event_type: str
    details: Optional[str] = None
    actor: Optional[str] = None
    action_id: Optional[str] = None
    timestamp: str


class ExportRequest(BaseModel):
    """Request to export confirmed order items."""
    order_id: Optional[int] = None
    items: Optional[list[dict]] = None


class ShopSettings(BaseModel):
    """Merchant identity printed on the picking / delivery note.

    ``tax_id`` is display text the merchant types themselves. It is printed on
    an internal operations document and makes no claim of tax validity: this
    build is not an ETA-integrated e-invoicing provider and never submits
    anything to the authority.
    """
    shop_name: str = ""
    shop_address: str = ""
    shop_phone: str = ""
    tax_id: str = ""
    document_title: str = "إذن صرف"
    footer_note: str = ""
    logo_data_url: str = ""
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class ShopSettingsRequest(BaseModel):
    """Operator update of the shop identity panel."""
    shop_name: str = Field(min_length=1, max_length=200)
    shop_address: str = Field(default="", max_length=400)
    shop_phone: str = Field(default="", max_length=60)
    tax_id: str = Field(default="", max_length=60)
    document_title: str = Field(default="إذن صرف", min_length=1, max_length=80)
    footer_note: str = Field(default="", max_length=400)
    logo_data_url: str = Field(
        default="",
        # 500 KB of image is ~683 KB of base64 plus the data: prefix. The bound
        # here is a cheap first gate; the decoded bytes are checked exactly.
        max_length=750_000,
        description="data: URL for a PNG or JPEG logo, or empty to remove it",
    )
