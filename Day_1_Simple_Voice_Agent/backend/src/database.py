import json
import logging
import os
import sqlite3
from typing import List, Optional

logger = logging.getLogger("food_agent_sqlite.db")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
if not logger.handlers:
    logger.addHandler(handler)

DB_FILE = "order_db.sqlite"


def get_db_path() -> str:
    """Return absolute path for the DB file. If __file__ is not defined (interactive), fall back to cwd."""
    try:
        base = os.path.abspath(os.path.dirname(__file__))
    except NameError:
        base = os.getcwd()
    # ensure directory exists
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, DB_FILE)


def get_conn():
    """
    Return a sqlite3 Connection tuned for light concurrent access from
    threads/background tasks.
    """
    path = get_db_path()
    # check_same_thread=False required for async/background tasks accessing DB
    # timeout gives a bit of breathing room if DB is busy
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # enable foreign keys and attempt WAL for better concurrency
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
    except Exception:
        logger.debug("Couldn't set PRAGMA journal_mode=WAL; continuing with defaults.")
    return conn


def seed_database():
    """Create tables and seed the Indian catalog if empty."""
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        # ---- TABLES ----
        cur.execute("""
            CREATE TABLE IF NOT EXISTS catalog (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT,
                price REAL NOT NULL,
                brand TEXT,
                size TEXT,
                units TEXT,
                tags TEXT -- JSON encoded list
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                timestamp TEXT,
                total REAL,
                customer_name TEXT,
                address TEXT,
                status TEXT DEFAULT 'received',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT,
                item_id TEXT,
                name TEXT,
                unit_price REAL,
                quantity INTEGER,
                notes TEXT,
                FOREIGN KEY(order_id) REFERENCES orders(order_id) ON DELETE CASCADE
            )
        """)

        # ---- SEED DATA ----
        cur.execute("SELECT COUNT(1) FROM catalog")
        if cur.fetchone()[0] == 0:
            catalog = [
                # Dairy
                ("milk-amul-1l", "Amul Taaza Milk", "Dairy", 72.00, "Amul", "1L", "pack", json.dumps(["dairy", "essential"])),
                ("paneer-200g", "Amul Malai Paneer", "Dairy", 95.00, "Amul", "200g", "pack", json.dumps(["dairy", "protein", "veg"])),
                ("butter-100g", "Amul Butter", "Dairy", 58.00, "Amul", "100g", "pack", json.dumps(["dairy"])),
                ("curd-400g", "Mother Dairy Dahi", "Dairy", 40.00, "Mother Dairy", "400g", "cup", json.dumps(["dairy"])),

                # Staples / Pantry
                ("atta-5kg", "Aashirvaad Whole Wheat Atta", "Staples", 245.00, "Aashirvaad", "5kg", "bag", json.dumps(["flour", "roti"])),
                ("rice-basmati-1kg", "India Gate Basmati Rice", "Staples", 160.00, "India Gate", "1kg", "bag", json.dumps(["rice", "premium"])),
                ("dal-toor-1kg", "Tata Sampann Toor Dal", "Staples", 185.00, "Tata", "1kg", "pack", json.dumps(["protein", "dal"])),
                ("salt-1kg", "Tata Salt", "Staples", 28.00, "Tata", "1kg", "pack", json.dumps(["essential"])),
                ("sugar-1kg", "Madhur Sugar", "Staples", 60.00, "Madhur", "1kg", "pack", json.dumps(["sweet"])),

                # Snacks & Instant
                ("maggi-masala", "Maggi 2-Minute Noodles", "Instant Food", 14.00, "Nestle", "70g", "pack", json.dumps(["snack", "noodles"])),
                ("biscuits-marie", "Britannia Marie Gold", "Snacks", 35.00, "Britannia", "250g", "pack", json.dumps(["tea-time"])),
                ("chips-lays", "Lays Magic Masala", "Snacks", 20.00, "Lays", "50g", "pack", json.dumps(["snack", "spicy"])),
                ("tea-250g", "Red Label Tea", "Beverages", 140.00, "Brooke Bond", "250g", "pack", json.dumps(["chai", "tea"])),

                # Veggies (Market Price estimates)
                ("potato-1kg", "Fresh Potatoes", "Vegetables", 40.00, "", "1kg", "kg", json.dumps(["veg"])),
                ("onion-1kg", "Fresh Onions", "Vegetables", 55.00, "", "1kg", "kg", json.dumps(["veg"])),
                ("tomato-1kg", "Fresh Tomatoes", "Vegetables", 60.00, "", "1kg", "kg", json.dumps(["veg"])),
                ("ginger-100g", "Fresh Ginger", "Vegetables", 20.00, "", "100g", "g", json.dumps(["veg", "chai"])),
            ]
            cur.executemany("""
                INSERT INTO catalog (id, name, category, price, brand, size, units, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, catalog)
            conn.commit()
            logger.info(f"✅ Seeded Indian catalog into {get_db_path()}")

    except Exception as e:
        logger.exception("Failed to seed database: %s", e)
    finally:
        if conn:
            conn.close()


# Seed DB on import/run (safe to call multiple times)
seed_database()


# ----- DB helper functions -----

def find_catalog_item_by_id_db(item_id: str) -> Optional[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM catalog WHERE LOWER(id) = LOWER(?) LIMIT 1", (item_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    record = dict(row)
    try:
        record["tags"] = json.loads(record.get("tags") or "[]")
    except Exception:
        record["tags"] = []
    return record


def search_catalog_by_name_db(query: str, limit: int = 50) -> List[dict]:
    q = f"%{query.lower()}%"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM catalog
        WHERE LOWER(name) LIKE ? OR LOWER(tags) LIKE ?
        LIMIT ?
    """, (q, q, limit))
    rows = cur.fetchall()
    conn.close()
    results = []
    for r in rows:
        rec = dict(r)
        try:
            rec["tags"] = json.loads(rec.get("tags") or "[]")
        except Exception:
            rec["tags"] = []
        results.append(rec)
    return results


