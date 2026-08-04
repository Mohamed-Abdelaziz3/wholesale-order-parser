"""Order item extraction using the Google Gemini API.

Reliability notes
-----------------
The blind evaluation measured a 20% provider error rate, which destroyed more
line items than any modelling problem. The original implementation treated every
failure identically: walk to the next model and give up, with no timeout, no
backoff, and no distinction between a transient 429 and a permanent bad request.
This module retries transient failures on the same model with exponential
backoff and jitter, bounds every call with a timeout, and only falls through to
the next model on non-transient errors.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from typing import Optional

from .models import ExtractedItem, ExtractionResult
from .normalizer import normalize_arabic

logger = logging.getLogger(__name__)

DEFAULT_MODELS = ("gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.5-flash")
DEFAULT_TIMEOUT_SECONDS = 25.0
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 0.8
# Hard ceiling across every model and retry. Without it, three models x four
# attempts x backoff parked a worker thread for over five minutes per request.
DEFAULT_DEADLINE_SECONDS = 45.0
MAX_MESSAGE_CHARS = 6000

_TRANSIENT_MARKERS = (
    "429", "resource_exhausted", "rate limit", "ratelimit", "quota",
    "500", "502", "503", "504", "unavailable", "internal error",
    "deadline", "timeout", "timed out", "connection",
)


class ExtractionUnavailable(RuntimeError):
    """The provider could not be reached or refused every attempt."""


EXTRACTION_PROMPT = """أنت نظام ذكي متخصص في تحليل طلبات تجار الجملة المكتوبة باللهجة المصرية.

## المطلوب
استخرج كل منتج مطلوب من الرسالة التالية، مع الكمية والوحدة.

## القواعد الصارمة
1. استخرج كل منتج على حدة كعنصر منفصل.
2. حوّل الأرقام المكتوبة بالحروف (مثل: تلاته، خمسه، عشرين) إلى أرقام.
3. حدد الوحدة من: كرتونة، رول، باكو، قطعة، دستة، جركن، لفة، كيس.
4. إذا لم تُذكر الكمية صراحة، ضع 1.
5. إذا لم تُذكر الوحدة، ضع "قطعة".
6. إذا ذُكر تصحيح للكمية (مثل: "10 لا خلي 7" أو "عشرة بس غيرها لخمسة")، استخدم الرقم المصحح فقط.
7. "نص دستة" = 6 قطع.
8. "دستة" = 12 قطعة.
9. حافظ على أبعاد المنتج إن وُجدت (مثل: 50 في 70، 70×100).
10. حافظ على الحجم أو السعة أو الوزن إن وُجد (مثل: 500 مل، 1 لتر، 5 كيلو) داخل product_description، لأنه غالباً هو الفرق الوحيد بين أصناف متشابهة.
11. إذا كان جزء من الرسالة غير مفهوم أو لا يتعلق بطلب منتج، ضعه في unresolved_text.
12. لا تخترع منتجات غير مذكورة في الرسالة.

## الرسالة
{message}

