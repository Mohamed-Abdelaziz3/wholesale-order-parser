"""Authentication, session identity, and output-sanitisation helpers.

Design contract
---------------
* The reviewer identity recorded in the audit trail is **derived from the
  authenticated session**, never from the request body. A body-supplied
  ``actor`` is still required (it keeps the explicit-intent contract) but it is
  rejected with 403 when it disagrees with the session identity.
* Authentication is enforced by default. If no password is configured the
  application generates a random one at startup and prints it, instead of
  silently running open to the internet.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple
from urllib.parse import quote, urlsplit

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

SESSION_OPERATOR_KEY = "operator"
SESSION_ISSUED_AT_KEY = "issued_at"

# Login throttling (per client address). Deliberately in-process and small: this
# is a single-node pilot deployment, not a distributed auth service.
_MAX_ATTEMPTS = 8
_WINDOW_SECONDS = 300
_LOCKOUT_SECONDS = 300


@dataclass
class AuthConfig:
    """Resolved authentication configuration for one application instance."""

    users: Dict[str, str] = field(default_factory=dict)
    shared_password: Optional[str] = None
    session_secret: str = ""
    session_max_age: int = 60 * 60 * 12
    generated_password: bool = False
    generated_secret: bool = False

    @property
    def uses_named_accounts(self) -> bool:
        return bool(self.users)

    def verify(self, operator: str, password: str) -> bool:
        """Constant-time credential check."""
        operator = (operator or "").strip()
        password = password or ""
        if not operator or not password:
            return False
        if self.users:
            expected = self.users.get(operator)
            if expected is None:
                # Still burn a comparison so timing does not leak account existence.
                hmac.compare_digest(password, secrets.token_urlsafe(16))
                return False
            return hmac.compare_digest(password, expected)
        if self.shared_password is None:
            return False
        return hmac.compare_digest(password, self.shared_password)


def _parse_users(raw: str) -> Dict[str, str]:
    """Parse ``name:password,name:password`` into a mapping."""
    users: Dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(
                "APP_USERS entries must look like 'name:password'; "
                f"received {chunk!r}"
            )
        name, password = chunk.split(":", 1)
        name, password = name.strip(), password.strip()
        if not name or not password:
            raise ValueError("APP_USERS entries must have a non-empty name and password")
        users[name] = password
    return users


def load_auth_config(env: Optional[Dict[str, str]] = None) -> AuthConfig:
    """Build an :class:`AuthConfig` from the environment, failing safe."""
    source = os.environ if env is None else env

    users = _parse_users(source.get("APP_USERS", "") or "")
    shared = (source.get("APP_PASSWORD") or "").strip() or None
    generated_password = False

    if not users and shared is None:
        shared = secrets.token_urlsafe(12)
        generated_password = True

    secret = (source.get("APP_SESSION_SECRET") or "").strip()
    generated_secret = False
    if not secret:
        secret = secrets.token_urlsafe(48)
        generated_secret = True

    try:
        max_age = int(source.get("APP_SESSION_MAX_AGE", "43200"))
    except ValueError:
        max_age = 43200

    return AuthConfig(
        users=users,
        shared_password=shared,
        session_secret=secret,
        session_max_age=max(300, max_age),
        generated_password=generated_password,
        generated_secret=generated_secret,
    )


def announce(config: AuthConfig) -> None:
    """Log startup credentials exactly once, loudly, when auto-generated."""
    if config.generated_password:
        logger.warning(
            "\n"
            "==========================================================\n"
            " No APP_PASSWORD / APP_USERS was configured.\n"
            " A temporary password was generated for this process:\n\n"
            "     %s\n\n"
            " Set APP_PASSWORD (or APP_USERS) before any real deployment.\n"
            "==========================================================",
            config.shared_password,
        )
    if config.generated_secret:
        logger.warning(
            "APP_SESSION_SECRET is not set; a random secret was generated. "
            "Sessions will not survive a restart. Set it for production."
        )


class LoginThrottle:
    """Small fixed-window throttle so the login form is not a brute-force oracle."""

    def __init__(self) -> None:
        self._attempts: Dict[str, Tuple[int, float]] = {}

    def blocked_for(self, key: str, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        count, first_seen = self._attempts.get(key, (0, now))
        if count < _MAX_ATTEMPTS:
            return 0.0
        remaining = (first_seen + _WINDOW_SECONDS + _LOCKOUT_SECONDS) - now
        if remaining <= 0:
            self._attempts.pop(key, None)
            return 0.0
        return remaining

    def record_failure(self, key: str, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        count, first_seen = self._attempts.get(key, (0, now))
        if now - first_seen > _WINDOW_SECONDS and count < _MAX_ATTEMPTS:
            count, first_seen = 0, now
        self._attempts[key] = (count + 1, first_seen)

    def reset(self, key: str) -> None:
        self._attempts.pop(key, None)


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _origin_of(request: Request) -> Optional[str]:
    """Best-effort browser origin for this request, or None for non-browser clients."""
    origin = (request.headers.get("origin") or "").strip()
    if origin:
        return origin.rstrip("/").lower()
    referer = (request.headers.get("referer") or "").strip()
    if referer:
        parts = urlsplit(referer)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}".rstrip("/").lower()
    return None


def allowed_origins(request: Request) -> set[str]:
    """Origins accepted for state-changing requests to this deployment."""
    configured = {
        o.strip().rstrip("/").lower()
        for o in (os.getenv("ALLOWED_ORIGINS", "") or "").split(",")
        if o.strip()
    }
    url = request.url
    if url.netloc:
        configured.add(f"{url.scheme}://{url.netloc}".rstrip("/").lower())
    return configured


def csrf_violation(request: Request) -> Optional[str]:
    """Return a rejection reason for a cross-site state-changing request.

    ``SameSite=Lax`` alone does not stop CSRF here. "Same site" ignores the port,
    so a page served from another port on the same host — exactly how this app is
    first demoed and tunnelled — is treated as same-site and its cookies are sent.
    Subdomains share a site too. A ``multipart/form-data`` POST is additionally a
    CORS-safelisted request, so it reaches the server with no preflight at all:
    that is enough to replace the merchant's whole catalog from a hostile page.

    Missing Origin/Referer is allowed: browsers normally send one on a
    cross-origin state-changing request, while non-browser clients (curl, the
    test suite, server-to-server callers) legitimately send neither.

    An explicit ``Origin: null`` is different from a missing header. Browsers
    use it for opaque origins such as sandboxed documents, so it is rejected.
    """
    if request.method.upper() in SAFE_METHODS:
        return None
    origin = _origin_of(request)
    if origin is None:
        return None
    if origin in allowed_origins(request):
        return None
    return origin


def client_key(request: Request) -> str:
    """Throttle key for the caller's network address.

    ``X-Forwarded-For`` is attacker-controlled unless a trusted proxy sets it.
    Honouring it unconditionally turned the login throttle into decoration: an
    attacker rotates the header per request and never trips the counter. It is
    now used only when the deployment explicitly declares it is behind a proxy.
    """
    trust = (os.getenv("TRUST_PROXY_HEADERS", "false") or "").strip().lower()
    if trust in {"1", "true", "yes", "on"}:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def account_key(operator: str) -> str:
    """Throttle key for the account being guessed.

    Address-based throttling alone cannot stop a distributed or header-spoofing
    attacker. Counting failures per account bounds password guessing regardless
    of where the attempts appear to come from.
    """
    return "account:" + (operator or "").strip().lower()


def session_operator(request: Request) -> Optional[str]:
    """Return the authenticated operator name bound to this session, if any."""
    session = getattr(request, "session", None)
    if not session:
        return None
    operator = session.get(SESSION_OPERATOR_KEY)
    return operator if isinstance(operator, str) and operator.strip() else None


def require_operator(request: Request) -> str:
    """FastAPI dependency: the authenticated operator, or 401."""
    operator = session_operator(request)
    if not operator:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Cookie"},
        )
    return operator


def bind_actor(declared_actor: str, operator: str) -> str:
    """Reconcile a body-declared actor with the authenticated session identity.

    The session always wins. A mismatch is refused rather than silently
    rewritten, so a forged actor is an auditable failure instead of a quiet
    correction.
    """
    declared = (declared_actor or "").strip()
    if declared and declared != operator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "The submitted actor does not match the signed-in operator. "
                "Actions are recorded under the authenticated identity only."
            ),
        )
    return operator


# --------------------------------------------------------------------------
# Output sanitisation
# --------------------------------------------------------------------------

_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


MAX_LOGO_BYTES = 500 * 1024

# Magic bytes, not the declared MIME type. The merchant's browser declares the
# type; only the file's own header proves what it is.
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def sanitize_logo_data_url(value: str) -> str:
    """Validate a logo ``data:`` URL and rebuild it from the decoded bytes.

    The stored string is embedded in a downloadable HTML document, so it is
    never trusted as written. The URL is parsed, the payload is strictly
    base64-decoded, identified by its magic bytes, size-checked, and then
    **re-serialised** from those bytes. Anything smuggled in the original
    string — a second payload after whitespace, an ``svg+xml`` body carrying
    script, a bogus MIME label — does not survive the round trip.

    Returns "" for an empty input (meaning: no logo). Raises ``ValueError``
    with an operator-readable Arabic message otherwise.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    if not raw.lower().startswith("data:"):
        raise ValueError("الشعار لازم يكون ملف صورة PNG أو JPG.")
    try:
        header, payload = raw.split(",", 1)
    except ValueError as exc:
        raise ValueError("الشعار لازم يكون ملف صورة PNG أو JPG.") from exc
    if "base64" not in header.lower():
        raise ValueError("الشعار لازم يكون ملف صورة PNG أو JPG.")

    # validate=True rejects any character outside the base64 alphabet instead of
    # silently skipping it, so a payload with markup spliced in fails here.
    compact = "".join(payload.split())
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("الشعار مش صورة صالحة.") from exc

    if not decoded:
        raise ValueError("الشعار مش صورة صالحة.")
    if len(decoded) > MAX_LOGO_BYTES:
        raise ValueError(
            f"حجم الشعار أكبر من الحد المسموح ({MAX_LOGO_BYTES // 1024} كيلوبايت)."
        )

    mime = next(
        (m for signature, m in _IMAGE_SIGNATURES if decoded.startswith(signature)),
        None,
    )
    if mime is None:
        raise ValueError("الشعار لازم يكون ملف صورة PNG أو JPG.")

    return f"data:{mime};base64," + base64.b64encode(decoded).decode("ascii")


