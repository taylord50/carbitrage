"""SQLite storage for Carbitrage.

Tables:
    watches         saved search definitions (make/model/year/price/radius)
    vehicles        one row per VIN (or per manual URL when no VIN is known)
    price_history   daily snapshot per vehicle, the core arbitrage asset
    manual_urls     listings tracked by URL because the API does not carry them
    api_usage       calls per month, drives the budget gauge and hard stop
"""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    watch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    make TEXT NOT NULL,
    model TEXT DEFAULT '',
    trim_contains TEXT DEFAULT '',      -- substring filter on trim field
    year_min INTEGER,
    year_max INTEGER,
    price_max INTEGER,
    price_min INTEGER,
    mileage_max INTEGER,
    zip_code TEXT DEFAULT '',
    radius INTEGER DEFAULT 0,          -- 0 means nationwide
    enabled INTEGER DEFAULT 1,
    created_at TEXT,
    last_run TEXT,
    last_count INTEGER
);

CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id INTEGER PRIMARY KEY AUTOINCREMENT,
    vin TEXT UNIQUE,                    -- may be NULL for manual URLs
    year INTEGER,
    make TEXT,
    model TEXT,
    trim TEXT,
    mileage INTEGER,
    price INTEGER,                      -- NULL means "accepting offers" / unlisted
    accepting_offers INTEGER DEFAULT 0,
    dealer_name TEXT,
    city TEXT,
    state TEXT,
    listing_url TEXT,
    source TEXT DEFAULT 'api',          -- api | manual
    watch_id INTEGER REFERENCES watches(watch_id),
    starred INTEGER DEFAULT 0,
    first_seen TEXT,
    last_seen TEXT,
    is_active INTEGER DEFAULT 1,
    sold_detected TEXT,                 -- date we noticed it disappeared
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS price_history (
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(vehicle_id),
    snapshot_date TEXT NOT NULL,
    price INTEGER,
    mileage INTEGER,
    PRIMARY KEY (vehicle_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS manual_urls (
    manual_id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    label TEXT DEFAULT '',
    vin TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    created_at TEXT,
    last_checked TEXT,
    last_status TEXT,                   -- ok | gone | error
    vehicle_id INTEGER REFERENCES vehicles(vehicle_id)
);

CREATE TABLE IF NOT EXISTS api_usage (
    month TEXT PRIMARY KEY,             -- YYYY-MM
    calls INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_vehicles_vin ON vehicles(vin);
CREATE INDEX IF NOT EXISTS idx_vehicles_active ON vehicles(is_active);
CREATE INDEX IF NOT EXISTS idx_history_date ON price_history(snapshot_date);
"""

#: A vehicle unseen for this many days is presumed sold/delisted.
SOLD_AFTER_DAYS = 7


class Database:
    def __init__(self, path: str = "carbitrage.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive migrations for existing databases."""
        cols = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(watches)").fetchall()}
        if "trim_contains" not in cols:
            self.conn.execute(
                "ALTER TABLE watches ADD COLUMN trim_contains TEXT DEFAULT ''")
            self.conn.commit()

    # ------------------------------------------------------------------
    # API budget
    # ------------------------------------------------------------------

    def month_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def calls_this_month(self) -> int:
        row = self.conn.execute(
            "SELECT calls FROM api_usage WHERE month = ?", (self.month_key(),)
        ).fetchone()
        return row["calls"] if row else 0

    def record_call(self, n: int = 1) -> None:
        self.conn.execute(
            """
            INSERT INTO api_usage (month, calls) VALUES (?, ?)
            ON CONFLICT(month) DO UPDATE SET calls = calls + ?
            """,
            (self.month_key(), n, n),
        )
        self.conn.commit()

    def usage_gauge(self, budget: int) -> Dict:
        """Everything the UI needs to render the gas gauge."""
        used = self.calls_this_month()
        today = date.today()
        # Days remaining in this calendar month, inclusive of today.
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
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cursor = self.conn.execute(
            f"INSERT INTO watches ({cols}) VALUES ({marks})",
            list(fields.values()),
        )
        self.conn.commit()
        return cursor.lastrowid

    def watches(self, enabled_only: bool = False) -> List[sqlite3.Row]:
        sql = "SELECT * FROM watches"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return self.conn.execute(sql + " ORDER BY watch_id").fetchall()

    def set_watch_enabled(self, watch_id: int, enabled: bool) -> None:
        self.conn.execute("UPDATE watches SET enabled = ? WHERE watch_id = ?",
                          (int(enabled), watch_id))
        self.conn.commit()

    def delete_watch(self, watch_id: int) -> None:
        self.conn.execute("DELETE FROM watches WHERE watch_id = ?", (watch_id,))
        self.conn.commit()

    def touch_watch(self, watch_id: int, count: int) -> None:
        self.conn.execute(
            "UPDATE watches SET last_run = ?, last_count = ? WHERE watch_id = ?",
            (datetime.now(timezone.utc).isoformat(), count, watch_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Vehicles
    # ------------------------------------------------------------------

    def upsert_vehicle(self, record: Dict, watch_id: Optional[int] = None,
                       source: str = "api") -> Tuple[int, bool]:
        """Insert or refresh a vehicle. Returns (vehicle_id, is_new)."""
        today = date.today().isoformat()
        vin = (record.get("vin") or "").strip().upper() or None

        existing = None
        if vin:
            existing = self.conn.execute(
                "SELECT vehicle_id, price FROM vehicles WHERE vin = ?", (vin,)
            ).fetchone()
        elif record.get("listing_url"):
            existing = self.conn.execute(
                "SELECT vehicle_id, price FROM vehicles WHERE listing_url = ?",
                (record["listing_url"],),
            ).fetchone()

        price = record.get("price")
        accepting = 1 if record.get("accepting_offers") else 0

        if existing:
            vehicle_id = existing["vehicle_id"]
            self.conn.execute(
                """
                UPDATE vehicles
                   SET price = COALESCE(?, price),
                       accepting_offers = ?,
                       mileage = COALESCE(?, mileage),
                       last_seen = ?, is_active = 1, sold_detected = NULL
                 WHERE vehicle_id = ?
                """,
                (price, accepting, record.get("mileage"), today, vehicle_id),
            )
            is_new = False
        else:
            cursor = self.conn.execute(
                """
                INSERT INTO vehicles
                    (vin, year, make, model, trim, mileage, price,
                     accepting_offers, dealer_name, city, state, listing_url,
                     source, watch_id, first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (vin, record.get("year"), record.get("make"),
                 record.get("model"), record.get("trim"),
                 record.get("mileage"), price, accepting,
                 record.get("dealer_name"), record.get("city"),
                 record.get("state"), record.get("listing_url"),
                 source, watch_id, today, today),
            )
            vehicle_id = cursor.lastrowid
            is_new = True

        # Daily snapshot. NULL price is still recorded so days-on-market works
        # for accepting-offers exotics.
        self.conn.execute(
            """
            INSERT INTO price_history (vehicle_id, snapshot_date, price, mileage)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(vehicle_id, snapshot_date) DO UPDATE SET
                price = excluded.price, mileage = excluded.mileage
            """,
            (vehicle_id, today, price, record.get("mileage")),
        )
        self.conn.commit()
        return vehicle_id, is_new

    def mark_sold(self) -> int:
        """Vehicles unseen for SOLD_AFTER_DAYS are presumed sold/delisted."""
        cutoff = (date.today() - timedelta(days=SOLD_AFTER_DAYS)).isoformat()
        cursor = self.conn.execute(
            """
            UPDATE vehicles
               SET is_active = 0, sold_detected = ?
             WHERE last_seen < ? AND is_active = 1
            """,
            (date.today().isoformat(), cutoff),
        )
        self.conn.commit()
        return cursor.rowcount

    def set_starred(self, vehicle_id: int, starred: bool) -> None:
        self.conn.execute(
            "UPDATE vehicles SET starred = ? WHERE vehicle_id = ?",
            (int(starred), vehicle_id),
        )
        self.conn.commit()

    def vehicles(self, active_only: bool = True, starred_only: bool = False,
                 watch_id: Optional[int] = None) -> List[sqlite3.Row]:
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
        return self.conn.execute(sql, params).fetchall()

    def price_series(self, vehicle_id: int) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT snapshot_date, price, mileage FROM price_history "
            "WHERE vehicle_id = ? ORDER BY snapshot_date",
            (vehicle_id,),
        ).fetchall()

    def sold_vehicles(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT v.*,
                   (SELECT price FROM price_history h
                     WHERE h.vehicle_id = v.vehicle_id AND price IS NOT NULL
                  ORDER BY snapshot_date DESC LIMIT 1) AS last_price,
                   (SELECT COUNT(*) FROM price_history h
                     WHERE h.vehicle_id = v.vehicle_id) AS days_tracked
              FROM vehicles v
             WHERE is_active = 0
          ORDER BY sold_detected DESC
            """
        ).fetchall()

    # ------------------------------------------------------------------
    # Manual URLs
    # ------------------------------------------------------------------

    def add_manual_url(self, url: str, label: str = "", vin: str = "") -> int:
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO manual_urls (url, label, vin, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (url.strip(), label.strip(), vin.strip().upper(),
             datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
        return cursor.lastrowid

    def manual_urls(self, enabled_only: bool = False) -> List[sqlite3.Row]:
        sql = "SELECT * FROM manual_urls"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return self.conn.execute(sql + " ORDER BY manual_id").fetchall()

    def update_manual(self, manual_id: int, status: str,
                      vehicle_id: Optional[int] = None) -> None:
        self.conn.execute(
            """
            UPDATE manual_urls
               SET last_checked = ?, last_status = ?,
                   vehicle_id = COALESCE(?, vehicle_id)
             WHERE manual_id = ?
            """,
            (datetime.now(timezone.utc).isoformat(), status, vehicle_id, manual_id),
        )
        self.conn.commit()

    def delete_manual_url(self, manual_id: int) -> None:
        self.conn.execute("DELETE FROM manual_urls WHERE manual_id = ?",
                          (manual_id,))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
