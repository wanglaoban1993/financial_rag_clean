import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server_app.db import ensure_schema, list_tableau_view_rows


if __name__ == "__main__":
    ensure_schema()
    rows = list_tableau_view_rows(limit=10)
    print(f"tableau_view_rows={len(rows)}")
    for row in rows:
        print(row)
