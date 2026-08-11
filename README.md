# Wholesale Order Parser

<p align="center">
  <img src="linkedin-shot-01-input.png" alt="Wholesale Order Parser — Arabic order intake" width="100%">
</p>

<p align="center">
  <a href="https://github.com/Mohamed-Abdelaziz3/wholesale-order-parser/actions/workflows/ci.yml"><img src="https://github.com/Mohamed-Abdelaziz3/wholesale-order-parser/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/workflow-human--in--the--loop-7C3AED" alt="Human in the loop">
  <img src="https://img.shields.io/badge/status-pilot--ready-0F766E" alt="Pilot ready">
</p>

<p align="center"><strong>من رسالة واتساب بالعامية المصرية إلى طلب مُراجع، مُعتمد، وقابل للتصدير — من غير ما النموذج يقرر بدل الإنسان.</strong></p>

Turns free-text wholesale orders written in Egyptian Arabic into structured,
catalog-matched orders with mandatory, attributable human review. The system
**narrows the catalog; it never decides.** Every line needs an explicit decision
from a signed-in operator before approval or export.

**Release notes:** [CHANGELOG.md](CHANGELOG.md)

## Why this exists

Wholesale orders arrive as noisy voice-to-text, abbreviations, dialect, mixed
units, and product nicknames. A fully automatic SKU pick is unsafe on catalogs
with close variants; a manual re-entry workflow is slow and loses provenance.
This project keeps the useful part of automation — extraction and shortlist
generation — while making the commercial decision explicit, reviewable, and
auditable.

## Architecture

```mermaid
flowchart LR
    A["WhatsApp order\nEgyptian Arabic"] --> B["Gemini extraction\nproduct · quantity · unit"]
    B --> C["RapidFuzz retrieval\ntop-5 catalog candidates"]
    C --> D["Signed-in operator\nconfirm · correct · reject"]
    D --> E["SQLite transaction\naudit + immutable snapshot"]
    E --> F["Picking note\nXLSX · CSV · HTML/image"]
```

---

## Product flow

```
رسالة واتساب بالعامية
      │
      ▼
  Gemini extraction        →  (منتج، كمية، وحدة) لكل سطر
      │
      ▼
  RapidFuzz matching       →  أفضل 5 أصناف مرشّحة من الكتالوج
      │
      ▼
  مراجعة بشرية إجبارية      →  الترشيح مختار مسبقاً، والمراجع يأكّد أو يعدّل
      │
      ▼
  اعتماد بتوقيع + snapshot ثابت
      │
      ▼
  إذن صرف للتحميل (HTML/صورة) + CSV للمخزن
```

The downloadable document is an **internal picking / delivery note**
(`إذن صرف / أمر تحضير`). This build is not an ETA-integrated e-invoicing
provider: it has no e-signature, submits nothing to the Tax Authority, and
receives no UUID from it. Every generated document says so in a footer, and
`tests/test_document_and_settings.py` fails the build if any source file claims
otherwise. The optional `الكود الضريبي` field in shop settings is display text
only.

### Human-reviewed matching

Gemini extracts advisory product, quantity, and unit candidates. It does not
select a commercial SKU, set a price, or approve an order. Catalog-backed
matching and an explicit human decision remain authoritative for every line;
the shortlist assists review and is not an automatic commercial decision.

### Review → approval

<p align="center">
  <img src="linkedin-shot-02-review.png" alt="Mandatory human review" width="49%">
  <img src="linkedin-shot-03-approved.png" alt="Approved wholesale order" width="49%">
</p>

---

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Do not create .env inside this repository: it may be cloud-synchronised.
# On Windows, copy the field reference to C:\ProgramData\wop\wop.env and edit
# that machine-local file. Or set WOP_ENV_FILE to another absolute,
# non-synchronised path. Railway uses service variables instead.
# Configure GEMINI_API_KEY, APP_PASSWORD (or APP_USERS), and APP_SESSION_SECRET
# in that external development environment file.

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>; you will be redirected to `/login`.

For local development, configure explicit credentials before opening the app.
If they are absent, a temporary development credential is generated but never
printed or accepted as a production setup; do not rely on that fallback.

> **Dependency note:** extraction imports `from google import genai`, which comes
> from the **`google-genai`** package (declared in `requirements.txt`). The older
> `google-generativeai` package provides `google.generativeai` and does **not**
> satisfy this import.

### Tests

```bash
pip install -r requirements-dev.txt
ruff check app tests serve_demo.py
pytest
pip-audit -r requirements.txt
```

