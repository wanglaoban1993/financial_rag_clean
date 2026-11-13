# Stage 2 Operations

Stage 2 adds the first real service layer:

- FastAPI on `api:8000`
- Streamlit on `dashboard:8501`
- PostgreSQL-backed watchlist import from `csv_update`

## Start Services

```sh
docker compose up -d api dashboard
```

## Import Watchlists

```sh
docker compose run --rm api python -m server_app.import_watchlists
```

This imports all `csv_update/**/*.csv` files into:

- `app.watchlist_imports`
- `app.watchlist_rows`

The importer stores common columns such as `WKN`, `ISIN`, `Name`, `COMMENT`, `Bid`, `Ask`, and return percentages as typed columns, while preserving the full source row in `raw` JSONB.

## Local Checks

```sh
open http://127.0.0.1:8501
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/summary
```

## Cloudflare Routes

Keep the existing `stage1.financial-rag.org` route as a simple uptime check.

For the dashboard, create a new Access-protected application and tunnel public hostname:

```text
Hostname: dashboard.financial-rag.org
Service:  http://dashboard:8501
```

For the API, create a separate Access-protected hostname:

```text
Hostname: api.financial-rag.org
Service:  http://api:8000
```

Do not route PostgreSQL or model/vector services through Cloudflare.
