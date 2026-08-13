"""Turso (cloud SQLite) storage for Carbitrage.

Uses Turso's HTTP pipeline API — works from any machine with just `requests`.
No special drivers or websocket libraries needed.

Tables:
    watches         saved search definitions (make/model/year/price/radius)
    vehicles        one row per VIN (or per manual URL when no VIN is known)
    price_history   daily snapshot per vehicle, the core arbitrage asset
    manual_urls     listings tracked by URL because the API does not carry them
    api_usage       calls per month, drives the budget gauge and hard stop
"""

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests as http_requests

# --- Turso connection config ---
TURSO_URL = os.environ.get(
    "TURSO_URL",
    "https://carbitrage-taylord50.aws-us-west-2.turso.io"
)
TURSO_TOKEN = os.environ.get(
    "TURSO_TOKEN",
    "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODY2NjIyNDUsImlkIjoiMDE5ZmZkNWQtODYwMS03YTk3LTk1N2EtYjk4YzRlMzhjZmZhIiwia2lkIjoiUHBLSDhiUGdfaV90Q1FhMUlldDUtZkpvMGhHOW4tODZHdTUwcUZYd2s5dyIsInJpZCI6IjI1NGIxNTRmLWM5MjQtNDI3Zi04ODEzLWE1YWI2ZDliMzE1ZSJ9.NBVqGaISA9D8O55hsqHnydRC2pl9F-v1em4eTB9_jvBmLrLaeODZO4iZh0lSUDn8BYQvIa-BzNobrq167F3eBQ"
)

SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS watches (
        watch_id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL,
        make TEXT NOT NULL,
        model TEXT DEFAULT '',
        trim_contains TEXT DEFAULT '',
        year_min INTEGER,
        year_max INTEGER,
        price_max INTEGER,
        price_min INTEGER,
        mileage_max INTEGER,
        zip_code TEXT DEFAULT '',
        radius INTEGER DEFAULT 0,
        enabled INTEGER DEFAULT 1,
        created_at TEXT,
        last_run TEXT,
        last_count INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS vehicles (
        vehicle_id INTEGER PRIMARY KEY AUTOINCREMENT,
        vin TEXT UNIQUE,
        year INTEGER,
        make TEXT,
        model TEXT,
        trim TEXT,
        mileage INTEGER,
        price INTEGER,
        accepting_offers INTEGER DEFAULT 0,
        dealer_name TEXT,
        city TEXT,
        state TEXT,
        listing_url TEXT,
        source TEXT DEFAULT 'api',
        watch_id INTEGER,
        starred INTEGER DEFAULT 0,
        first_seen TEXT,
        last_seen TEXT,
        is_active INTEGER DEFAULT 1,
        sold_detected TEXT,
        notes TEXT DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS price_history (
        vehicle_id INTEGER NOT NULL,
        snapshot_date TEXT NOT NULL,
        price INTEGER,
        mileage INTEGER,
        PRIMARY KEY (vehicle_id, snapshot_date)
    )""",
    """CREATE TABLE IF NOT EXISTS manual_urls (
        manual_id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT UNIQUE NOT NULL,
        label TEXT DEFAULT '',
        vin TEXT DEFAULT '',
        enabled INTEGER DEFAULT 1,
        created_at TEXT,
        last_checked TEXT,
        last_status TEXT,
        vehicle_id INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS api_usage (
        month TEXT PRIMARY KEY,
        calls INTEGER DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_vehicles_vin ON vehicles(vin)",
    "CREATE INDEX IF NOT EXISTS idx_vehicles_active ON vehicles(is_active)",
    "CREATE INDEX IF NOT EXISTS idx_history_date ON price_history(snapshot_date)",
]

SOLD_AFTER_DAYS = 7


def _val(v: Any) -> Dict:
    """Convert a Python value to a Turso API value object."""
    if v is None:
        return {"type": "null"}
    elif isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    elif isinstance(v, float):
        return {"type": "float", "value": v}
    else:
        return {"type": "text", "value": str(v)}


def _from_val(v: Dict) -> Any:
    """Convert a Turso API value object back to Python."""
    if v["type"] == "null":
        return None
    elif v["type"] == "integer":
        return int(v["value"])
    elif v["type"] == "float":
        return float(v["value"])
    else:
        return v["value"]


class Row(dict):
    """Dict-like row that also supports attribute access and index by column name."""
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def keys(self):
        return super().keys()


class Database:
    def __init__(self, path: str = ""):
        """Path is ignored — we always connect to Turso cloud."""
        self._url = TURSO_URL
        self._token = TURSO_TOKEN
        self._ensure_schema()

    def _execute_pipeline(self, statements: List[Dict]) -> List[Dict]:
        """Send a batch of statements to Turso via HTTP pipeline API."""
        requests_payload = []
        for stmt in statements:
            if isinstance(stmt, str):
                requests_payload.append({
                    "type": "execute",
                    "stmt": {"sql": stmt}
                })
            elif isinstance(stmt, dict) and "sql" in stmt:
                requests_payload.append({"type": "execute", "stmt": stmt})
            else:
                requests_payload.append({"type": "execute", "stmt": stmt})
        requests_payload.append({"type": "close"})

        resp = http_requests.post(
            f"{self._url}/v2/pipeline",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            json={"requests": requests_payload},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    def _execute(self, sql: str, params: List = None) -> Dict:
        """Execute a single SQL statement. Returns the result dict."""
        stmt: Dict[str, Any] = {"sql": sql}
        if params:
            stmt["args"] = [_val(p) for p in params]

        results = self._execute_pipeline([stmt])
        if results and results[0].get("type") == "ok":
            return results[0]["response"]["result"]
        elif results and results[0].get("type") == "error":
            raise RuntimeError(f"Turso error: {results[0].get('error', {}).get('message', 'unknown')}")
        return {"cols": [], "rows": [], "affected_row_count": 0, "last_insert_rowid": None}

    def _query(self, sql: str, params: List = None) -> List[Row]:
        """Execute a SELECT and return list of Row dicts."""
        result = self._execute(sql, params)
        cols = [c["name"] for c in result.get("cols", [])]
        rows = []
        for raw_row in result.get("rows", []):
            row = Row()
            for i, col in enumerate(cols):
                row[col] = _from_val(raw_row[i])
            rows.append(row)
        return rows

    def _execute_returning_id(self, sql: str, params: List = None) -> int:
        """Execute an INSERT and return last_insert_rowid."""
        result = self._execute(sql, params)
        rowid = result.get("last_insert_rowid")
        if rowid and isinstance(rowid, str):
            return int(rowid)
        return int(rowid) if rowid else 0

    def _ensure_schema(self):
        """Create tables if they don't exist."""
        self._execute_pipeline(SCHEMA_STATEMENTS)

    # ------------------------------------------------------------------
    # API budget
    # ------------------------------------------------------------------

    def month_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def calls_this_month(self) -> int:
        rows = self._query(
            "SELECT calls FROM api_usage WHERE month = ?",
            [self.month_key()]
        )
        return rows[0]["calls"] if rows else 0

    def record_call(self, n: int = 1) -> None:
        self._execute(
            "INSERT INTO api_usage (month, calls) VALUES (?, ?) "
            "ON CONFLICT(month) DO UPDATE SET calls = calls + ?",
            [self.month_key(), n, n]
        )

    def usage_gauge(self, budget: int) -> Dict:
        used = self.calls_this_month()
        today = date.today()
        if today.month == 12:
            next_month = date(today.year + 1, 1, 1)
        else:
            next_month = date(today.year, today.month + 1, 1)
        days_left = (next_month - today).days
        remaining = max(budget - used, 0)
        return {
            "used": used,
            "budget": budget,
            "remaining": remaining,
            "pct": round(used / budget * 100, 1) if budget else 0,
            "days_left": days_left,
            "daily_allowance": remaining // days_left if days_left else remaining,
            "exhausted": used >= budget,
        }

    # ------------------------------------------------------------------
    # Watches
    # ------------------------------------------------------------------

    def add_watch(self, **fields) -> int:
        fields.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        cols = list(fields.keys())
        placeholders = ", ".join("?" for _ in cols)
        col_str = ", ".join(cols)
        return self._execute_returning_id(
            f"INSERT INTO watches ({col_str}) VALUES ({placeholders})",
            list(fields.values())
        )

    def watches(self, enabled_only: bool = False) -> List[Row]:
        sql = "SELECT * FROM watches"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return self._query(sql + " ORDER BY watch_id")

    def set_watch_enabled(self, watch_id: int, enabled: bool) -> None:
        self._execute("UPDATE watches SET enabled = ? WHERE watch_id = ?",
                      [int(enabled), watch_id])

    def delete_watch(self, watch_id: int) -> None:
        self._execute("DELETE FROM watches WHERE watch_id = ?", [watch_id])

    def touch_watch(self, watch_id: int, count: int) -> None:
        self._execute(
            "UPDATE watches SET last_run = ?, last_count = ? WHERE watch_id = ?",
            [datetime.now(timezone.utc).isoformat(), count, watch_id]
        )

    # ------------------------------------------------------------------
    # Vehicles
    # ------------------------------------------------------------------

    def upsert_vehicle(self, record: Dict, watch_id: Optional[int] = None,
                       source: str = "api") -> Tuple[int, bool]:
        today = date.today().isoformat()
        vin = (record.get("vin") or "").strip().upper() or None

        existing = None
        if vin:
            rows = self._query("SELECT vehicle_id, price FROM vehicles WHERE vin = ?", [vin])
            existing = rows[0] if rows else None
        elif record.get("listing_url"):
            rows = self._query("SELECT vehicle_id, price FROM vehicles WHERE listing_url = ?",
                               [record["listing_url"]])
            existing = rows[0] if rows else None

        price = record.get("price")
        accepting = 1 if record.get("accepting_offers") else 0

        if existing:
            vehicle_id = existing["vehicle_id"]
            self._execute(
                "UPDATE vehicles SET price = COALESCE(?, price), "
                "accepting_offers = ?, mileage = COALESCE(?, mileage), "
                "last_seen = ?, is_active = 1, sold_detected = NULL "
                "WHERE vehicle_id = ?",
                [price, accepting, record.get("mileage"), today, vehicle_id]
            )
            is_new = False
        else:
            vehicle_id = self._execute_returning_id(
                "INSERT INTO vehicles "
                "(vin, year, make, model, trim, mileage, price, "
                "accepting_offers, dealer_name, city, state, listing_url, "
                "source, watch_id, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [vin, record.get("year"), record.get("make"),
                 record.get("model"), record.get("trim"),
                 record.get("mileage"), price, accepting,
                 record.get("dealer_name"), record.get("city"),
                 record.get("state"), record.get("listing_url"),
                 source, watch_id, today, today]
            )
            is_new = True

        # Daily snapshot
        self._execute(
            "INSERT INTO price_history (vehicle_id, snapshot_date, price, mileage) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(vehicle_id, snapshot_date) DO UPDATE SET "
            "price = excluded.price, mileage = excluded.mileage",
            [vehicle_id, today, price, record.get("mileage")]
        )
        return vehicle_id, is_new

    def upsert_vehicles_batch(self, records: List[Dict],
                              watch_id: Optional[int] = None,
                              source: str = "api") -> Tuple[int, int]:
        """Batch upsert using pipeline — much faster over network.
        Returns (total_processed, new_count_estimate)."""
        if not records:
            return 0, 0
        today = date.today().isoformat()
        stmts = []

        for record in records:
            vin = (record.get("vin") or "").strip().upper() or None
            if not vin:
                continue
            price = record.get("price")
            accepting = 1 if record.get("accepting_offers") else 0

            # INSERT OR IGNORE (new vehicles) + UPDATE (refresh existing)
            stmts.append({
                "sql": "INSERT OR IGNORE INTO vehicles "
                       "(vin, year, make, model, trim, mileage, price, "
                       "accepting_offers, dealer_name, city, state, listing_url, "
                       "source, watch_id, first_seen, last_seen) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                "args": [_val(v) for v in [
                    vin, record.get("year"), record.get("make"),
                    record.get("model"), record.get("trim"),
                    record.get("mileage"), price, accepting,
                    record.get("dealer_name"), record.get("city"),
                    record.get("state"), record.get("listing_url"),
                    source, watch_id, today, today
                ]]
            })
            stmts.append({
                "sql": "UPDATE vehicles SET price = COALESCE(?, price), "
                       "accepting_offers = ?, mileage = COALESCE(?, mileage), "
                       "last_seen = ?, is_active = 1, sold_detected = NULL "
                       "WHERE vin = ?",
                "args": [_val(v) for v in [price, accepting, record.get("mileage"), today, vin]]
            })

        # Send all inserts+updates in one pipeline call
        if stmts:
            self._execute_pipeline(stmts)

        # Batch price history in a second pipeline
        hist_stmts = []
        for record in records:
            vin = (record.get("vin") or "").strip().upper() or None
            if not vin:
                continue
            price = record.get("price")
            hist_stmts.append({
                "sql": "INSERT OR REPLACE INTO price_history "
                       "(vehicle_id, snapshot_date, price, mileage) "
                       "SELECT vehicle_id, ?, ?, ? FROM vehicles WHERE vin = ?",
                "args": [_val(v) for v in [today, price, record.get("mileage"), vin]]
            })

        if hist_stmts:
            self._execute_pipeline(hist_stmts)

        return len(records), 0

    def mark_sold(self) -> int:
        cutoff = (date.today() - timedelta(days=SOLD_AFTER_DAYS)).isoformat()
        result = self._execute(
            "UPDATE vehicles SET is_active = 0, sold_detected = ? "
            "WHERE last_seen < ? AND is_active = 1",
            [date.today().isoformat(), cutoff]
        )
        return result.get("affected_row_count", 0)

    def set_starred(self, vehicle_id: int, starred: bool) -> None:
        self._execute("UPDATE vehicles SET starred = ? WHERE vehicle_id = ?",
                      [int(starred), vehicle_id])

    def vehicles(self, active_only: bool = True, starred_only: bool = False,
                 watch_id: Optional[int] = None) -> List[Row]:
        sql = """
            SELECT v.*,
                   (SELECT COUNT(*) FROM price_history h
                     WHERE h.vehicle_id = v.vehicle_id) AS days_tracked,
                   (SELECT MIN(price) FROM price_history h
                     WHERE h.vehicle_id = v.vehicle_id
                       AND price IS NOT NULL) AS lowest_price,
                   (SELECT price FROM price_history h
                     WHERE h.vehicle_id = v.vehicle_id AND price IS NOT NULL
                  ORDER BY snapshot_date ASC LIMIT 1) AS first_price
              FROM vehicles v WHERE 1=1
        """
        params: List = []
        if active_only:
            sql += " AND v.is_active = 1"
        if starred_only:
            sql += " AND v.starred = 1"
        if watch_id:
            sql += " AND v.watch_id = ?"
            params.append(watch_id)
        sql += " ORDER BY v.starred DESC, v.price ASC NULLS LAST"
        return self._query(sql, params if params else None)

    def price_series(self, vehicle_id: int) -> List[Row]:
        return self._query(
            "SELECT snapshot_date, price, mileage FROM price_history "
            "WHERE vehicle_id = ? ORDER BY snapshot_date",
            [vehicle_id]
        )

    def sold_vehicles(self) -> List[Row]:
        return self._query("""
            SELECT v.*,
                   (SELECT price FROM price_history h
                     WHERE h.vehicle_id = v.vehicle_id AND price IS NOT NULL
                  ORDER BY snapshot_date DESC LIMIT 1) AS last_price,
                   (SELECT COUNT(*) FROM price_history h
                     WHERE h.vehicle_id = v.vehicle_id) AS days_tracked
              FROM vehicles v
             WHERE is_active = 0
          ORDER BY sold_detected DESC
        """)

    # ------------------------------------------------------------------
    # Manual URLs
    # ------------------------------------------------------------------

    def add_manual_url(self, url: str, label: str = "", vin: str = "") -> int:
        return self._execute_returning_id(
            "INSERT OR IGNORE INTO manual_urls (url, label, vin, created_at) "
            "VALUES (?, ?, ?, ?)",
            [url.strip(), label.strip(), vin.strip().upper(),
             datetime.now(timezone.utc).isoformat()]
        )

    def manual_urls(self, enabled_only: bool = False) -> List[Row]:
        sql = "SELECT * FROM manual_urls"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return self._query(sql + " ORDER BY manual_id")

    def update_manual(self, manual_id: int, status: str,
                      vehicle_id: Optional[int] = None) -> None:
        self._execute(
            "UPDATE manual_urls SET last_checked = ?, last_status = ?, "
            "vehicle_id = COALESCE(?, vehicle_id) WHERE manual_id = ?",
            [datetime.now(timezone.utc).isoformat(), status, vehicle_id, manual_id]
        )

    def delete_manual_url(self, manual_id: int) -> None:
        self._execute("DELETE FROM manual_urls WHERE manual_id = ?", [manual_id])

    # ------------------------------------------------------------------
    # For indexer compatibility — direct SQL execution on sold detection
    # ------------------------------------------------------------------
    class _Conn:
        def __init__(self, db):
            self._db = db
        def execute(self, sql, params=None):
            self._db._execute(sql, list(params) if params else None)
        def commit(self):
            pass  # Turso auto-commits

    @property
    def conn(self):
        return self._Conn(self)

    def close(self) -> None:
        pass  # No persistent connection to close
