# Railway single-merchant deployment

This is the only approved production topology for the assisted pilot:

```text
one Railway project
  └─ one web service
       ├─ one replica / one region
       ├─ one SQLite database: /app/data/orders.db
       └─ one Railway Volume: /app/data
```

Do not add a second merchant, a second service, a second database, a second
replica, or a worker to this deployment. A separate merchant requires a new
Railway project, service, volume, database, credentials, and session secret.

## 1. Create the project and service

1. In Railway, create an **Empty Project** for this merchant only.
2. Add exactly one service from the repository branch containing this file,
   `Dockerfile`, `railway.json`, `app/runtime_config.py`, and the pilot changes.
   Confirm the deployment log reports the intended Git commit SHA.
3. In **Service → Settings → Scale**, keep one region with exactly **one
   replica**. Do not increase it. Railway volumes cannot be used with replicas;
   this setting is an operational invariant as well as a SQLite requirement.
4. In **Service → Settings → Volumes**, attach exactly one volume. Set its mount
   path to **`/app/data`**. Do not mount a volume during the image build; Railway
   mounts it only at runtime.

Railway supplies `RAILWAY_VOLUME_MOUNT_PATH` after the volume is attached. Do
not create or set that variable manually. The application compares it with the
explicit pilot setting below, then on Linux reads its current
`/proc/self/mountinfo` and requires `/app/data` to be an actual mountpoint. A
matching path string or an ordinary `/app/data` directory in the container
image is not accepted as proof of persistence.

This **runtime mount verification** proves only that the configured path is a
mountpoint in the running container's mount namespace. It does not prove that
data will survive a later redeploy, a volume replacement, or an operator
mistake outside the process. The separate post-restart verification in section
5 remains mandatory before merchant data is introduced.

## 2. Set service variables

In **Service → Variables**, set these values for the production environment.
Do not put them in Git, a project-folder `.env`, tickets, screenshots, or chat.

| Variable | Required value / rule |
| --- | --- |
| `APP_ENV` | `production` |
| `GEMINI_API_KEY` | A real Gemini API key, stored as a Railway secret |
| `APP_USERS` | One or more named accounts, each with a unique generated password: `operator:<generated-password>` |
| `APP_SESSION_SECRET` | A distinct generated secret for this deployment, at least 32 characters |
| `SESSION_HTTPS_ONLY` | `true` |
| `TRUST_PROXY_HEADERS` | `true` |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.0.0.0/8,fc00::/7,::1` |
| `PERSISTENT_VOLUME_PATH` | `/app/data` |
| `ORDERS_DB_PATH` | `/app/data/orders.db` |
| `CATALOG_BACKUP_DIR` | `/app/data/catalog-backups` |
| `RAILWAY_RUN_UID` | `0` |
| `EXPOSE_API_DOCS` | `false` |

`RAILWAY_RUN_UID=0` is required because Railway mounts volumes as root while the
image normally runs as an unprivileged user. It is limited to this isolated
single-service pilot, and is not a reason to grant shell access to operators.

The forwarded-IP list uses Railway's documented private proxy ranges plus
`100.0.0.0/8`. Never use `*`, `0.0.0.0/0`, or `::/0`: doing so makes attacker
supplied forwarding headers trustworthy.

Generate every credential locally, once per merchant, and paste the result only
into Railway Variables:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(24))"  # one APP_USERS password
python -c "import secrets; print(secrets.token_urlsafe(48))"  # APP_SESSION_SECRET
```

Passwords cannot contain a comma because `APP_USERS` uses commas to separate
accounts. Do not set `APP_PASSWORD` in production.

After Railway generates the public HTTPS domain, set `ALLOWED_ORIGINS` to its
exact origin if an additional origin is required. The app's own public origin is
allowed automatically; do not add wildcard origins.

## 3. Deploy and validate startup

Deploy the service. A production process must fail its deployment rather than
serve traffic if a secret, named account, HTTPS-only cookie setting, proxy
network, actual Linux volume mount, persistent-volume path, or database path
is absent or unsafe.

Verify the public health endpoint without credentials:

```powershell
curl.exe -fsS https://YOUR_RAILWAY_DOMAIN/api/health
```

It must return a successful JSON health response. Then verify that an anonymous
request to `/api/orders` returns `401`, and that the sign-in session cookie is
marked `Secure` in the browser's developer tools.

## 4. Verify the mounted data and backups

Before loading merchant data, upload a small synthetic catalog, create and
approve one synthetic order, and export it. Confirm these paths exist through
Railway's volume file browser or an operator shell:

```text
/app/data/orders.db
/app/data/catalog-backups/
```

Replace the synthetic catalog once. Confirm a new timestamped catalog backup is
present under `/app/data/catalog-backups/`; retain at most the configured pilot
limit (10 by default). Catalog backups are catalog-only recovery points, so a
restore must not remove approved order history.

List the backup IDs with:

```powershell
python -m app.catalog_maintenance list-backups --db /app/data/orders.db
```

To restore, stop the service first, run this command against the mounted volume
with the authenticated operator name, then start the same one-replica service:

```powershell
python -m app.catalog_maintenance restore --db /app/data/orders.db --backup-id BACKUP_ID --actor OPERATOR_NAME --confirm-service-stopped
```

After startup, verify the previous catalog and approved synthetic order. The
restore only replaces catalog content; it must not remove approved order history.

## 5. Mandatory post-restart persistence verification

Complete this procedure even after runtime mount verification passes. Runtime
verification catches an unmounted image directory at startup; this synthetic
restart/redeploy check verifies that Railway still presents the same data after
the service lifecycle event.

After the synthetic `scripts/pilot_smoke_test.py run` stage and an operator
restart or redeploy, this command remains required before merchant data:

```powershell
python scripts/pilot_smoke_test.py verify-persistence `
    --base-url https://YOUR_RAILWAY_DOMAIN `
    --operator pilot-smoke `
    --checkpoint pilot-smoke-checkpoint.json `
    --confirm-restarted
```

1. Record the synthetic order ID, its approved/exported status, and the catalog
   version shown by the application.
2. Redeploy the **same service** without detaching, replacing, or wiping the
   volume.
3. Sign in again and verify the catalog, approved order, immutable snapshot, and
   export are still present.
4. Confirm `RAILWAY_VOLUME_MOUNT_PATH` remains `/app/data` and that there is
   still one volume and one replica.

Do not use Railway's **Wipe Volume**, detach the volume, or create a replacement
service during this verification. Those actions can make pilot data unrecoverable
without a separately verified Railway volume backup.

## 6. Operating boundaries

The service accepts manually pasted order text only. It has no WhatsApp Business
API, ERP, ETA, tax invoice, inventory, warehouse, multi-tenant, or first-class
variant capability. Every order line requires human review before approval.

Reference: [Railway volume documentation](https://docs.railway.com/volumes) and
[Railway volume reference](https://docs.railway.com/volumes/reference).
