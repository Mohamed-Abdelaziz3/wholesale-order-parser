# Pilot deployment

The only supported production topology for this assisted pilot is:

```text
one Railway project
  └─ one service
       ├─ one region and one replica
       ├─ one Uvicorn worker
       ├─ one Railway volume mounted at /app/data
       └─ one SQLite database inside that volume
```

Follow [the Railway single-merchant deployment guide](docs/pilot/RAILWAY_SINGLE_MERCHANT_DEPLOYMENT.md)
exactly. It is the authoritative procedure for service creation, credentials,
proxy configuration, the mounted-volume check, catalog recovery, and the
mandatory post-restart persistence test.

Before merchant data is entered, the deployment must have:

- `APP_ENV=production`, named `APP_USERS`, and a unique
  `APP_SESSION_SECRET` configured in Railway service variables;
- HTTPS-only session cookies, the documented Railway proxy CIDRs, and exactly
  one replica / one Uvicorn worker;
- `ORDERS_DB_PATH` and catalog backups under the mounted `/app/data` volume;
- a successful health and authentication check, plus a synthetic catalog,
  reviewed order, approval, export, backup, and post-restart persistence check.

Do not deploy this pilot on Fly.io, a generic VPS/Docker recipe, multiple
replicas or workers, or ephemeral SQLite storage. Do not use `APP_PASSWORD` in
production. The restart verification command is mandatory:

```powershell
python scripts/pilot_smoke_test.py verify-persistence `
    --base-url https://YOUR_RAILWAY_DOMAIN `
    --operator pilot-smoke `
    --checkpoint pilot-smoke-checkpoint.json `
    --confirm-restarted
```
