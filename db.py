"""Turso (cloud SQLite) storage for Carbitrage.

Uses Turso's HTTP pipeline API — works from any machine with just `requests`.
No special drivers or websocket libraries needed.

Multi-user: each user has their own watches and vehicles, but they share the
API budget (one key, one monthly counter).
"""

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests as http_requests

# --- Turso connection config ---
# Credentials MUST come from environment variables. Never hardcode secrets
# in source code — this repo is public on GitHub.
TURSO_URL = os.environ.get("TURSO_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")

SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS watches (
        watch_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL DEFAULT 1,
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
        user_id INTEGER NOT NULL DEFAULT 1,
        vin TEXT,
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
        user_id INTEGER NOT NULL DEFAULT 1,
        url TEXT NOT NULL,
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
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_vehicles_user_vin ON vehicles(user_id, vin)",
    "CREATE INDEX IF NOT EXISTS idx_vehicles_active ON vehicles(is_active)",
    "CREATE INDEX IF NOT EXISTS idx_vehicles_user ON vehicles(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_history_date ON price_history(snapshot_date)",
    "CREATE INDEX IF NOT EXISTS idx_watches_user ON watches(user_id)",
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
    """Dict-like row that also supports attribute access."""
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def keys(self):
        return super().keys()


class Database:
    def __init__(self, path: str = ""):
        if not TURSO_URL or not TURSO_TOKEN:
            raise RuntimeError(
                "TURSO_URL and TURSO_TOKEN environment variables must be set. "
                "Create a .env file (see .env.example) or set them in your "
                "shell/Replit Secrets. Never hardcode credentials in source."
            )
        self._url = TURSO_URL
        self._token = TURSO_TOKEN
        self._ensure_schema()
        self._ensure_default_users()

    def _execute_pipeline(self, statements: List) -> List[Dict]:
        """Send a batch of statements to Turso via HTTP pipeline API."""
        requests_payload = []
        for stmt in statements:
            if isinstance(stmt, str):
                requests_payload.append({"type": "execute", "stmt": {"sql": stmt}})
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
        result = self._execute(sql, params)
        rowid = result.get("last_insert_rowid")
        if rowid and isinstance(rowid, str):
            return int(rowid)
        return int(rowid) if rowid else 0

    def _ensure_schema(self):
        self._execute_pipeline(SCHEMA_STATEMENTS)

    def _ensure_default_users(self):
        """Create the two default users if they don't exist."""
        existing = self._query("SELECT username FROM users")
        names = {r["username"] for r in existing}
        if "darren" not in names:
            self._execute(
                "INSERT INTO users (username, display_name, created_at) VALUES (?, ?, ?)",
                ["darren", "Darren", datetime.now(timezone.utc).isoformat()]
            )
        if "taylor" not in names:
            self._execute(
                "INSERT INTO users (username, display_name, created_at) VALUES (?, ?, ?)",
                ["taylor", "Taylor", datetime.now(timezone.utc).isoformat()]
            )

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def get_user(self, username: str) -> Optional[Row]:
        rows = self._query("SELECT * FROM users WHERE username = ?", [username])
        return rows[0] if rows else None

    def get_users(self) -> List[Row]:
        return self._query("SELECT * FROM users ORDER BY user_id")

    # ------------------------------------------------------------------
    # API budget (shared across all users)
    # ------------------------------------------------------------------

    def month_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def calls_this_month(self) -> int:
        rows = self._query("SELECT calls FROM api_usage WHERE month = ?", [self.month_key()])
        return rows[0]["calls"] if rows else 0

    def record_call(self, n: int = 1) -> None:
        self._execute(
            "INSERT INTO api_usage (month, calls) VALUES (?, ?) "
            "ON CONFLICT(month) DO UPDATE SET calls = calls + ?",
            [self.month_key(), n, n]
        )

    def set_usage(self, month: str, calls: int) -> None:
        """Manually set usage for a month (for syncing with API provider)."""
        self._execute(
            "INSERT INTO api_usage (month, calls) VALUES (?, ?) "
            "ON CONFLICT(month) DO UPDATE SET calls = ?",
            [month, calls, calls]
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
    # Watches (per user)
    # ------------------------------------------------------------------

    def add_watch(self, user_id: int = 1, **fields) -> int:
        fields.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        fields["user_id"] = user_id
        cols = list(fields.keys())
        placeholders = ", ".join("?" for _ in cols)
        col_str = ", ".join(cols)
        return self._execute_returning_id(
            f"INSERT INTO watches ({col_str}) VALUES ({placeholders})",
            list(fields.values())
        )

    def watches(self, user_id: int = None, enabled_only: bool = False) -> List[Row]:
        sql = "SELECT * FROM watches WHERE 1=1"
        params = []
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if enabled_only:
            sql += " AND enabled = 1"
        return self._query(sql + " ORDER BY watch_id", params or None)

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
    # Vehicles (per user)
    # ------------------------------------------------------------------

    def upsert_vehicle(self, record: Dict, user_id: int = 1,
                       watch_id: Optional[int] = None,
                       source: str = "api") -> Tuple[int, bool]:
        today = date.today().isoformat()
        vin = (record.get("vin") or "").strip().upper() or None

        existing = None
        if vin:
            rows = self._query(
                "SELECT vehicle_id, price FROM vehicles WHERE vin = ? AND user_id = ?",
                [vin, user_id])
            existing = rows[0] if rows else None
        elif record.get("listing_url"):
            rows = self._query(
                "SELECT vehicle_id, price FROM vehicles WHERE listing_url = ? AND user_id = ?",
                [record["listing_url"], user_id])
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
                "(user_id, vin, year, make, model, trim, mileage, price, "
                "accepting_offers, dealer_name, city, state, listing_url, "
                "source, watch_id, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [user_id, vin, record.get("year"), record.get("make"),
                 record.get("model"), record.get("trim"),
                 record.get("mileage"), price, accepting,
                 record.get("dealer_name"), record.get("city"),
                 record.get("state"), record.get("listing_url"),
                 source, watch_id, today, today]
            )
            is_new = True

        self._execute(
            "INSERT INTO price_history (vehicle_id, snapshot_date, price, mileage) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(vehicle_id, snapshot_date) DO UPDATE SET "
            "price = excluded.price, mileage = excluded.mileage",
            [vehicle_id, today, price, record.get("mileage")]
        )
        return vehicle_id, is_new

    def upsert_vehicles_batch(self, records: List[Dict], user_id: int = 1,
                              watch_id: Optional[int] = None,
                              source: str = "api") -> Tuple[int, int]:
        """Batch upsert using pipeline."""
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

            stmts.append({
                "sql": "INSERT INTO vehicles "
                       "(user_id, vin, year, make, model, trim, mileage, price, "
                       "accepting_offers, dealer_name, city, state, listing_url, "
                       "source, watch_id, first_seen, last_seen) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(user_id, vin) DO NOTHING",
                "args": [_val(v) for v in [
                    user_id, vin, record.get("year"), record.get("make"),
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
                       "WHERE vin = ? AND user_id = ?",
                "args": [_val(v) for v in [price, accepting, record.get("mileage"), today, vin, user_id]]
            })

        if stmts:
            self._execute_pipeline(stmts)

        # Batch price history
        hist_stmts = []
        for record in records:
            vin = (record.get("vin") or "").strip().upper() or None
            if not vin:
                continue
            price = record.get("price")
            hist_stmts.append({
                "sql": "INSERT OR REPLACE INTO price_history "
                       "(vehicle_id, snapshot_date, price, mileage) "
                       "SELECT vehicle_id, ?, ?, ? FROM vehicles WHERE vin = ? AND user_id = ?",
                "args": [_val(v) for v in [today, price, record.get("mileage"), vin, user_id]]
            })

        if hist_stmts:
            self._execute_pipeline(hist_stmts)

        return len(records), 0

    def mark_sold(self, user_id: int = None) -> int:
        cutoff = (date.today() - timedelta(days=SOLD_AFTER_DAYS)).isoformat()
        sql = ("UPDATE vehicles SET is_active = 0, sold_detected = ? "
               "WHERE last_seen < ? AND is_active = 1")
        params = [date.today().isoformat(), cutoff]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        result = self._execute(sql, params)
        return result.get("affected_row_count", 0)

    def set_starred(self, vehicle_id: int, starred: bool) -> None:
        self._execute("UPDATE vehicles SET starred = ? WHERE vehicle_id = ?",
                      [int(starred), vehicle_id])

    def vehicles(self, user_id: int = None, active_only: bool = True,
                 starred_only: bool = False, watch_id: Optional[int] = None) -> List[Row]:
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
        if user_id is not None:
            sql += " AND v.user_id = ?"
            params.append(user_id)
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

    def sold_vehicles(self, user_id: int = None) -> List[Row]:
        sql = """
            SELECT v.*,
                   (SELECT price FROM price_history h
                     WHERE h.vehicle_id = v.vehicle_id AND price IS NOT NULL
                  ORDER BY snapshot_date DESC LIMIT 1) AS last_price,
                   (SELECT COUNT(*) FROM price_history h
                     WHERE h.vehicle_id = v.vehicle_id) AS days_tracked
              FROM vehicles v WHERE is_active = 0
        """
        params = []
        if user_id is not None:
            sql += " AND v.user_id = ?"
            params.append(user_id)
        sql += " ORDER BY sold_detected DESC"
        return self._query(sql, params if params else None)

    # ------------------------------------------------------------------
    # Manual URLs (per user)
    # ------------------------------------------------------------------

    def add_manual_url(self, url: str, user_id: int = 1,
                       label: str = "", vin: str = "") -> int:
        return self._execute_returning_id(
            "INSERT OR IGNORE INTO manual_urls (user_id, url, label, vin, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [user_id, url.strip(), label.strip(), vin.strip().upper(),
             datetime.now(timezone.utc).isoformat()]
        )

    def manual_urls(self, user_id: int = None, enabled_only: bool = False) -> List[Row]:
        sql = "SELECT * FROM manual_urls WHERE 1=1"
        params = []
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if enabled_only:
            sql += " AND enabled = 1"
        return self._query(sql + " ORDER BY manual_id", params or None)

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
    # Compatibility
    # ------------------------------------------------------------------
    class _Conn:
        def __init__(self, db):
            self._db = db
        def execute(self, sql, params=None):
            self._db._execute(sql, list(params) if params else None)
        def commit(self):
            pass

    @property
    def conn(self):
        return self._Conn(self)

    def close(self) -> None:
        pass