GitHub Actions repeats these gates for pull requests and pushes to `main`,
builds the Docker image, and runs scheduled CodeQL analysis. Dependabot watches Python,
GitHub Actions, and Docker dependencies weekly.

---

## Onboarding a merchant's catalog

The demo catalog (50 cleaning/packaging products) ships in `catalog.csv` and is
installed automatically on first run. For a real merchant:

1. Sign in → **إدارة الكتالوج** → **تحميل نموذج** to get a template.
2. They export their product list; upload the CSV or XLSX as-is.
3. The upload replaces the working catalog atomically and rebuilds the matcher.

The parser accepts:

* **CSV** (UTF-8, UTF-8-BOM, or **cp1256** — what Arabic Windows Excel produces)
  with comma, semicolon, or tab delimiters, and **XLSX/XLSM**.
* **English or Arabic headers**: `product_id` / `sku` / `كود المنتج`,
  `product_name` / `الصنف` / `اسم المنتج`, `aliases` / `مرادفات`,
  `unit` / `الوحدة`, `price` / `السعر`.
* `aliases` are optional. Product ID, product name, unit, and price are required
  commercial fields.

Duplicate codes, missing required fields, malformed prices, and invalid units
are rejected with actionable row diagnostics.

Replacing the catalog **cannot rewrite history**: approved snapshots store the
product name, unit, and price captured at approval time.

---

## Security model

| Control | Behaviour |
| --- | --- |
| Authentication | Required on every data endpoint. The only public routes are `/login`, `/api/login`, `/api/logout`, `/api/session`, `/api/health`. `/docs`, `/redoc` and `/openapi.json` are **disabled** unless `EXPOSE_API_DOCS=true`. |
| Reviewer identity | Taken from the **signed-in session**. A request whose `actor` disagrees with the session is rejected with `403` — never silently rewritten. |
| Sessions | Signed cookies (`itsdangerous`), 12 h default, `SESSION_HTTPS_ONLY=true` behind TLS. `APP_ENV=production` refuses a generated or short session secret. |
| Brute force | Login throttling keyed on **both** the proxy-normalised client address and the account name. Raw `X-Forwarded-For` is never read by application code; Uvicorn accepts it only from the configured trusted proxy CIDRs. |
| Production startup | The single-merchant profile requires an explicit Gemini key, named `APP_USERS`, HTTPS-only cookies, trusted proxy CIDRs, and an `ORDERS_DB_PATH` inside `PERSISTENT_VOLUME_PATH`; unsafe production configuration stops startup. |
| Stored XSS | Every untrusted value is HTML-escaped before it reaches the DOM. |
| CSV injection | Cells beginning `= + - @` are prefixed so Excel treats them as text. |
| Error responses | Generic messages to the client; details go to the server log only. |
| CSRF | Browser-originated state-changing requests with an `Origin` or `Referer` must be same-origin. Requests without either header are allowed for authenticated non-browser callers. `SameSite=Lax` alone is not enough: "same site" ignores the port, so another port on the same host — and any sibling subdomain — is same-site, and a `multipart/form-data` POST needs no preflight. |
| Browser hardening | CSP, anti-framing, `nosniff`, no-referrer, restricted browser capabilities, and `no-store` on sensitive responses. |
| Export semantics | CSV/XLSX exports are POST operations because they create immutable audit events; state-changing GET exports are rejected. |

**Known limitations, stated plainly:**

* **Instance-per-customer.** One deployment serves one merchant, one catalog,
  and one database. Do not put two customers on one instance.
* **No per-order authorization.** Any signed-in operator can read and act on any
  order in that deployment.
* **Logout clears the client session but does not revoke the signed cookie**,
  which stays valid until `APP_SESSION_MAX_AGE` elapses.
* Keep the database **outside** any cloud-synced folder. SQLite in WAL mode
  writes `orders.db`, `orders.db-wal` and `orders.db-shm` as one atomic unit;
  OneDrive syncing them independently can corrupt or silently roll back data.
  Set `ORDERS_DB_PATH` to a path such as `C:\ProgramData\wop\orders.db`.
* Do not keep a runtime `.env` in this repository or any synced folder. The app
  only loads an explicit `WOP_ENV_FILE` (or `C:\ProgramData\wop\wop.env` on
  Windows); Railway uses service variables. Each deployment needs distinct
  `APP_USERS` passwords and a distinct `APP_SESSION_SECRET`.