def attachment_filename(stem: str, extension: str, fallback: str) -> str:
    """Build a ``Content-Disposition`` value that cannot inject a header.

    The stem comes from the merchant's shop name, which is free text. CR/LF are
    what turn a filename into extra response headers; quotes are what break out
    of the quoted-string. Both are removed rather than escaped, and the ASCII
    fallback keeps old clients working while ``filename*`` carries the Arabic.
    """
    spaced = re.sub(r"\s+", "-", (stem or "").strip())
    cleaned = "".join(
        ch for ch in spaced if ch.isalnum() or ch in {"-", "_"} or ord(ch) > 127
    )
    cleaned = cleaned.replace('"', "").strip("-")[:60].strip("-")
    name = f"{cleaned or fallback}.{extension}"

    ascii_name = "".join(ch for ch in name if 32 < ord(ch) < 127 and ch not in '"\\;')
    if not ascii_name or ascii_name == f".{extension}":
        ascii_name = f"{fallback}.{extension}"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def csv_safe(value: object) -> object:
    """Neutralise spreadsheet formula injection in exported cells.

    Order text originates from the merchant's own customers, so a cell can be
    fully attacker-controlled. Excel treats a leading ``= + - @`` as a formula;
    prefixing with an apostrophe forces text interpretation while remaining
    readable.
    """
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = str(value)
    if text.startswith(_CSV_INJECTION_PREFIXES):
        return "'" + text
    return text
