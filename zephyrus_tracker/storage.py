"""SQLite persistence: products, price history, offer state and sent alerts."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import Alert, Offer, Product

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku           TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    model_number  TEXT DEFAULT '',
    url           TEXT DEFAULT '',
    image         TEXT DEFAULT '',
    manufacturer  TEXT DEFAULT '',
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);

-- One row per *change* in price or availability. Unchanged scans are not
-- recorded, which keeps the history compact and readable.
CREATE TABLE IF NOT EXISTS observations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    sku            TEXT NOT NULL,
    condition      TEXT NOT NULL,
    price          REAL,
    regular_price  REAL,
    available      INTEGER NOT NULL DEFAULT 0,
    store_count    INTEGER NOT NULL DEFAULT 0,
    stores         TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_obs_offer_ts ON observations (sku, condition, ts);

-- Latest known state per (sku, condition), used for change detection.
CREATE TABLE IF NOT EXISTS offer_state (
    sku            TEXT NOT NULL,
    condition      TEXT NOT NULL,
    price          REAL,
    regular_price  REAL,
    available      INTEGER NOT NULL DEFAULT 0,
    store_count    INTEGER NOT NULL DEFAULT 0,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    PRIMARY KEY (sku, condition)
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    sku         TEXT NOT NULL,
    condition   TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    price       REAL,
    severity    INTEGER NOT NULL DEFAULT 1,
    message     TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_alerts_dedupe ON alerts (dedupe_key, ts);

CREATE TABLE IF NOT EXISTS stores (
    store_id     TEXT PRIMARY KEY,
    name         TEXT DEFAULT '',
    city         TEXT DEFAULT '',
    region       TEXT DEFAULT '',
    postal_code  TEXT DEFAULT '',
    distance     REAL,
    store_type   TEXT DEFAULT '',
    refreshed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    products      INTEGER NOT NULL DEFAULT 0,
    offers        INTEGER NOT NULL DEFAULT 0,
    alerts        INTEGER NOT NULL DEFAULT 0,
    api_requests  INTEGER NOT NULL DEFAULT 0,
    duration_s    REAL NOT NULL DEFAULT 0,
    error         TEXT
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------- products

    def upsert_product(self, product: Product) -> bool:
        """Insert or refresh a product. Returns True if it was new to us."""
        now = utcnow()
        cur = self.conn.execute("SELECT sku FROM products WHERE sku = ?", (product.sku,))
        is_new = cur.fetchone() is None
        if is_new:
            self.conn.execute(
                "INSERT INTO products (sku, name, model_number, url, image, manufacturer,"
                " first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)",
                (product.sku, product.name, product.model_number, product.url,
                 product.image, product.manufacturer, now, now),
            )
        else:
            self.conn.execute(
                "UPDATE products SET name=?, model_number=?, url=?, image=?,"
                " manufacturer=?, last_seen=? WHERE sku=?",
                (product.name, product.model_number, product.url, product.image,
                 product.manufacturer, now, product.sku),
            )
        return is_new

    def get_products(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM products ORDER BY name"))

    def product_name(self, sku: str) -> str:
        row = self.conn.execute("SELECT name FROM products WHERE sku=?", (sku,)).fetchone()
        return row["name"] if row else sku

    # ---------------------------------------------------------------- state

    def get_offer_state(self, sku: str, condition: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM offer_state WHERE sku=? AND condition=?", (sku, condition)
        ).fetchone()

    def all_offer_states(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM offer_state"))

    def record_offer(self, offer: Offer, *, changed: bool) -> None:
        """Persist current state, and append to history only when something moved."""
        now = utcnow()
        available = 1 if offer.available else 0
        stores_json = json.dumps([
            {"store_id": s, "name": n, "city": c, "low_stock": bool(low)}
            for s, n, c, low in offer.stores
        ])

        if changed:
            self.conn.execute(
                "INSERT INTO observations (ts, sku, condition, price, regular_price,"
                " available, store_count, stores) VALUES (?,?,?,?,?,?,?,?)",
                (now, offer.sku, offer.condition, offer.price, offer.regular_price,
                 available, len(offer.stores), stores_json),
            )

        existing = self.get_offer_state(offer.sku, offer.condition)
        if existing is None:
            self.conn.execute(
                "INSERT INTO offer_state (sku, condition, price, regular_price, available,"
                " store_count, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)",
                (offer.sku, offer.condition, offer.price, offer.regular_price,
                 available, len(offer.stores), now, now),
            )
        else:
            self.conn.execute(
                "UPDATE offer_state SET price=?, regular_price=?, available=?,"
                " store_count=?, last_seen=? WHERE sku=? AND condition=?",
                (offer.price, offer.regular_price, available, len(offer.stores),
                 now, offer.sku, offer.condition),
            )

    def mark_offers_gone(self, seen_keys: set[tuple[str, str]]) -> list[tuple[str, str]]:
        """Flag offers we've tracked before but that were absent this scan.

        Returns the keys that just transitioned from available to gone, so the
        caller can record the disappearance in history.
        """
        gone: list[tuple[str, str]] = []
        now = utcnow()
        for row in self.all_offer_states():
            key = (row["sku"], row["condition"])
            if key in seen_keys or not row["available"]:
                continue
            gone.append(key)
            self.conn.execute(
                "INSERT INTO observations (ts, sku, condition, price, regular_price,"
                " available, store_count, stores) VALUES (?,?,?,?,?,0,0,'[]')",
                (now, row["sku"], row["condition"], None, row["regular_price"]),
            )
            self.conn.execute(
                "UPDATE offer_state SET available=0, price=NULL, store_count=0, last_seen=?"
                " WHERE sku=? AND condition=?",
                (now, row["sku"], row["condition"]),
            )
        return gone

    # -------------------------------------------------------------- history

    def price_history(self, sku: str, condition: str, *, limit: int = 500) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT ts, price, regular_price, available, store_count FROM observations"
            " WHERE sku=? AND condition=? ORDER BY ts DESC LIMIT ?",
            (sku, condition, limit),
        ))

    def lowest_price(self, sku: str, condition: str, *, since_days: int | None = None) -> float | None:
        """All-time (or windowed) minimum recorded price for an offer."""
        sql = "SELECT MIN(price) AS low FROM observations WHERE sku=? AND condition=? AND price IS NOT NULL"
        params: list[Any] = [sku, condition]
        if since_days:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat(timespec="seconds")
            sql += " AND ts >= ?"
            params.append(cutoff)
        row = self.conn.execute(sql, params).fetchone()
        return row["low"] if row and row["low"] is not None else None

    def observation_count(self, sku: str, condition: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM observations WHERE sku=? AND condition=?", (sku, condition)
        ).fetchone()
        return int(row["n"]) if row else 0

    # --------------------------------------------------------------- alerts

    def was_alerted_recently(self, dedupe_key: str, cooldown_hours: int) -> bool:
        if cooldown_hours <= 0:
            return False
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)).isoformat(timespec="seconds")
        row = self.conn.execute(
            "SELECT 1 FROM alerts WHERE dedupe_key=? AND ts >= ? LIMIT 1", (dedupe_key, cutoff)
        ).fetchone()
        return row is not None

    def record_alert(self, alert: Alert) -> None:
        self.conn.execute(
            "INSERT INTO alerts (ts, kind, sku, condition, dedupe_key, price, severity,"
            " message, payload) VALUES (?,?,?,?,?,?,?,?,?)",
            (utcnow(), alert.kind, alert.sku, alert.condition, alert.dedupe_key,
             alert.price, alert.severity, alert.message, json.dumps(alert.as_dict())),
        )

    def recent_alerts(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)
        ))

    # --------------------------------------------------------------- stores

    def save_stores(self, stores: list[dict[str, Any]]) -> None:
        now = utcnow()
        with self.tx() as conn:
            conn.execute("DELETE FROM stores")
            conn.executemany(
                "INSERT OR REPLACE INTO stores (store_id, name, city, region, postal_code,"
                " distance, store_type, refreshed_at) VALUES (?,?,?,?,?,?,?,?)",
                [
                    (str(s.get("storeId")), s.get("name") or "", s.get("city") or "",
                     s.get("region") or "", str(s.get("postalCode") or ""),
                     float(s.get("distance") or 0), s.get("storeType") or "", now)
                    for s in stores
                ],
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('stores_refreshed_at', ?)", (now,)
            )

    def get_stores(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM stores ORDER BY distance"))

    def stores_age_hours(self) -> float | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key='stores_refreshed_at'").fetchone()
        if not row:
            return None
        then = datetime.fromisoformat(row["value"])
        return (datetime.now(timezone.utc) - then).total_seconds() / 3600

    # ---------------------------------------------------------------- scans

    def record_scan(self, *, products: int, offers: int, alerts: int,
                    api_requests: int, duration_s: float, error: str | None = None) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO scans (ts, products, offers, alerts, api_requests, duration_s, error)"
                " VALUES (?,?,?,?,?,?,?)",
                (utcnow(), products, offers, alerts, api_requests, round(duration_s, 2), error),
            )

    def last_scan(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