---

## Human-review contract

Every line decision is one complete, attributable, idempotent command:

```json
{
  "actor": "operator-name",
  "action_id": "review-<unique-id>",
  "final_decision": "SELECT",
  "selected_sku": "CL010",
  "quantity": 6,
  "unit": "قطعة"
}
```

`actor`, `action_id`, and `final_decision` are mandatory, and `actor` must equal
the signed-in operator. `SELECT` also requires SKU, quantity, and unit.
`NOT_FOUND` must not carry a SKU. Reusing an `action_id` with different content
is rejected.

Approval is a separate attributable command:

```json
{ "actor": "operator-name", "action_id": "approve-<unique-id>" }
```

Approval, its audit event, and the immutable snapshot are one transaction. Export
reads only that snapshot and refuses rows without verified review and approval
provenance. Human review never overwrites the original model recommendation or
its confidence.

`RR_K_AUTO_ACCEPT_ENABLED` is inert: a truthy value is logged and ignored.
Autonomous auto-accept is not available in this build.

---

## API

| Method | Path | Auth | Purpose |
| --- | --- | :---: | --- |
| GET | `/login` | – | Sign-in page |
| POST | `/api/login` | – | Start a session |
| POST | `/api/logout` | – | End the session |
| GET | `/api/session` | – | Who am I |
| GET | `/api/health` | – | Liveness probe |
| GET | `/` | ✔ | Review UI |
| POST | `/api/process` | ✔ | Extract + match one message |
| GET | `/api/orders` | ✔ | Recent orders |
| GET | `/api/orders/{id}` | ✔ | Working state + approved snapshot |
| POST | `/api/orders/{id}/items/{item}/review` | ✔ | Apply one line decision |
| PUT | `/api/orders/{id}/items/{item}` | ✔ | Compatibility alias |
| POST | `/api/orders/{id}/items/{item}/confirm-not-found` | ✔ | Compatibility NOT_FOUND |
| POST | `/api/orders/{id}/approve` | ✔ | Approve + snapshot |
| POST | `/api/orders/{id}/export` | ✔ | Export the verified snapshot as CSV + audit the export |
| POST | `/api/orders/{id}/export.xlsx` | ✔ | Export the verified snapshot as formatted XLSX + audit the export |
| GET | `/api/orders/{id}/document` | ✔ | Download the picking note (self-contained HTML) |
| GET | `/api/orders/{id}/audit` | ✔ | Audit events |
| GET | `/api/settings` | ✔ | Shop identity printed on the document |
| PUT | `/api/settings` | ✔ | Update shop identity (incl. base64 logo) |
| GET | `/api/catalog` | ✔ | Current catalog |
| GET | `/api/catalog/info` | ✔ | Catalog version metadata |
| GET | `/api/catalog/template` | ✔ | Download a catalog template |
| POST | `/api/catalog/upload` | ✔ | Replace the catalog (CSV/XLSX) |

---

## Timing metrics

The UI reports **measured** time only:

* Before approval it shows the automated processing time and states explicitly
  that no saving has been realised yet.
* After approval it shows the measured analysis→approval cycle against a clearly
  labelled manual baseline (45 s/line — an assumption, not a measurement;
  calibrate it against the merchant's real workflow).

---

## Deployment

For the first real deployment, follow
[docs/pilot/RAILWAY_SINGLE_MERCHANT_DEPLOYMENT.md](docs/pilot/RAILWAY_SINGLE_MERCHANT_DEPLOYMENT.md)
exactly. It requires one Railway service, one replica, one mounted `/app/data`
volume, and one SQLite database. [DEPLOY.md](DEPLOY.md) is a concise entry point
to that supported Railway-only topology.

---

## Migration from an older database

```python
from app.database import (
    preflight_human_review_migration,
    apply_human_review_migration,
    verify_human_review_migration,
)

preflight_human_review_migration("orders.db")   # read-only counts
# back up orders.db here
apply_human_review_migration("orders.db")
verify_human_review_migration("orders.db")
```

Startup refuses a legacy schema instead of rewriting it silently. Existing
snapshots are preserved but become legacy-unverified — and legacy-unverified
snapshots cannot be exported — unless matching attributable review and approval
events validate them. Never fabricate actors, action IDs, timestamps, or audit
events to make a row exportable.

---

## License

Released under the [MIT License](LICENSE). You may use, modify, and distribute
the software under its terms, including retention of the copyright and
permission notice; it is provided without warranty.
