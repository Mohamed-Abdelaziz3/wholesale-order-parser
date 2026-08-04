# GitHub readiness & security audit

**Audit date:** 4 August 2026
**Scope:** the runnable `wholesale-order-parser` application, its tests, deployment
files, repository hygiene, documentation, and presentation artifact.

## Verdict

The codebase is ready for a **public GitHub release** under the MIT License and
is pilot-ready after the deployment checklist below is completed. Public
repository hygiene, automated quality gates, and security reporting files are
included in the release.

## Verified evidence

| Gate | Result |
| --- | --- |
| Offline test suite | Source audit: **329 passed**. Public snapshot: **327 passed, 2 skipped** because restricted immutable evaluation evidence is intentionally excluded. Live Gemini is also excluded because it spends quota. |
| Ruff | **0 findings** |
| Installed dependency consistency | `pip check`: **no broken requirements** |
| Published dependency vulnerabilities | Internet-backed `pip-audit`: **no known vulnerabilities** |
| Common secret signatures in working tree | **0 hits** for Google, GitHub, OpenAI, and private-key patterns |
| Common secret signatures in reachable Git history | **0 file hits** |
| GitHub workflow YAML | Parsed successfully |
| Presentation QA | Every slide rendered and inspected; **no overflow detected** |

## High-value fixes applied

- Ignored local virtual environments, `.claude` settings, audit output, Ruff
  caches, and inaccessible pytest scratch paths so `git add .` cannot sweep in
  thousands of machine-local files.
- Rejected opaque (`Origin: null`) and cross-scheme browser origins.
- Added CSP, anti-framing, `nosniff`, no-referrer, restricted browser-capability,
  no-store, and HTTPS HSTS response headers.
- Changed audited CSV/XLSX exports from state-changing GET routes to POST routes.
- Reworked the dynamic order-history query so user data remains parameterized and
  the SQL structure is assembled only from fixed clauses.
- Added GitHub CI for Ruff, pytest, `pip-audit`, and a production Docker build.
- Added scheduled CodeQL analysis and weekly Dependabot checks for Python,
  GitHub Actions, and Docker.
- Rebuilt the README around real product evidence, architecture, measured
  accuracy, security boundaries, and deployment instructions.

## Known boundaries — not hidden

- One deployment serves one merchant. The application is not multi-tenant.
- Authentication is password-based; there is no SSO or 2FA.
- Any signed-in operator can access any order in that deployment.
- Rate limiting covers login attempts, not general API throughput.
- Logout clears the browser session but does not maintain a server-side token
  revocation list.
- SQLite is intended for a single application instance and must live on a
  persistent, non-synced volume.
- The live Gemini regression test was not run in this audit because it requires
  a real key and paid/quota-bearing API calls.
- Docker was not available on the audit machine, so the local image build was
  not executed; the committed CI workflow makes it a required GitHub gate.

## Required before opening the deployment publicly

1. Set `GEMINI_API_KEY`, `APP_USERS` (or a strong `APP_PASSWORD`), and a stable
   random `APP_SESSION_SECRET` in the hosting platform — never in Git.
2. Set `SESSION_HTTPS_ONLY=true` and serve only through HTTPS.
3. Mount a persistent volume and set `ORDERS_DB_PATH` to that volume.
4. Run one real merchant message through intake → review → approval → export.
5. Confirm `/api/orders` returns `401` when signed out and `/api/health` reports
   `auth_enforced: true`.
6. Keep GitHub Actions, CodeQL, Dependabot, secret scanning, push protection,
   branch protection, and private vulnerability reporting enabled.

## Safe release process

Before committing, inspect the exact staged list with:

```bash
git status --short
git diff --check
git diff --cached --name-status
```

Do not stage `.env`, databases, logs, exports, customer files, local virtual
environments, forensic preservation folders, or operator backups.