## أجب بصيغة JSON فقط (بدون أي نص إضافي):
{{
  "items": [
    {{
      "raw_text": "الجزء الأصلي من الرسالة الخاص بهذا المنتج",
      "product_description": "وصف المنتج بعد التنظيف مع الأبعاد والحجم إن وُجدت",
      "quantity": الكمية_كرقم,
      "unit": "الوحدة"
    }}
  ],
  "unresolved_text": ["أي جزء غير مفهوم"]
}}"""


def _parse_json_response(text: str) -> dict:
    """Parse JSON from a model response, tolerating markdown fences."""
    if not text or not text.strip():
        raise ValueError("Empty response from the extraction provider")

    fenced = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse JSON from the extraction response:\n{text[:500]}")


def _is_transient(error: BaseException) -> bool:
    blob = f"{type(error).__name__} {error}".lower()
    return any(marker in blob for marker in _TRANSIENT_MARKERS)


def _coerce_quantity(value: object) -> float:
    try:
        quantity = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1.0
    if quantity != quantity or quantity in (float("inf"), float("-inf")):
        return 1.0
    return quantity if quantity > 0 else 1.0


def _build_result(data: dict) -> ExtractionResult:
    items: list[ExtractedItem] = []
    for raw_item in data.get("items") or []:
        if not isinstance(raw_item, dict):
            continue
        description = str(raw_item.get("product_description") or "").strip()
        if not description:
            continue
        items.append(
            ExtractedItem(
                raw_text=str(raw_item.get("raw_text") or description).strip(),
                product_description=description,
                quantity=_coerce_quantity(raw_item.get("quantity", 1)),
                unit=str(raw_item.get("unit") or "قطعة").strip() or "قطعة",
            )
        )

    unresolved = data.get("unresolved_text") or []
    if isinstance(unresolved, str):
        unresolved = [unresolved] if unresolved.strip() else []
    unresolved = [str(u).strip() for u in unresolved if str(u).strip()]

    return ExtractionResult(items=items, unresolved_text=unresolved)


class GeminiExtractor:
    """Extract order items from Egyptian-dialect messages using Gemini."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        models: Optional[tuple[str, ...]] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_delay: float = DEFAULT_BASE_DELAY,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    ):
        self.api_key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()
        if not self.api_key:
            raise ValueError(
                "GEMINI_API_KEY not set. Set it in the .env file or pass it to the constructor."
            )

        try:
            from google import genai
        except ImportError as exc:
            raise ExtractionUnavailable(
                "The 'google-genai' package is required for Gemini extraction. "
                "Install it with: pip install google-genai"
            ) from exc

        configured = os.getenv("GEMINI_MODELS", "").strip()
        self.candidate_models = list(
            models
            or ([m.strip() for m in configured.split(",") if m.strip()] or DEFAULT_MODELS)
        )
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self.base_delay = max(0.05, float(base_delay))
        self.deadline_seconds = max(5.0, float(deadline_seconds))
        self.client = genai.Client(api_key=self.api_key)

    # -- internals ---------------------------------------------------------

    def _generate(self, model: str, prompt: str) -> str:
        from google.genai import types

        config_kwargs: dict = {"temperature": 0.1, "max_output_tokens": 2048}
        try:
            config = types.GenerateContentConfig(
                **config_kwargs,
                http_options=types.HttpOptions(timeout=int(self.timeout_seconds * 1000)),
            )
        except TypeError:  # pragma: no cover - older SDK without http_options
            config = types.GenerateContentConfig(**config_kwargs)

        response = self.client.models.generate_content(
            model=model, contents=prompt, config=config
        )
        return response.text or ""

    def _sleep_for(self, attempt: int, remaining: float) -> None:
        delay = self.base_delay * (2 ** attempt)
        # Randomness only de-synchronises retries; it protects no secret.
        jitter = random.random()  # noqa: S311
        time.sleep(max(0.0, min(8.0, delay, remaining) * (0.5 + jitter)))

    # -- public API --------------------------------------------------------

    def extract(self, message: str) -> ExtractionResult:
        """Extract order items from a raw message.

        Raises:
            ExtractionUnavailable: every model and retry was exhausted.
        """
        message = (message or "").strip()
        if not message:
            return ExtractionResult(items=[], unresolved_text=[])
        if len(message) > MAX_MESSAGE_CHARS:
            message = message[:MAX_MESSAGE_CHARS]

        prompt = EXTRACTION_PROMPT.format(message=message)
        last_error: Optional[BaseException] = None
        deadline = time.monotonic() + self.deadline_seconds

        for model in self.candidate_models:
            for attempt in range(self.max_attempts):
                if time.monotonic() >= deadline:
                    raise ExtractionUnavailable(
                        f"Extraction deadline of {self.deadline_seconds:.0f}s exceeded. "
                        f"Last error: {last_error}"
                    ) from last_error
                try:
                    return _build_result(_parse_json_response(self._generate(model, prompt)))
                except Exception as error:  # noqa: BLE001 - provider surface is broad
                    last_error = error
                    transient = _is_transient(error)
                    logger.warning(
                        "Extraction attempt %s/%s on %s failed (%s): %s",
                        attempt + 1,
                        self.max_attempts,
                        model,
                        "transient" if transient else "permanent",
                        error,
                    )
                    if not transient:
                        break  # try the next model rather than hammering this one
                    if attempt < self.max_attempts - 1:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._sleep_for(attempt, remaining)

        raise ExtractionUnavailable(
            f"All extraction models failed. Last error: {last_error}"
        ) from last_error


class MockExtractor:
    """Deterministic extractor for tests and offline demos."""

    def __init__(self, predefined: dict[str, ExtractionResult] | None = None):
        self.predefined = predefined or {}

    def extract(self, message: str) -> ExtractionResult:
        normalized = normalize_arabic(message)
        if message in self.predefined:
            return self.predefined[message]
        if normalized in self.predefined:
            return self.predefined[normalized]
        return self._simple_extract(normalized)

    def _simple_extract(self, text: str) -> ExtractionResult:
        """Very basic extraction for testing - not production quality."""
        items = []
        pattern = re.compile(
            r"(\d+)\s*"
            r"(كرتون[ةه]?|كراتين|رول|لف[ةه]?|باكو|قطع[ةه]?|حت[ةه]?|"
            r"دست[ةه]?|جركن|جراكن|كيس|لفف)?\s*"
            r"([^\d,،و]+?)(?=\s*(?:و\s*\d|\d|$))",
            re.UNICODE,
        )
        for match in pattern.finditer(text):
            product = match.group(3).strip()
            if product:
                items.append(
                    ExtractedItem(
                        raw_text=match.group(0).strip(),
                        product_description=product,
                        quantity=float(match.group(1)),
                        unit=match.group(2) or "قطعة",
                    )
                )
        return ExtractionResult(items=items, unresolved_text=[])
