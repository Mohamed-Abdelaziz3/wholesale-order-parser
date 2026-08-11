# Security policy

## Threat model

The order text originates from the **merchant's own customers over WhatsApp**.
Treat every field derived from it — `raw_text`, `extracted_product`, `unit` — as
fully attacker-controlled input that ends up in a browser and in an Excel file.

## Controls in this build

| Control | Where |
| --- | --- |
| Authentication required on every data endpoint | `app/security.py`, `app/main.py` |
| Reviewer identity bound to the session; a mismatched body `actor` is rejected `403` | `security.bind_actor` |
| Signed session cookies, configurable lifetime, `SESSION_HTTPS_ONLY` for TLS | `SessionMiddleware` |
| Login throttling per proxy-normalised client address and account name; raw forwarded headers are never read by app code | `security.LoginThrottle`, Uvicorn proxy middleware |
| Production startup refuses generated credentials, insecure cookies, broad proxy trust, or an ephemeral database path | `app.runtime_config` |
| HTML escaping of every untrusted value before it reaches the DOM | `templates/index.html` (`esc()`) |
| CSV formula-injection neutralisation (`= + - @` prefixing) | `security.csv_safe` |
| Generic client error messages; details only in the server log | `main._server_error` |
| Catalog upload size limit and strict parsing | `main.upload_catalog`, `catalog.parse_catalog_bytes` |
| Approval and export refuse rows without verified human provenance | `database.approve_order`, `database.record_export` |
| CSP, anti-framing, `nosniff`, no-referrer and `no-store` response headers | `main._reject_cross_site_writes` |
| Audited CSV/XLSX exports use POST; state-changing GET exports do not exist | `main.export_order_csv`, `main.export_order_xlsx` |

Regression tests for all of the above live in `tests/test_security_and_catalog.py`.
CI also runs Ruff security rules, `pip-audit`, a Docker build, and GitHub CodeQL.

## Known limitations — state these to any customer

* **Instance-per-customer.** One isolated deployment serves one merchant. Do
  not host two customers on one instance.
* **No SSO or 2FA.** Shared password or `name:password` accounts only.
* **No per-row authorization.** Any signed-in operator can read and act on any
  order in that deployment.
* **Rate limiting covers login only**, not general API abuse.
* **Credentials are temporary pilot credentials.** `APP_USERS` values are kept
  in deployment environment variables, not a hashed user database. Every
  deployment must have its own generated passwords and session secret.

## Safe publication rules

* Never commit `.env`, API keys, private keys, credentials, customer orders,
  production databases, logs, or exports.
* Keep provider keys in environment variables, an explicitly configured
  machine-local `WOP_ENV_FILE`, or Railway service variables. The application
  does not search the working directory for `.env`; `.env.example` holds field
  names only.
* Treat benchmark labels, sealed evaluation data, predictions, and generated
  results as restricted unless their owner has approved publication.
* Review the staged file list before every push. A GitHub repository can be
  public even when the local folder is private.
* Do not keep `.env` inside a cloud-synced folder (OneDrive, Dropbox, Google
  Drive). Sync copies the secret off the machine. On Windows use
  `C:\ProgramData\wop\wop.env` or another explicit non-synchronised path.

## If a secret was exposed

1. Revoke or rotate it immediately with the provider.
2. Check access logs and purge the secret from every published git history where
   possible.
3. Do not report it in a public issue — use GitHub private vulnerability
   reporting or contact the repository owner directly.

## Reporting

Report a vulnerability privately to the repository owner. Include reproduction
steps and the affected endpoint.
