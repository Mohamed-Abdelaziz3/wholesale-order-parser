"""Product catalog loading, parsing, and validation.

The catalog is the highest-friction part of onboarding a real merchant: they
hand over a file exported from Excel, in an unpredictable encoding, with Arabic
or English headers, and possibly no alias column at all. This module absorbs
that mess so the rest of the system can assume a clean list.
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .models import CatalogProduct

DEFAULT_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "catalog.csv",
)

MAX_CATALOG_ROWS = 50_000
MAX_ALIASES_PER_PRODUCT = 40

# Encodings an Egyptian merchant's Excel export realistically arrives in.
# Excel's "Unicode Text (*.txt)" export is UTF-16LE with a BOM; Arabic Windows
# Excel writes cp1256. Both are what a merchant actually sends.
CANDIDATE_ENCODINGS = ("utf-8-sig", "utf-8", "utf-16", "cp1256", "latin-1")

# Accepted header spellings, normalised to lowercase/stripped.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "product_id": (
        "product_id", "productid", "id", "sku", "code", "product code",
        "كود", "الكود", "كود المنتج", "كود الصنف", "رقم الصنف", "رقم المنتج",
    ),
    "product_name": (
        "product_name", "productname", "name", "product", "description",
        "اسم المنتج", "الاسم", "المنتج", "الصنف", "اسم الصنف", "البيان",
    ),
    "aliases": (
        "aliases", "alias", "synonyms", "keywords", "other names",
        "مرادفات", "المرادفات", "اسماء بديلة", "أسماء بديلة", "اسماء اخرى", "أسماء أخرى",
    ),
    "unit": (
        "unit", "uom", "unit of measure", "packaging",
        "الوحدة", "الوحده", "وحدة", "وحده", "وحدة القياس", "التعبئة",
    ),
    "price": (
        "price", "unit price", "cost", "amount",
        "السعر", "سعر", "سعر الوحدة", "سعر البيع", "الثمن",
    ),
}

ALIAS_SEPARATORS = re.compile(r"[|;،]")
_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789.")


class CatalogError(ValueError):
    """Raised when an uploaded catalog cannot be accepted."""


@dataclass
class CatalogParseResult:
    """Outcome of parsing an uploaded catalog file."""

    products: list[CatalogProduct] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    detected_encoding: str = ""
    source_format: str = "csv"

    @property
    def count(self) -> int:
        return len(self.products)


def _normalise_header(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("﻿", "").strip()).lower()


def _resolve_columns(headers: Sequence[str]) -> dict[str, str]:
    """Map canonical field names to the actual header strings present."""
    normalised = {_normalise_header(h): h for h in headers if h is not None}
    resolved: dict[str, str] = {}
    for canonical, options in COLUMN_ALIASES.items():
        for option in options:
            if option in normalised:
                resolved[canonical] = normalised[option]
                break
    return resolved


def _has_arabic(text: str) -> bool:
    return any("؀" <= ch <= "ۿ" for ch in text)


def _decode(raw: bytes) -> tuple[str, str]:
    """Decode bytes, returning ``(text, encoding_used)``."""
    for encoding in CANDIDATE_ENCODINGS:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        # cp1256 will "successfully" decode UTF-8 Arabic into mojibake, so it is
        # only accepted when the result actually contains Arabic. latin-1 is the
        # last-resort fallback and is deliberately NOT gated that way: it can
        # never produce an Arabic codepoint, so gating it made the branch dead
        # code and left accented Latin catalogs unreadable.
        if encoding == "cp1256" and not _has_arabic(text):
            continue
        if encoding == "utf-16" and "\x00" in text:
            continue
        return text, encoding
    return raw.decode("utf-8", errors="replace"), "utf-8 (with replacements)"


def _clean_price(value: object) -> float:
    if value is None:
        return 0.0
    text = str(value).translate(_ARABIC_INDIC).strip()
    if not text:
        return 0.0
    text = re.sub(r"[^\d.\-]", "", text.replace(",", ""))
    if not text or text in {"-", ".", "-."}:
        return 0.0
    try:
        return round(abs(float(text)), 4)
    except ValueError:
        return 0.0


def _split_aliases(value: object) -> list[str]:
    if not value:
        return []
    parts = ALIAS_SEPARATORS.split(str(value))
    seen: list[str] = []
    for part in parts:
        cleaned = re.sub(r"\s+", " ", part).strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen[:MAX_ALIASES_PER_PRODUCT]


def _rows_from_xlsx(raw: bytes) -> list[list[object]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise CatalogError(
            "ملفات Excel تحتاج مكتبة openpyxl. ثبّتها أو صدّر الملف بصيغة CSV."
        ) from exc
    workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        sheet = workbook.active
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()


def _looks_like_xlsx(raw: bytes, filename: str) -> bool:
    return filename.lower().endswith((".xlsx", ".xlsm")) or raw[:2] == b"PK"


def parse_catalog_bytes(raw: bytes, filename: str = "catalog.csv") -> CatalogParseResult:
    """Parse an uploaded catalog file into validated products.

    Accepts CSV (several encodings, comma/semicolon/tab delimited) and XLSX.
    Raises :class:`CatalogError` with an operator-readable Arabic message when
    the file cannot be used.
    """
    if not raw or not raw.strip():
        raise CatalogError("الملف فارغ.")

    result = CatalogParseResult()

    if _looks_like_xlsx(raw, filename):
        raw_rows = _rows_from_xlsx(raw)
        result.source_format = "xlsx"
        result.detected_encoding = "xlsx"
        if not raw_rows:
            raise CatalogError("الملف لا يحتوي على أي صفوف.")
        headers = [str(cell) if cell is not None else "" for cell in raw_rows[0]]
        body: Iterable[dict[str, object]] = [
            {headers[i]: (row[i] if i < len(row) else None) for i in range(len(headers))}
            for row in raw_rows[1:]
        ]
    else:
        text, encoding = _decode(raw)
        result.detected_encoding = encoding
        sample = text[:8192]
        try:
            dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(
                sample, delimiters=",;\t"
            )
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        headers = list(reader.fieldnames or [])
        body = reader

    columns = _resolve_columns(headers)
    missing = [f for f in ("product_id", "product_name") if f not in columns]
    if missing:
        present = ", ".join(h for h in headers if h) or "(بدون عناوين)"
        raise CatalogError(
            "الملف لازم يحتوي على عمود للكود وعمود لاسم المنتج. "
            f"الأعمدة الموجودة: {present}. "
            "الأعمدة المقبولة للكود: product_id / sku / كود المنتج — "
            "وللاسم: product_name / اسم المنتج / الصنف."
        )

    products: list[CatalogProduct] = []
    seen_ids: dict[str, int] = {}
    skipped_blank = 0
    truncated = False

    for line_number, row in enumerate(body, start=2):
        if len(products) >= MAX_CATALOG_ROWS:
            truncated = True
            break

        product_id = str(row.get(columns["product_id"]) or "").strip()
        product_name = re.sub(
            r"\s+", " ", str(row.get(columns["product_name"]) or "")
        ).strip()

        if not product_id and not product_name:
            skipped_blank += 1
            continue
        if not product_id:
            raise CatalogError(f"صف رقم {line_number}: كود المنتج مفقود.")
        if not product_name:
            raise CatalogError(
                f"صف رقم {line_number}: اسم المنتج مفقود (الكود {product_id})."
            )

        if product_id in seen_ids:
            raise CatalogError(
                f"الكود '{product_id}' مكرر في صف {seen_ids[product_id]} وصف "
                f"{line_number}. لازم كل كود يكون فريد."
            )
        seen_ids[product_id] = line_number

        aliases = _split_aliases(row.get(columns["aliases"])) if "aliases" in columns else []
        unit = str(row.get(columns["unit"]) or "").strip() if "unit" in columns else ""
        price = _clean_price(row.get(columns["price"])) if "price" in columns else 0.0

        products.append(
            CatalogProduct(
                product_id=product_id,
                product_name=product_name,
                aliases=aliases,
                unit=unit or "قطعة",
                price=price,
            )
        )

    if not products:
        raise CatalogError("لم يتم العثور على أي منتج صالح في الملف.")

    if truncated:
        result.warnings.append(
            f"تم قبول أول {MAX_CATALOG_ROWS} صنف فقط؛ باقي الصفوف تم تجاهلها."
        )
    if skipped_blank:
        result.warnings.append(f"تم تجاهل {skipped_blank} صف فارغ.")
    if "aliases" not in columns:
        result.warnings.append(
            "لا يوجد عمود مرادفات. النظام هيشتغل، لكن إضافة أسماء بالعامية "
            "لكل صنف بترفع دقة المطابقة بشكل ملحوظ."
        )
    if "price" not in columns:
        result.warnings.append("لا يوجد عمود سعر؛ تم ضبط كل الأسعار على صفر.")

    result.products = products
    return result


def load_catalog(csv_path: str | None = None) -> list[CatalogProduct]:
    """Load a catalog from disk (bundled demo catalog and first-run seeding)."""
    if csv_path is None:
        csv_path = os.getenv("CATALOG_PATH") or DEFAULT_CATALOG_PATH

    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Catalog file not found: {csv_path}")

    return parse_catalog_bytes(path.read_bytes(), path.name).products


def get_catalog_dict(products: list[CatalogProduct]) -> dict[str, CatalogProduct]:
    """Create a ``product_id -> CatalogProduct`` lookup dict."""
    return {p.product_id: p for p in products}


def catalog_template_csv() -> str:
    """A ready-to-fill template the merchant can export their catalog into."""
    buffer = io.StringIO()
    buffer.write("﻿")
    writer = csv.writer(buffer)
    writer.writerow(["product_id", "product_name", "aliases", "unit", "price"])
    writer.writerow(
        [
            "CL001",
            "صابون سائل لليدين كبير 2 لتر",
            "صابون سايل كبير|صابون ايد كبير",
            "قطعة",
            "45.00",
        ]
    )
    writer.writerow(["CL004", "كلور مركز 4 لتر", "كلوركس مركز|كلور كبير", "جركن", "35.00"])
    return buffer.getvalue()
