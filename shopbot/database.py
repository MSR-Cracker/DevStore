import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
 telegram_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT NOT NULL, photo_file_id TEXT,
 credits REAL NOT NULL DEFAULT 0 CHECK(credits >= 0), referral_owner_id INTEGER REFERENCES users(telegram_id),
 referral_earned REAL NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1,
 registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_activity_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS products (
 id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, short_description TEXT NOT NULL, description TEXT NOT NULL,
 price REAL NOT NULL CHECK(price >= 0), product_type TEXT NOT NULL DEFAULT 'paid' CHECK(product_type IN ('paid','free')), stock_quantity INTEGER, image_urls TEXT NOT NULL DEFAULT '[]', is_active INTEGER NOT NULL DEFAULT 1,
 storage_repository_id INTEGER, storage_path TEXT, storage_sha TEXT, storage_url TEXT, storage_asset_id INTEGER, storage_release_id INTEGER, file_size INTEGER, created_by INTEGER NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS purchases (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(telegram_id), product_id INTEGER NOT NULL REFERENCES products(id),
 price REAL NOT NULL, status TEXT NOT NULL CHECK(status IN ('Pending','Paid','Delivering','Completed','Failed','Refunded')),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS credit_transactions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(telegram_id), amount REAL NOT NULL,
 kind TEXT NOT NULL, balance_after REAL NOT NULL, reference_type TEXT, reference_id TEXT, actor_id INTEGER,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS credit_reference_unique ON credit_transactions(kind, reference_type, reference_id) WHERE reference_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS referrals (id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER NOT NULL, referred_id INTEGER NOT NULL UNIQUE, reward REAL NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER REFERENCES users(telegram_id), event_type TEXT NOT NULL, message TEXT NOT NULL, metadata TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS payments (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, credits INTEGER NOT NULL, stars INTEGER NOT NULL, status TEXT NOT NULL, telegram_charge_id TEXT UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, paid_at TEXT);
CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER NOT NULL, body TEXT NOT NULL, audience TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Pending', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS notification_deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, notification_id INTEGER NOT NULL REFERENCES notifications(id), user_id INTEGER NOT NULL REFERENCES users(telegram_id), status TEXT NOT NULL DEFAULT 'Pending', attempts INTEGER NOT NULL DEFAULT 0, error TEXT, sent_at TEXT, UNIQUE(notification_id,user_id));
CREATE TABLE IF NOT EXISTS storage_repositories (id INTEGER PRIMARY KEY AUTOINCREMENT, repo_name TEXT NOT NULL UNIQUE, max_bytes INTEGER NOT NULL, safe_bytes INTEGER NOT NULL, used_bytes INTEGER NOT NULL DEFAULT 0, reserved_bytes INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT NOT NULL, chat_id TEXT NOT NULL UNIQUE, url TEXT NOT NULL, is_required INTEGER NOT NULL DEFAULT 1, is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS support_contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, target TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS custom_buttons (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'url' CHECK(kind IN ('url','internal','dialog')), target TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '', is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS redeem_links (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, credits REAL NOT NULL CHECK(credits > 0), max_uses INTEGER NOT NULL CHECK(max_uses > 0), used_count INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1, created_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS redeem_claims (id INTEGER PRIMARY KEY AUTOINCREMENT, link_id INTEGER NOT NULL REFERENCES redeem_links(id), user_id INTEGER NOT NULL REFERENCES users(telegram_id), claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(link_id,user_id));
CREATE TABLE IF NOT EXISTS services (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', credits REAL NOT NULL DEFAULT 10 CHECK(credits >= 0), is_active INTEGER NOT NULL DEFAULT 1, created_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS service_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(telegram_id), service_id INTEGER NOT NULL REFERENCES services(id), credits REAL NOT NULL, status TEXT NOT NULL DEFAULT 'Building' CHECK(status IN ('Building','Done','Failed','Refunded','Cancelled')), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""

class Database:
    def __init__(self, path: str = "data.db") -> None:
        self.path = Path(path)
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 30000")
        return con
    def initialize(self):
        with self.connect() as con:
            con.executescript(SCHEMA)
            columns={row[1] for row in con.execute("PRAGMA table_info(products)")}
            if "stock_quantity" not in columns:
                con.execute("ALTER TABLE products ADD COLUMN stock_quantity INTEGER")
            if "product_type" not in columns:
                con.execute("ALTER TABLE products ADD COLUMN product_type TEXT NOT NULL DEFAULT 'paid'")
            if "link_message" not in columns:
                con.execute("ALTER TABLE products ADD COLUMN link_message TEXT NOT NULL DEFAULT ''")
            for name, definition in {"storage_url":"TEXT", "storage_asset_id":"INTEGER", "storage_release_id":"INTEGER"}.items():
                if name not in columns:
                    con.execute(f"ALTER TABLE products ADD COLUMN {name} {definition}")
            sub_columns={row[1] for row in con.execute("PRAGMA table_info(subscriptions)")}
            if "image_url" not in sub_columns:
                con.execute("ALTER TABLE subscriptions ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
            svc_columns={row[1] for row in con.execute("PRAGMA table_info(services)")}
            if "description" not in svc_columns:
                con.execute("ALTER TABLE services ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    @contextmanager
    def transaction(self):
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except Exception:
            con.rollback(); raise
        finally: con.close()
