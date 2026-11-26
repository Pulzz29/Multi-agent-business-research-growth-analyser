# Deploying InsightIQ (prepared, not executed)

Nothing in this folder has been applied to any cloud resource. Each step below needs separate approval.

## Images
```powershell
docker build -t <REGISTRY>/insightiq-api:<RELEASE> .
docker build -t <REGISTRY>/insightiq-web:<RELEASE> frontend
docker push <REGISTRY>/insightiq-api:<RELEASE>; docker push <REGISTRY>/insightiq-web:<RELEASE>
```
**Local check:** `docker compose --env-file .env up --build`, then open http://localhost:8080. This runs its own PostgreSQL in a Docker volume.

## Prerequisites
- **A PostgreSQL-compatible server reachable from the cluster:**
  - TLS with a trusted certificate (`DB_SSLMODE=verify-full`, `DB_SSLROOTCERT=system`, or a mounted CA bundle).
  - A login with CONNECT and CREATE on the schema.
  - CREATEDB if the backup CronJob verifies restores.
- **Data:** imported once from a machine that has the current data:
  - `scripts\setup_postgres.py`;
  - `scripts\migrate_sqlite_to_postgres.py`;
  - `scripts\migrate_files_to_postgres.py`.

  Run them with `DATABASE_URL` pointing at the new server.
- **Secret `insightiq-secrets`** (template: `aks/secret.example.yaml`), ideally provided by Key Vault.
- **Ingress:**
  - the NGINX ingress controller and cert-manager, or an equivalent;
  - DNS for `<HOSTNAME>`;
  - `TRUSTED_PROXY_IPS` set to the ingress pod CIDR.

## Deploy a release
1. Take a verified backup: `python scripts/backup_postgres.py` (or trigger the CronJob).
2. Fill in the placeholders in `aks/insightiq.yaml`, then run `kubectl apply -f deploy/aks/insightiq.yaml`.
3. Wait for the migration Job: `kubectl -n insightiq wait --for=condition=complete job/insightiq-migrate-<RELEASE>`.
4. Check the rollout: `kubectl -n insightiq rollout status deploy/insightiq-api deploy/insightiq-web`.
5. Create the login accounts (passwords are prompted with hidden input and
   stored only as scrypt hashes; existing accounts are never changed):
   `kubectl -n insightiq exec -it deploy/insightiq-api -- python scripts/provision_users.py <email> [<email> ...]`.
   Rotate a password with `--reset` (this also signs that user out).
6. Smoke test: `/health/live`, then sign in with email and password and open one company.
6. Check observability: Activity → Operations shows the new traces and no new alerts.

Migrations are additive and idempotent, so the previous release keeps working with the new schema.

## Rollback
- **Application:**
  - `kubectl -n insightiq rollout undo deploy/insightiq-api deploy/insightiq-web`, or re-apply the previous `<RELEASE>` tags.
  - No schema rollback is needed: migrations only add tables and columns.
- **Data (only if data was damaged):**
  1. Restore the last verified dump into a new database: `pg_restore -d <new_db> <dump>`.
  2. Point `DB_NAME` or `DATABASE_URL` at it.
  3. Restart the API.
- **Local SQLite fallback** (development only): `DB_BACKEND=sqlite`.

## Scaling notes
- **API replicas: 1.** Research jobs and their cancellation run inside the API process. Rate limits are also per process.
  - The cache lease, migrations and cleanup are already safe with several replicas.
  - Scaling out also needs an external job runner, which is out of scope.
- **Connection budget:** keep `DB_POOL_MAX_SIZE` × replicas, plus 2 for jobs, below the server's connection limit.
