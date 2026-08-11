# Executive Verdict

READY FOR SINGLE-MERCHANT ASSISTED PILOT

This verdict applies to the reviewed repository and its automated regression
evidence. Before any merchant data is used, the deployment operator must apply
the exact Railway configuration below and complete the synthetic, fresh-instance
smoke procedure in `scripts/pilot_smoke_test.py`.

# Deployment Model

This is intentionally **instance-per-customer**:

- One Railway project and one service for one merchant.
- Exactly one region, one replica, and one Uvicorn worker.
- One SQLite database at `/app/data/orders.db`.
- One Railway persistent volume mounted at `/app/data`.
- Unique `APP_USERS` credentials and `APP_SESSION_SECRET` for that deployment.

There is no multi-tenancy, shared merchant database, or tenant boundary.

# Changes Made

- Added durable, database-backed request idempotency to `POST /api/process`,
  including payload-conflict detection, concurrent claims, retry recovery, and
  durable replay of the original order.
- Replaced new commercial `REAL`/float persistence and calculations with
  `Decimal` plus canonical decimal-text SQLite storage. New input is limited to
  two EGP decimal places; derived line totals use `ROUND_HALF_UP`.
- Added strict commercial catalog ingestion: decimal prices, required price and
  unit fields, detected source mapping, unmapped-column reporting, bounded
  malformed-row diagnostics, and no partial catalog installation.
- Added fail-closed requested-unit versus catalog-unit checks, explicit audited
  human mismatch resolution, approval blocking, and matching frontend guards.
- Added durable catalog-only backups before replacement, 10-point retention,
  validated stopped-service restore CLI, and restore tests that preserve
  approved order history.
- Added Railway production validation, explicit credential rules, trusted proxy
  configuration, persistent-volume validation, and no-cwd-`.env` loading.
- Added privacy disclosures, Arabic merchant disclosure, catalog compatibility
  diagnostics, and a synthetic deployment smoke test.

# Security Invariants

- Production startup requires explicit named `APP_USERS`, a generated-strength
  `APP_SESSION_SECRET`, Gemini key, HTTPS-only session cookies, explicit proxy
  CIDRs, and a confirmed Railway volume.
- Production refuses `APP_PASSWORD`, generated credential fallbacks, broad
  forwarded-header trust, a missing volume, or a database/backup path outside
  the volume.
- The application never reads raw `X-Forwarded-For`; it relies only on Uvicorn's
  proxy-normalised client after the configured proxy trust boundary.
- Passwords and provider/model response content are not intentionally logged.
  Extraction failures use content-free diagnostics so pasted order text is not
  echoed from malformed provider responses.

# Commercial Integrity Invariants

- Gemini extracts advisory text only. It never authorises SKU, product identity,
  unit conversion, price, or approval.
- Catalog-backed deterministic data and a named human decision are required for
  every selected commercial line.
- New money is exact `Decimal` business logic and canonical text persistence;
  browser totals are display-only server values. Historical higher-scale money
  evidence remains readable without being silently rounded.
- Unknown, missing, or differing requested/catalog units cannot silently price
  a line. Approval requires an auditable explicit human resolution, and no unit
  conversion factor is inferred.
- Approval creates an immutable commercial snapshot. CSV, XLSX, and dispatch
  documents read the verified approved snapshot, never the live catalog.

# Data Persistence

- `ORDERS_DB_PATH=/app/data/orders.db` and catalog backups under
  `/app/data/catalog-backups` are required inside the one Railway volume.
- At Linux production startup, `/app/data` must be an exact mountpoint in the
  current process's `/proc/self/mountinfo`; a matching environment variable or
  ordinary image directory is not accepted. An unavailable or malformed mount
  table fails startup rather than claiming persistence is proven.
- Runtime mount verification proves the mount exists at that instant only. The
  documented synthetic restart/redeploy persistence verification remains
  mandatory before merchant data because it validates a different failure
  class.
- Catalog replacement creates a timestamped, hash-validated catalog-only
  recovery point before deleting existing catalog rows. Backup failure aborts
  replacement.
- Restoring a catalog backup replaces catalog rows only. Approved orders,
  immutable snapshots, audit events, and exports remain intact.
- The restore CLI requires the service to be stopped and explicit operator
  acknowledgement; restart the one service afterwards so its in-memory matcher
  reloads the restored catalog.

# Known Limitations

- No multi-tenancy.
- No ERP integration.
- No ETA integration.
- No tax invoices.
- No WhatsApp integration.
- No inventory.
- No unit conversions.
- No first-class variants.
- No customer price lists.

The system is an assisted operational workflow, not an ERP, tax, inventory, or
messaging replacement.

# What Must Be Measured During Pilot

Keep privacy-safe pilot measurement records outside the public repository. Track
ambiguity rate, top-1 correctness, manual SKU corrections, review time per
line, order completion time, colour/size/model errors, customer SKU use, and
whether source ERP SKUs already distinguish each size/colour combination.

# Rollback Procedure

For a catalog rollback, stop the one replica, run:

```powershell
python -m app.catalog_maintenance list-backups --db /app/data/orders.db
python -m app.catalog_maintenance restore --db /app/data/orders.db --backup-id BACKUP_ID --actor OPERATOR_NAME --confirm-service-stopped
```

Then restart the same service. This restores catalog content only and does not
rewind approved order history. For an application-code rollback, redeploy the
previous verified Git commit against the same mounted volume, then run the
synthetic persistence check before accepting merchant work.

# Test Results

- Final repository suite: `421 passed, 3 skipped`; it includes
  idempotency, concurrency, Decimal, legacy-money, unit, catalog backup/restore,
  production configuration, privacy, and regression coverage.
- Static verification: `ruff check app tests scripts`, `compileall`, and
  `git diff --check` pass.

The three skipped tests require deliberately undistributed evaluation evidence;
they are not pilot production paths.

# Exact Railway Configuration Required

- Service scale: exactly one region and one replica. Runtime: one Uvicorn
  worker.
- One volume mounted at `/app/data`; Railway injects
  `RAILWAY_VOLUME_MOUNT_PATH=/app/data`. Do not set that variable manually;
  runtime startup also requires `/app/data` to be a real Linux mountpoint.
- `APP_ENV=production`
- `GEMINI_API_KEY=<Railway secret>`
- `APP_USERS=operator:<unique-generated-password>`
- `APP_SESSION_SECRET=<unique-generated-secret>`
- `SESSION_HTTPS_ONLY=true`
- `TRUST_PROXY_HEADERS=true`
- `FORWARDED_ALLOW_IPS=127.0.0.1/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.0.0.0/8,fc00::/7,::1`
- `PERSISTENT_VOLUME_PATH=/app/data`
- `ORDERS_DB_PATH=/app/data/orders.db`
- `CATALOG_BACKUP_DIR=/app/data/catalog-backups`
- `RAILWAY_RUN_UID=0`
- `EXPOSE_API_DOCS=false`

Use `docs/pilot/RAILWAY_SINGLE_MERCHANT_DEPLOYMENT.md` for the complete
operator procedure, health check, backup verification, and redeploy-persistence
verification.

# Remaining P0 Blockers

None in the repository.

The first live deployment still requires the documented operator-run synthetic
smoke and redeploy-persistence checks before merchant data is introduced. Those
checks remain mandatory even after runtime mount verification: they validate
post-restart Railway volume and service behavior, which is a different failure
class from an unmounted directory at startup. They do not represent an
unresolved code P0.
