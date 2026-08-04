"""The merchant-facing half of the workflow: money, notes, and the document.

Each test here stands for something a merchant could not do in the previous
build — see the total, read what the model failed to understand, or hand
anything at all to a delivery driver.
"""

from __future__ import annotations

import base64
import io
import re
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

import app.main as main_module
from app import database
from app.documents import LEGAL_FOOTER, money
from app.main import create_app
from app.models import ExtractedItem, ExtractionResult
from app.xlsx_export import (
    COLUMNS,
    CONFIDENCE_COLUMN,
    TOTAL_LABEL_COLUMN,
    TOTAL_VALUE_COLUMN,
    build_order_workbook,
)
from tests.helpers import login

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# One catalog, three priced products, one of them carrying an XSS payload in the
# name a merchant would actually see.
XSS_NAME = '<img src=x onerror="alert(1)">لبن'
CATALOG_CSV = (
    "product_id,product_name,unit,price\n"
    "LB001,لبن جهينة 1 لتر,كرتونة,42.50\n"
    "SG002,سكر ناعم 1 كيلو,كيس,31.25\n"
    f'XS003,"{XSS_NAME}",قطعة,10.00\n'
).encode("utf-8")

FREE_CATALOG_CSV = (
    "product_id,product_name,unit,price\n"
    "LB001,لبن جهينة 1 لتر,كرتونة,0\n"
    "SG002,سكر ناعم 1 كيلو,كيس,0\n"
).encode("utf-8")

ORDER_TEXT = "عايز 2 لبن جهينة و 3 سكر ولحمة مفرومة ابعتهم بسرعة الله يكرمك"
UNRESOLVED_NOTE = "ابعتهم بسرعة الله يكرمك"


class ScriptedExtractor:
    """Returns a fixed multi-line order plus the fragments it could not parse."""

    def __init__(self, unresolved=(UNRESOLVED_NOTE,), product_name="لبن جهينة"):
        self.unresolved = list(unresolved)
        self.product_name = product_name

    def extract(self, message: str) -> ExtractionResult:
        return ExtractionResult(
            items=[
                ExtractedItem(
                    raw_text="2 لبن جهينة",
                    product_description=self.product_name,
                    quantity=2.0,
                    unit="كرتونة",
                ),
                ExtractedItem(
                    raw_text="3 سكر",
                    product_description="سكر ناعم",
                    quantity=3.0,
                    unit="كيس",
                ),
                ExtractedItem(
                    raw_text="لحمة مفرومة",
                    product_description="لحمة مفرومة",
                    quantity=1.0,
                    unit="كيلو",
                ),
            ],
            unresolved_text=self.unresolved,
        )


@pytest.fixture
def shop(tmp_path, monkeypatch):
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    monkeypatch.setattr(main_module, "extractor", ScriptedExtractor())
    with TestClient(create_app(db_path)) as client:
        login(client, "reviewer")
        upload_catalog(client, CATALOG_CSV)
        yield client, db_path


