"""SQLite storage: schema, seed data and small helpers."""
import os
import sqlite3

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
DB_PATH = os.path.join(DATA_DIR, "tiptracker.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- A place whose minimum-wage rules apply (city, county, state).
CREATE TABLE IF NOT EXISTS locations (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE,
    notes TEXT DEFAULT ''
);

-- Minimum wage rules for a location, starting on effective_date.
CREATE TABLE IF NOT EXISTS wage_rates (
    id               INTEGER PRIMARY KEY,
    location_id      INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    effective_date   TEXT NOT NULL,
    min_wage         REAL NOT NULL,
    tipped_cash_wage REAL NOT NULL,
    source           TEXT DEFAULT '',
    UNIQUE (location_id, effective_date)
);

CREATE TABLE IF NOT EXISTS employers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    location_id INTEGER REFERENCES locations(id),
    hourly_rate REAL NOT NULL DEFAULT 0,
    tipped      INTEGER NOT NULL DEFAULT 1,
    active      INTEGER NOT NULL DEFAULT 1,
    notes       TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS shifts (
    id          INTEGER PRIMARY KEY,
    employer_id INTEGER NOT NULL REFERENCES employers(id),
    date        TEXT NOT NULL,
    start_time  TEXT DEFAULT '',
    end_time    TEXT DEFAULT '',
    hours       REAL NOT NULL DEFAULT 0,
    hourly_rate REAL NOT NULL DEFAULT 0,
    cash_tips   REAL NOT NULL DEFAULT 0,
    card_tips   REAL NOT NULL DEFAULT 0,
    notes       TEXT DEFAULT '',
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_shifts_date ON shifts(date);

CREATE TABLE IF NOT EXISTS paystubs (
    id           INTEGER PRIMARY KEY,
    employer_id  INTEGER NOT NULL REFERENCES employers(id),
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    pay_date     TEXT DEFAULT '',
    hours        REAL,
    wages        REAL,
    tips         REAL,
    gross        REAL,
    net          REAL,
    notes        TEXT DEFAULT '',
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Photos of tip slips (kind='shift') and paystub images/PDFs (kind='paystub').
CREATE TABLE IF NOT EXISTS attachments (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,
    parent_id     INTEGER NOT NULL,
    filename      TEXT NOT NULL,
    original_name TEXT DEFAULT '',
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_attach_parent ON attachments(kind, parent_id);
"""

# Rates researched September 2026. Editable in Settings.
SEED_LOCATIONS = [
    ("Albuquerque (city limits)",
     "State $12.00 beats the city's $11.85, but the city's $7.20 tipped cash wage beats the state's $3.00.",
     [("2026-01-01", 12.00, 7.20, "City of Albuquerque 2026 notice (cabq.gov)"),
      ("2027-01-01", 13.00, 7.20, "Ordinance O-26-33 (June 2026) - verify against the city notice posted by Oct 15")]),
    ("Bernalillo County (unincorporated)",
     "County rate ($10.95) is below the state's, so state rules apply.",
     [("2026-01-01", 12.00, 3.00, "NM state minimum wage supersedes county rate")]),
    ("New Mexico (elsewhere in state)",
     "",
     [("2026-01-01", 12.00, 3.00, "NM Department of Workforce Solutions")]),
]

DEFAULT_SETTINGS = {
    "week_start": "0",       # 0 = Monday ... 6 = Sunday
    "overtime_hours": "40",
}


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 0:
        for name, notes, rates in SEED_LOCATIONS:
            cur = conn.execute("INSERT INTO locations(name, notes) VALUES (?, ?)", (name, notes))
            for eff, mw, tw, src in rates:
                conn.execute(
                    "INSERT INTO wage_rates(location_id, effective_date, min_wage, tipped_cash_wage, source)"
                    " VALUES (?,?,?,?,?)", (cur.lastrowid, eff, mw, tw, src))
    # Migrations for databases created by older versions.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(employers)")}
    for col, ddl in [("pay_frequency", "TEXT DEFAULT ''"),     # weekly|biweekly|semimonthly|monthly
                     ("period_anchor", "TEXT DEFAULT ''"),     # start date of any one pay period
                     ("payday_offset", "INTEGER DEFAULT 0")]:  # days from period end to payday
        if col not in cols:
            conn.execute(f"ALTER TABLE employers ADD COLUMN {col} {ddl}")
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()


def get_setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key)


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
