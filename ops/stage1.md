# Stage 1 Operations

This is the minimum server foundation for the personal Financial RAG server:

- Docker Compose
- PostgreSQL
- automatic `pg_dump` backups
- Cloudflare Tunnel connector, enabled only after Cloudflare Access is configured

## 1. Prepare secrets

```sh
cp .env.example .env
```

Edit `.env` and set a long random `POSTGRES_PASSWORD`.

Important: the existing `config.yaml` currently contains live-looking credentials and API keys. Rotate those keys before treating this repo as secure, especially if it has ever been pushed to a remote.

## 2. Start PostgreSQL and backups

```sh
docker compose up -d postgres postgres-backup stage1-check
docker compose ps
```

PostgreSQL is bound to `127.0.0.1:${POSTGRES_PORT:-5432}` only. It is not exposed on the LAN or the public Internet.
The `stage1-check` service is an internal-only test page for Cloudflare Access/Tunnel verification.

## 3. Manual backup and restore

Create a one-off backup:

```sh
scripts/backup_postgres.sh
```

Restore a backup:

```sh
scripts/restore_postgres.sh backups/postgres/<file>.dump
```

The restore command cleans existing database objects before restoring. Use it only when you intentionally want to roll the database back.

## 4. Cloudflare Access first

Before starting `cloudflared`, create the Cloudflare Access self-hosted application and an allow policy for your email or identity provider.

Recommended order:

1. Cloudflare Zero Trust: create or confirm your identity provider.
2. Access controls > Applications: add a self-hosted app for the future dashboard/API hostname.
3. Add an Allow policy for only your account at first.
4. Networks > Tunnels: create a tunnel for this Mac mini.
5. Copy the connector token into `CLOUDFLARED_TUNNEL_TOKEN` in `.env`.
6. Add a temporary published application route to `http://stage1-check:80`.
7. Later, replace that route with `http://dashboard:8501` or `http://api:8000` once phase 2 exists.

Cloudflare warns that if you publish a tunnel route without an Access app, the app can be reachable by anyone on the Internet. Keep Access in place before adding public routes.

## 5. Start the tunnel connector

```sh
docker compose up -d stage1-check
docker compose --profile tunnel up -d cloudflared
docker compose logs -f cloudflared
```

Do not create tunnel routes for PostgreSQL, Qdrant, Ollama, Redis, or any other internal service. Only route dashboard/API hostnames that sit behind Cloudflare Access.

For the phase-2 dashboard, the Cloudflare Tunnel service URL should be:

```text
http://dashboard:8501
```

For the API, use a separate Access-protected hostname and route it to:

```text
http://api:8000
```

## 6. Local checks

```sh
set -a
source .env
set +a
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select now();"
docker compose logs --tail=50 postgres-backup
docker compose ps stage1-check
```

## References

- Cloudflare Tunnel: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/
- Create a tunnel from the dashboard: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/
- Cloudflare Access self-hosted applications: https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/