def upload_catalog(client: TestClient, payload: bytes):
    response = client.post(
        "/api/catalog/upload",
        files={"file": ("catalog.csv", payload, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response


def analyse(client: TestClient, message: str = ORDER_TEXT):
    response = client.post("/api/process", json={"message": message})
    assert response.status_code == 200, response.text
    return response.json()


def review_line(client: TestClient, order_id, item_id, sku, quantity, unit, tag):
    payload = {
        "actor": "reviewer",
        "action_id": f"review-{order_id}-{tag}",
        "final_decision": "SELECT" if sku else "NOT_FOUND",
        "selected_sku": sku,
        "quantity": quantity,
        "unit": unit,
    }
    if not sku:
        payload = {
            "actor": "reviewer",
            "action_id": f"review-{order_id}-{tag}",
            "final_decision": "NOT_FOUND",
        }
    response = client.post(
        f"/api/orders/{order_id}/items/{item_id}/review", json=payload
    )
    assert response.status_code == 200, response.text
    return response.json()


def approved_order(client: TestClient, message: str = ORDER_TEXT):
    """Two priced lines confirmed, one line confirmed missing, then approved.

    Totals: 2 x 42.50 + 3 x 31.25 = 178.75. The NOT_FOUND line contributes
    nothing, which is the whole point of the third line existing here.
    """
    order = analyse(client, message)
    order_id = order["order_id"]
    review_line(client, order_id, order["items"][0]["id"], "LB001", 2.0, "كرتونة", "a")
    review_line(client, order_id, order["items"][1]["id"], "SG002", 3.0, "كيس", "b")
    review_line(client, order_id, order["items"][2]["id"], None, None, None, "c")
    approval = client.post(
        f"/api/orders/{order_id}/approve",
        json={"actor": "reviewer", "action_id": f"approve-{order_id}"},
    )
    assert approval.status_code == 200, approval.text
    return order_id


EXPECTED_TOTAL = 2 * 42.50 + 3 * 31.25  # 178.75


# ---------------------------------------------------------------------------
# A1 — the unresolved fragment must reach a human
# ---------------------------------------------------------------------------


def test_unresolved_text_reaches_the_api_response(shop):
    client, _ = shop
    order = analyse(client)
    assert order["unresolved"] == [UNRESOLVED_NOTE], (
        "the extractor's unresolved fragments were dropped between the audit "
        "trail and the operator"
    )

    reloaded = client.get(f"/api/orders/{order['order_id']}").json()
    assert reloaded["unresolved"] == [UNRESOLVED_NOTE]


def test_unresolved_text_survives_review_and_approval(shop):
    client, _ = shop
    order_id = approved_order(client)
    assert client.get(f"/api/orders/{order_id}").json()["unresolved"] == [UNRESOLVED_NOTE]


def test_review_page_renders_the_unresolved_banner(shop):
    client, _ = shop
    page = client.get("/").text
    assert "unresolvedBanner" in page
    assert "جزء من الرسالة لم يُفهَم" in page, "the operator is never warned on screen"


def test_an_order_with_nothing_unresolved_reports_an_empty_list(shop, monkeypatch):
    client, _ = shop
    monkeypatch.setattr(main_module, "extractor", ScriptedExtractor(unresolved=()))
    assert analyse(client)["unresolved"] == []


# ---------------------------------------------------------------------------
# A2 — prices and totals
# ---------------------------------------------------------------------------


def test_prices_are_present_on_every_priced_line(shop):
    client, _ = shop
    order = analyse(client)
    prices = {
        item["recommended_product"]["product_id"]: item["recommended_product"]["price"]
        for item in order["items"]
        if item["recommended_product"]
    }
    assert prices["LB001"] == 42.50
    assert prices["SG002"] == 31.25


def test_approved_totals_exclude_not_found_lines(shop):
    client, _ = shop
    order_id = approved_order(client)
    snapshot = client.get(f"/api/orders/{order_id}").json()["approved_items"]

    assert len(snapshot) == 2, "the NOT_FOUND line must not enter the snapshot"
    total = sum(row["price"] * row["quantity"] for row in snapshot)
    assert total == pytest.approx(EXPECTED_TOTAL)
    assert money(total) == "178.75"


def test_review_page_carries_the_price_columns_and_running_total(shop):
    client, _ = shop
    page = client.get("/").text
    assert "سعر الوحدة" in page and "الإجمالي" in page
    assert "إجمالي الطلب:" in page
    assert "no-prices" in page, "a priceless catalog must be able to hide the columns"
    assert "refreshTotals" in page, "the total must recompute as quantities change"


def test_a_catalog_without_prices_reports_zeros_for_the_ui_to_hide(shop):
    client, _ = shop
    upload_catalog(client, FREE_CATALOG_CSV)
    order = analyse(client)
    priced = [
        item["recommended_product"]["price"]
        for item in order["items"]
        if item["recommended_product"]
    ]
    assert priced and all(price == 0 for price in priced)


# ---------------------------------------------------------------------------
# A3 — the CSV
# ---------------------------------------------------------------------------


def test_csv_states_the_message_once_and_carries_a_totals_row(shop):
    client, _ = shop
    order_id = approved_order(client)
    response = client.post(f"/api/orders/{order_id}/export")
    assert response.status_code == 200, response.text
    text = response.content.decode("utf-8-sig")

    assert text.count(ORDER_TEXT) == 1, (
        "the order message was repeated on every line, making the export unreadable"
    )
    assert "رسالة العميل" in text
    assert "النص الأصلي" not in text, "the per-row message column must be gone"

    assert "الإجمالي النهائي" in text
    assert "178.75" in text, text
    assert "Model confidence (advisory)" in text  # existing contract preserved


def test_csv_totals_row_matches_the_snapshot(shop):
    client, _ = shop
    order_id = approved_order(client)
    text = client.post(f"/api/orders/{order_id}/export").content.decode("utf-8-sig")
    totals_line = [line for line in text.splitlines() if "الإجمالي النهائي" in line]
    assert len(totals_line) == 1
    assert str(round(EXPECTED_TOTAL, 2)) in totals_line[0]


def test_csv_still_sanitises_formula_injection(shop, monkeypatch):
    client, _ = shop
    monkeypatch.setattr(
        main_module, "extractor", ScriptedExtractor(product_name="=cmd|' /C calc'!A0")
    )
    order_id = approved_order(client)
    text = client.post(f"/api/orders/{order_id}/export").content.decode("utf-8-sig")
    assert "\n=cmd" not in text and ",=cmd" not in text


# ---------------------------------------------------------------------------
# A4 — the Excel export
# ---------------------------------------------------------------------------

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSX_HEADER_ROW = 9


def load_export_workbook(client: TestClient, order_id):
    response = client.post(f"/api/orders/{order_id}/export.xlsx")
    assert response.status_code == 200, response.text
    return response, load_workbook(io.BytesIO(response.content))


def data_rows(ws):
    """The table body: every row under the header that carries a line number."""
    return [
        row
        for row in ws.iter_rows(min_row=XLSX_HEADER_ROW + 1, max_col=len(COLUMNS))
        if isinstance(row[0].value, int)
    ]


def test_xlsx_export_returns_a_workbook_with_the_snapshot_rows(shop):
    client, _ = shop
    order_id = approved_order(client)
    response, workbook = load_export_workbook(client, order_id)

    assert response.headers["content-type"] == XLSX_MEDIA_TYPE
    assert ".xlsx" in response.headers["content-disposition"]
    assert len(data_rows(workbook.active)) == 2, (
        "the sheet must carry exactly the approved snapshot — the NOT_FOUND "
        "line is not part of it and must never appear as a priced row"
    )


def test_xlsx_header_labels_match_the_csv_contract(shop):
    client, _ = shop
    order_id = approved_order(client)
    _, workbook = load_export_workbook(client, order_id)
    headers = [cell.value for cell in workbook.active[XLSX_HEADER_ROW]]

    for label in (
        "اسم المنتج",
        "الكمية",
        "السعر الفردي",
        "الإجمالي",
        "Model confidence (advisory)",
    ):
        assert label in headers, f"{label} missing from the xlsx header row: {headers}"

    assert "Model confidence (advisory)" in headers, (
        "the confidence column was relabelled; every snapshot line was confirmed "
        "by a human, so the column must not read as an approval signal"
    )


def test_xlsx_sheet_is_right_to_left(shop):
    client, _ = shop
    order_id = approved_order(client)
    _, workbook = load_export_workbook(client, order_id)

    assert workbook.active.sheet_view.rightToLeft is True, (
        "the sheet opened left-to-right, so an Arabic order reads backwards"
    )
    assert workbook.active.freeze_panes == f"A{XLSX_HEADER_ROW + 1}"


def test_xlsx_title_cell_carries_the_shop_name(shop):
    """The letterhead is the merchant; the header block is the buyer.

    The customer is attached before approval on purpose — an approved order is
    locked for editing, so this is the only order in which the two names can
    both be set, and the sheet must not confuse one for the other.
    """
    client, _ = shop
    set_shop(client)
    order = analyse(client)
    order_id = order["order_id"]
    review_line(client, order_id, order["items"][0]["id"], "LB001", 2.0, "كرتونة", "a")
    review_line(client, order_id, order["items"][1]["id"], "SG002", 3.0, "كيس", "b")
    review_line(client, order_id, order["items"][2]["id"], None, None, None, "c")
    customer = client.put(
        f"/api/orders/{order_id}/customer",
        json={
            "actor": "reviewer",
            "action_id": f"customer-{order_id}",
            "customer_name": "محل الأمانة",
            "customer_phone": "01111111111",
        },
    )
    assert customer.status_code == 200, customer.text
    approval = client.post(
        f"/api/orders/{order_id}/approve",
        json={"actor": "reviewer", "action_id": f"approve-{order_id}"},
    )
    assert approval.status_code == 200, approval.text

    _, workbook = load_export_workbook(client, order_id)
    ws = workbook.active

    assert ws["A1"].value == "سوبر ماركت النور", (
        "the title cell must carry the merchant's own name, not the buyer's"
    )
    labels = {
        ws.cell(row=row, column=1).value: ws.cell(row=row, column=2).value
        for row in range(2, 8)
    }
    assert labels["اسم العميل"] == "محل الأمانة"
    assert labels["هاتف العميل"] == "01111111111"
    assert labels["رسالة العميل"] == ORDER_TEXT


def test_xlsx_sanitises_formula_injection(shop, monkeypatch):
    client, _ = shop
    monkeypatch.setattr(
        main_module, "extractor", ScriptedExtractor(product_name="=cmd|' /C calc'!A0")
    )
    order_id = approved_order(client)
    _, workbook = load_export_workbook(client, order_id)

    for row in workbook.active.iter_rows():
        for cell in row:
            assert not (isinstance(cell.value, str) and cell.value.startswith("=")), (
                "openpyxl does not escape formulas, so a product name taken from "
                f"customer text would execute when the merchant opens it: {cell.value!r}"
            )


def test_xlsx_totals_match_the_snapshot(shop):
    client, _ = shop
    order_id = approved_order(client)
    _, workbook = load_export_workbook(client, order_id)
    ws = workbook.active

    totals = {
        ws.cell(row=row, column=TOTAL_LABEL_COLUMN).value: ws.cell(
            row=row, column=TOTAL_VALUE_COLUMN
        ).value
        for row in range(XLSX_HEADER_ROW + 1, ws.max_row + 1)
    }
    assert totals["الإجمالي النهائي"] == pytest.approx(EXPECTED_TOTAL), (
        "the total must be a real number Excel can sum, not a pre-formatted string"
    )
    assert totals["الإجمالي قبل الخصم"] == pytest.approx(EXPECTED_TOTAL)


def test_xlsx_confidence_colours_follow_the_matcher_thresholds():
    """The colour scale is the matcher's, and it is advisory only.

    Built from a synthetic snapshot rather than the fixture because a real
    approved order happens to score high on every line, which would leave the
    amber and red branches unexercised.
    """
    snapshot = [
        {"product_id": "A1", "product_name": "عالي", "quantity": 1, "unit": "قطعة",
         "price": 10.0, "confidence": 0.92, "is_human_confirmed": 1},
        {"product_id": "B2", "product_name": "متوسط", "quantity": 1, "unit": "قطعة",
         "price": 10.0, "confidence": 0.70, "is_human_confirmed": 1},
        {"product_id": "C3", "product_name": "منخفض", "quantity": 1, "unit": "قطعة",
         "price": 10.0, "confidence": 0.30, "is_human_confirmed": 1},
    ]
    export_data = {
        "customer_name": "", "customer_phone": "", "customer_address": "",
        "original_text": ORDER_TEXT, "last_exported_at": "2026-08-04T10:00:00",
        "discount": 0.0, "grand_total": 30.0, "snapshot": snapshot,
    }
    workbook = load_workbook(
        io.BytesIO(build_order_workbook(export_data, {"shop_name": "متجر"}, 7))
    )
    ws = workbook.active

    fills = [
        ws.cell(row=XLSX_HEADER_ROW + offset, column=CONFIDENCE_COLUMN).fill.start_color.rgb
        for offset in (1, 2, 3)
    ]
    assert len(set(fills)) == 3, f"the three confidence tiers shared a colour: {fills}"
    for offset in (1, 2, 3):
        cell = ws.cell(row=XLSX_HEADER_ROW + offset, column=CONFIDENCE_COLUMN)
        assert isinstance(cell.value, float), (
            "confidence must stay a real number so the column sorts and filters"
        )
        assert cell.number_format == "0%"

    legend = " ".join(
        str(row[0].value) for row in ws.iter_rows(min_col=1, max_col=1) if row[0].value
    )
    assert "استرشادي" in legend and "مش إن السطر مش معتمد" in legend, (
        "without the legend a red cell reads as 'not approved', but every line "
        "in an approved snapshot was confirmed by a named human"
    )


# ---------------------------------------------------------------------------
# Part B — shop settings
# ---------------------------------------------------------------------------

PNG_1PX = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    + b"\x1f\x15\xc4\x89"
    + b"\x00\x00\x00\nIDAT" + zlib.compress(b"\x00\xff\xff\xff\xff")[:10]
    + b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()


def test_settings_default_to_a_picking_note_title(shop):
    client, _ = shop
    settings = client.get("/api/settings").json()
    assert settings["document_title"] == "إذن صرف"
    assert settings["shop_name"] == ""


def test_settings_round_trip_including_the_logo(shop):
    client, _ = shop
    response = client.put(
        "/api/settings",
        json={
            "shop_name": "سوبر ماركت النور",
            "shop_address": "٣ ش الجمهورية، طنطا",
            "shop_phone": "01000000000",
            "tax_id": "123-456-789",
            "document_title": "إذن صرف",
            "footer_note": "البضاعة المباعة لا تُرد ولا تُستبدل",
            "logo_data_url": PNG_DATA_URL,
        },
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["shop_name"] == "سوبر ماركت النور"
    assert saved["logo_data_url"].startswith("data:image/png;base64,")
    assert saved["updated_by"] == "reviewer"
    assert client.get("/api/settings").json()["tax_id"] == "123-456-789"


def test_settings_require_a_shop_name(shop):
    client, _ = shop
    response = client.put(
        "/api/settings", json={"shop_name": "   ", "document_title": "إذن صرف"}
    )
    assert response.status_code in (400, 422)


@pytest.mark.parametrize(
    "payload",
    [
        "data:image/svg+xml;base64,"
        + base64.b64encode(b"<svg onload='alert(1)'></svg>").decode(),
        "data:image/png;base64," + base64.b64encode(b"<svg onload='alert(1)'>").decode(),
        "javascript:alert(1)",
        "data:image/png;base64,!!!!not-base64!!!!",
    ],
)
def test_a_logo_that_is_not_a_real_png_or_jpeg_is_refused(shop, payload):
    client, _ = shop
    response = client.put(
        "/api/settings",
        json={
            "shop_name": "محل",
            "document_title": "إذن صرف",
            "logo_data_url": payload,
        },
    )
    assert response.status_code == 400, response.text
    assert client.get("/api/settings").json()["logo_data_url"] == ""


def test_an_oversized_logo_is_refused(shop):
    client, _ = shop
    fat = PNG_1PX + b"\x00" * (500 * 1024)
    response = client.put(
        "/api/settings",
        json={
            "shop_name": "محل",
            "document_title": "إذن صرف",
            "logo_data_url": "data:image/png;base64," + base64.b64encode(fat).decode(),
        },
    )
    assert response.status_code in (400, 422), response.text


def test_settings_endpoints_require_an_operator(shop):
    client, _ = shop
    client.post("/api/logout")
    assert client.get("/api/settings").status_code == 401
    assert client.put(
        "/api/settings", json={"shop_name": "x", "document_title": "y"}
    ).status_code == 401


# ---------------------------------------------------------------------------
# Part C — the document
# ---------------------------------------------------------------------------


def set_shop(client: TestClient, **overrides):
    body = {
        "shop_name": "سوبر ماركت النور",
        "shop_address": "٣ ش الجمهورية، طنطا",
        "shop_phone": "01000000000",
        "tax_id": "",
        "document_title": "إذن صرف",
        "footer_note": "",
        "logo_data_url": "",
    }
    body.update(overrides)
    response = client.put("/api/settings", json=body)
    assert response.status_code == 200, response.text
    return response


def test_document_requires_an_approved_order(shop):
    client, _ = shop
    order = analyse(client)
    response = client.get(f"/api/orders/{order['order_id']}/document")
    assert response.status_code == 400, response.text
    assert "معتمد" in response.json()["detail"]


def test_document_for_a_missing_order_is_404(shop):
    client, _ = shop
    assert client.get("/api/orders/98765/document").status_code == 404


def test_document_requires_an_operator(shop):
    client, _ = shop
    order_id = approved_order(client)
    client.post("/api/logout")
    assert client.get(f"/api/orders/{order_id}/document").status_code == 401


def test_document_downloads_as_an_attachment_named_for_the_shop(shop):
    client, _ = shop
    set_shop(client)
    order_id = approved_order(client)
    response = client.get(f"/api/orders/{order_id}/document")
    assert response.status_code == 200, response.text

    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f"order-{order_id}" in disposition
    assert "\n" not in disposition and "\r" not in disposition


def test_document_carries_the_totals_the_screen_shows(shop):
    client, _ = shop
    set_shop(client)
    order_id = approved_order(client)
    html = client.get(f"/api/orders/{order_id}/document").text

    assert "178.75" in html
    assert "الإجمالي النهائي" in html
    assert "85.00" in html, "2 x 42.50 line total"
    assert "93.75" in html, "3 x 31.25 line total"


def test_document_does_not_disclose_not_found_lines(shop):
    client, _ = shop
    set_shop(client)
    order_id = approved_order(client)
    html = client.get(f"/api/orders/{order_id}/document").text

    assert "أصناف غير متوفرة" not in html
    assert "لحمة مفرومة" not in html


def test_document_does_not_disclose_customer_message_notes(shop):
    client, _ = shop
    set_shop(client)
    order_id = approved_order(client)
    html = client.get(f"/api/orders/{order_id}/document").text

    assert "ملاحظات من رسالة العميل" not in html
    assert UNRESOLVED_NOTE not in html
    assert "أمين المخزن" in html and "المستلم" in html


def test_document_carries_the_legal_footer_and_shop_identity(shop):
    client, _ = shop
    set_shop(client, tax_id="100-200-300", footer_note="لا يُرد ولا يُستبدل")
    order_id = approved_order(client)
    html = client.get(f"/api/orders/{order_id}/document").text

    assert LEGAL_FOOTER in html
    assert "سوبر ماركت النور" in html
    assert "الكود الضريبي: 100-200-300" in html
    assert "لا يُرد ولا يُستبدل" in html


def test_document_embeds_the_logo_and_fetches_nothing_to_render(shop):
    client, _ = shop
    set_shop(client, logo_data_url=PNG_DATA_URL)
    order_id = approved_order(client)
    html = client.get(f"/api/orders/{order_id}/document").text

    assert "data:image/png;base64," in html

    # Nothing may be fetched while the page renders. The image library is the
    # single external URL in the file and it is only ever loaded from a click
    # handler, so a phone with no connection still shows the whole document.
    for tag in re.findall(r"<(?:link|img|script|iframe)\b[^>]*>", html, re.IGNORECASE):
        for url in re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', tag, re.IGNORECASE):
            assert url.startswith("data:"), f"render-time external request: {tag}"

    assert 'tag.src = H2C_URL' in html, "the library must be injected, not <script src>"
    assert "integrity" in html, "the CDN library must be pinned by SRI"


def test_document_is_byte_identical_after_the_catalog_is_repriced(shop):
    client, _ = shop
    set_shop(client)
    order_id = approved_order(client)
    before = client.get(f"/api/orders/{order_id}/document").content

    repriced = (
        "product_id,product_name,unit,price\n"
        "LB001,لبن مختلف تماماً,كرتونة,999.99\n"
        "SG002,سكر مختلف,كيس,888.88\n"
    ).encode("utf-8")
    upload_catalog(client, repriced)

    after = client.get(f"/api/orders/{order_id}/document").content
    assert after == before, "the document must be frozen at approval, not live"
    assert "999.99".encode() not in after
    assert "لبن مختلف تماماً".encode() not in after
    assert "178.75".encode() in after


def test_downloading_the_document_twice_returns_the_same_bytes(shop):
    client, _ = shop
    set_shop(client)
    order_id = approved_order(client)
    first = client.get(f"/api/orders/{order_id}/document").content
    second = client.get(f"/api/orders/{order_id}/document").content
    assert first == second


def test_document_does_not_count_as_an_export(shop):
    client, _ = shop
    set_shop(client)
    order_id = approved_order(client)
    client.get(f"/api/orders/{order_id}/document")
    assert client.get(f"/api/orders/{order_id}").json()["export_count"] == 0


def test_inline_document_is_served_under_a_restrictive_policy(shop):
    client, _ = shop
    set_shop(client)
    order_id = approved_order(client)
    response = client.get(f"/api/orders/{order_id}/document?inline=1")
    assert response.status_code == 200
    assert response.headers["content-disposition"] == "inline"

    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "object-src 'none'" in policy
    assert "base-uri 'none'" in policy

    # The policy is a header, so it must not change the document bytes.
    assert response.content == client.get(f"/api/orders/{order_id}/document").content


# ---------------------------------------------------------------------------
# Escaping — every value in the document is customer-controlled
# ---------------------------------------------------------------------------

# An escaped payload still contains the *letters* "onerror" as page text, which
# is inert and expected. What must never exist is a real tag carrying it, so the
# check looks at parsed markup rather than at a substring of the whole file.
_TAGS = re.compile(r"<[a-zA-Z][^>]*>")
_HANDLERS = re.compile(r"\bon[a-z]+\s*=", re.IGNORECASE)


def assert_no_handler_survives_as_markup(html: str):
    for tag in _TAGS.findall(html):
        assert not _HANDLERS.search(tag), f"an event handler reached the markup: {tag}"


def test_an_xss_payload_in_a_product_name_is_inert_in_the_document(shop, monkeypatch):
    client, _ = shop
    set_shop(client)
    monkeypatch.setattr(main_module, "extractor", ScriptedExtractor(product_name=XSS_NAME))

    order = analyse(client)
    order_id = order["order_id"]
    review_line(client, order_id, order["items"][0]["id"], "XS003", 2.0, "قطعة", "a")
    review_line(client, order_id, order["items"][1]["id"], "SG002", 3.0, "كيس", "b")
    review_line(client, order_id, order["items"][2]["id"], None, None, None, "c")
    assert client.post(
        f"/api/orders/{order_id}/approve",
        json={"actor": "reviewer", "action_id": f"approve-{order_id}"},
    ).status_code == 200

    html = client.get(f"/api/orders/{order_id}/document").text
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html, "the payload must appear escaped, not dropped"
    assert_no_handler_survives_as_markup(html)


def test_order_text_is_not_included_in_the_document(shop, monkeypatch):
    client, _ = shop
    set_shop(client)
    payload = '</table><script>alert(document.cookie)</script>'
    monkeypatch.setattr(main_module, "extractor", ScriptedExtractor(unresolved=(payload,)))

    order_id = approved_order(client, message=f"عايز حاجات {payload}")
    html = client.get(f"/api/orders/{order_id}/document").text

    assert "<script>alert(document.cookie)</script>" not in html
    assert "&lt;script&gt;" not in html
    assert "&lt;/table&gt;" not in html


def test_shop_identity_is_escaped_in_the_document(shop):
    client, _ = shop
    set_shop(
        client,
        shop_name='محل"><script>alert(1)</script>',
        footer_note="<b>bold</b>",
    )
    order_id = approved_order(client)
    html = client.get(f"/api/orders/{order_id}/document").text

    assert "<script>alert(1)</script>" not in html
    assert "<b>bold</b>" not in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html


# ---------------------------------------------------------------------------
# The legal-wording ban
# ---------------------------------------------------------------------------

BANNED_WORDING = ("فاتورة ضريبية", "tax invoice", "e-invoice", "فاتورة إلكترونية")

SCANNED_FILES = sorted(
    [p for p in (PROJECT_ROOT / "app").rglob("*.py")]
    + [p for p in (PROJECT_ROOT / "app" / "templates").rglob("*.html")]
)


@pytest.mark.parametrize("path", SCANNED_FILES, ids=lambda p: p.name)
def test_no_source_file_claims_tax_standing(path):
    """The product is not an ETA-integrated e-invoicing provider and must not read as one.

    The one sanctioned occurrence is :data:`LEGAL_FOOTER` itself — the sentence
    printed on every document *denying* that it is a tax document. It is removed
    before scanning so that the disclaimer cannot be mistaken for a claim, and
    so that deleting the disclaimer cannot silently pass this test.
    """
    text = path.read_text(encoding="utf-8")
    remainder = text.replace(LEGAL_FOOTER, "")

    for phrase in BANNED_WORDING:
        assert phrase not in remainder, (
            f"{path.name} claims tax standing with {phrase!r}; this build has no "
            "e-signature and submits nothing to the authority"
        )


def test_the_disclaimer_itself_is_still_present_and_unaltered():
    document = (PROJECT_ROOT / "app" / "templates" / "document.html").read_text("utf-8")
    assert "{{ legal_footer }}" in document
    assert "ليس فاتورة ضريبية" in LEGAL_FOOTER
    assert "لم يُرسل لمنظومة الفاتورة الإلكترونية" in LEGAL_FOOTER


def test_the_document_is_titled_as_an_internal_operations_document(shop):
    client, _ = shop
    set_shop(client)
    order_id = approved_order(client)
    html = client.get(f"/api/orders/{order_id}/document").text
    assert "إذن صرف" in html
    for phrase in ("tax invoice", "e-invoice"):
        assert phrase not in html.lower()