def insert_order_db(order_id: str, timestamp: str, total: float,
                    customer_name: str, address: str, status: str,
                    items: List[dict]) -> bool:
    """
    Insert an order and its items. Items may be CartItem-like objects or dicts.
    Returns True on success, False on failure.
    """
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO orders (order_id, timestamp, total, customer_name, address, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """, (order_id, timestamp, total, customer_name, address, status))

        for ci in items:

            # If CartItem object → use attributes
            if hasattr(ci, "__dict__"):
                item_id = getattr(ci, "item_id", None)
                name = getattr(ci, "name", None)
                unit_price = getattr(ci, "unit_price", None)
                quantity = getattr(ci, "quantity", None)
                notes = getattr(ci, "notes", "") or ""
            else:
                # If dict → use .get()
                item_id = ci.get("item_id")
                name = ci.get("name")
                unit_price = ci.get("unit_price")
                quantity = ci.get("quantity")
                notes = ci.get("notes", "")

            cur.execute("""
                INSERT INTO order_items (order_id, item_id, name, unit_price, quantity, notes)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (order_id, item_id, name, unit_price, quantity, notes))

        conn.commit()
        return True

    except sqlite3.IntegrityError as ie:
        logger.exception("DB integrity error inserting order %s: %s", order_id, ie)
        return False
    except Exception as e:
        logger.exception("Error inserting order %s: %s", order_id, e)
        return False
    finally:
        if conn:
            conn.close()



def get_order_db(order_id: str) -> Optional[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE order_id = ? LIMIT 1", (order_id,))
    o = cur.fetchone()
    if not o:
        conn.close()
        return None
    order = dict(o)
    cur.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,))
    items = [dict(r) for r in cur.fetchall()]
    conn.close()
    order["items"] = items
    return order


def list_orders_db(limit: int = 10, customer_name: Optional[str] = None) -> List[dict]:
    conn = get_conn()
    cur = conn.cursor()
    if customer_name:
        cur.execute("SELECT * FROM orders WHERE LOWER(customer_name) = LOWER(?) ORDER BY created_at DESC LIMIT ?", (customer_name, limit))
    else:
        cur.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def update_order_status_db(order_id: str, new_status: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE orders SET status = ?, updated_at = datetime('now') WHERE order_id = ?", (new_status, order_id))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed > 0


# Tag inference helper (moved to DB module because it queries catalog)
import re

def infer_items_from_tags(query: str, max_results: int = 6) -> List[str]:
    """Try to infer catalog items by matching query words to tags in the catalog. Returns list of item_ids."""
    words = re.findall(r"\w+", (query or "").lower())
    found = []
    conn = get_conn()
    cur = conn.cursor()
    for w in words:
        if len(found) >= max_results:
            break
        # match JSON-encoded tag (contains quotes) or name text
        q = f"%\"{w}\"%"
        cur.execute("SELECT * FROM catalog WHERE LOWER(tags) LIKE ? OR LOWER(name) LIKE ? LIMIT 10", (q, f"%{w}%"))
        rows = cur.fetchall()
        for r in rows:
            rid = r["id"]
            if rid not in found:
                found.append(rid)
                if len(found) >= max_results:
                    break
    conn.close()
    return found
