"""Local demo server with a deterministic extractor (no API key needed)."""

import os
import tempfile

os.environ.setdefault("APP_PASSWORD", "demo-pass")
os.environ.setdefault("APP_SESSION_SECRET", "demo-secret")
demo_db = os.path.join(tempfile.gettempdir(), "demo.db")
os.environ["ORDERS_DB_PATH"] = demo_db

import app.main as m  # noqa: E402
from app.models import ExtractedItem, ExtractionResult  # noqa: E402


class DemoExtractor:
    def extract(self, message: str) -> ExtractionResult:
        return ExtractionResult(
            items=[
                ExtractedItem(raw_text="3 كراتين أكياس سودا 50 في 70",
                              product_description="اكياس سودا 50×70", quantity=3, unit="كرتونة"),
                ExtractedItem(raw_text="اتنين رول استرتش الكبير",
                              product_description="استرتش كبير", quantity=2, unit="رول"),
                ExtractedItem(raw_text="5 بريل كبير",
                              product_description="بريل كبير", quantity=5, unit="قطعة"),
                ExtractedItem(raw_text="4 جركن كلور مركز",
                              product_description="كلور مركز", quantity=4, unit="جركن"),
                ExtractedItem(raw_text="5 كيلو سكر",
                              product_description="سكر", quantity=5, unit="كيس"),
            ],
            unresolved_text=[],
        )


m.extractor = DemoExtractor()
application = m.create_app(demo_db)
