import json
import sqlite3
import time
from datetime import date
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
    key TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS arms (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    label TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1,
    origin TEXT NOT NULL DEFAULT 'config',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    key TEXT PRIMARY KEY,
    arm_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    name TEXT NOT NULL,
    website TEXT,
    payload TEXT NOT NULL,
    dupes INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at REAL NOT NULL,
    processed_at REAL
);
CREATE INDEX IF NOT EXISTS candidates_status ON candidates(status, arm_id);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    arm_id TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    url TEXT,
    data TEXT NOT NULL,
    analysis TEXT NOT NULL,
    score INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    created_at REAL NOT NULL,
    notified_at REAL,
    digest_at REAL,
    message_id INTEGER
);
CREATE INDEX IF NOT EXISTS leads_status ON leads(status, kind);
CREATE TABLE IF NOT EXISTS feedback (
    lead_id INTEGER PRIMARY KEY,
    verdict TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    items TEXT NOT NULL,
    reason TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS directives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT PRIMARY KEY,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    report TEXT NOT NULL
);
"""


def loads(value, default=None):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def all(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, *args) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def run(self, sql: str, *args) -> int:
        return self.conn.execute(sql, args).lastrowid

    def get(self, key: str, default=None):
        row = self.one("SELECT value FROM kv WHERE key = ?", key)
        return loads(row["value"], default) if row else default

    def put(self, key: str, value) -> None:
        self.run(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            key,
            dumps(value),
        )

    def is_seen(self, key: str) -> bool:
        return self.one("SELECT 1 FROM seen WHERE key = ?", key) is not None

    def mark_seen(self, key: str) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO seen(key, created_at) VALUES(?, ?)", (key, time.time()))
        return cur.rowcount > 0

    def ensure_arm(self, arm_id: str, kind: str, source: str, label: str, params: dict, origin: str = "config", active: int = 1) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO arms(id, kind, source, label, params, state, active, origin, created_at) VALUES(?, ?, ?, ?, ?, '{}', ?, ?, ?)",
            (arm_id, kind, source, label, dumps(params), active, origin, time.time()),
        )
        return cur.rowcount > 0

    def arm(self, arm_id: str) -> dict | None:
        row = self.one("SELECT * FROM arms WHERE id = ?", arm_id)
        return self._arm(row) if row else None

    def arms(self, kind: str | None = None, source: str | None = None, active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM arms WHERE 1 = 1"
        args = []
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if source:
            sql += " AND source = ?"
            args.append(source)
        if active_only:
            sql += " AND active = 1"
        return [self._arm(r) for r in self.all(sql + " ORDER BY created_at", *args)]

    def set_arm_state(self, arm_id: str, state: dict) -> None:
        self.run("UPDATE arms SET state = ? WHERE id = ?", dumps(state), arm_id)

    def set_arm_active(self, arm_id: str, active: bool) -> bool:
        cur = self.conn.execute("UPDATE arms SET active = ? WHERE id = ?", (1 if active else 0, arm_id))
        return cur.rowcount > 0

    @staticmethod
    def _arm(row) -> dict:
        out = dict(row)
        out["params"] = loads(out["params"], {})
        out["state"] = loads(out["state"], {})
        return out

    def enqueue_candidate(self, key: str, arm_id: str, provider: str, name: str, website: str | None, payload: dict) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO candidates(key, arm_id, provider, name, website, payload, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (key, arm_id, provider, name, website, dumps(payload), time.time()),
        )
        if cur.rowcount == 0:
            self.run("UPDATE candidates SET dupes = dupes + 1 WHERE key = ?", key)
            return False
        return True

    def queued_counts(self) -> dict[str, int]:
        rows = self.all("SELECT arm_id, COUNT(*) AS n FROM candidates WHERE status = 'queued' GROUP BY arm_id")
        return {r["arm_id"]: r["n"] for r in rows}

    def take_candidates(self, arm_id: str, limit: int) -> list[dict]:
        rows = self.all(
            "SELECT * FROM candidates WHERE status = 'queued' AND arm_id = ? ORDER BY created_at LIMIT ?",
            arm_id,
            limit,
        )
        out = []
        for r in rows:
            item = dict(r)
            item["payload"] = loads(item["payload"], {})
            out.append(item)
        return out

    def finish_candidate(self, key: str, status: str) -> None:
        self.run("UPDATE candidates SET status = ?, processed_at = ? WHERE key = ?", status, time.time(), key)

    def add_lead(self, kind: str, arm_id: str, source: str, external_id: str, title: str, url: str | None, data: dict, analysis: dict, score: int) -> int | None:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO leads(kind, arm_id, source, external_id, title, url, data, analysis, score, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, arm_id, source, external_id, title, url, dumps(data), dumps(analysis), int(score), time.time()),
        )
        return cur.lastrowid if cur.rowcount else None

    def lead(self, lead_id: int) -> dict | None:
        row = self.one(
            "SELECT l.*, f.verdict FROM leads l LEFT JOIN feedback f ON f.lead_id = l.id WHERE l.id = ?",
            lead_id,
        )
        return self._lead(row) if row else None

    def leads(self, where: str = "1 = 1", *args, order: str = "l.id DESC", limit: int = 100) -> list[dict]:
        rows = self.all(
            f"SELECT l.*, f.verdict FROM leads l LEFT JOIN feedback f ON f.lead_id = l.id WHERE {where} ORDER BY {order} LIMIT ?",
            *args,
            limit,
        )
        return [self._lead(r) for r in rows]

    @staticmethod
    def _lead(row) -> dict:
        out = dict(row)
        out["data"] = loads(out["data"], {})
        out["analysis"] = loads(out["analysis"], {})
        return out

    def set_lead_status(self, lead_id: int, status: str, **fields) -> None:
        sets = ["status = ?"]
        args = [status]
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            args.append(value)
        args.append(lead_id)
        self.run(f"UPDATE leads SET {', '.join(sets)} WHERE id = ?", *args)

    def set_feedback(self, lead_id: int, verdict: str) -> None:
        now = time.time()
        self.run(
            "INSERT INTO feedback(lead_id, verdict, created_at, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(lead_id) DO UPDATE SET verdict = excluded.verdict, updated_at = excluded.updated_at",
            lead_id,
            verdict,
            now,
            now,
        )

    def add_usage(self, input_tokens: int, output_tokens: int, cost: float) -> None:
        self.run(
            "INSERT INTO usage(day, input_tokens, output_tokens, cost, calls) VALUES(?, ?, ?, ?, 1) "
            "ON CONFLICT(day) DO UPDATE SET input_tokens = input_tokens + excluded.input_tokens, "
            "output_tokens = output_tokens + excluded.output_tokens, cost = cost + excluded.cost, calls = calls + 1",
            date.today().isoformat(),
            input_tokens,
            output_tokens,
            cost,
        )

    def usage(self, day: str | None = None) -> dict:
        row = self.one("SELECT * FROM usage WHERE day = ?", day or date.today().isoformat())
        return dict(row) if row else {"day": day, "input_tokens": 0, "output_tokens": 0, "cost": 0.0, "calls": 0}

    def usage_month(self) -> float:
        prefix = date.today().strftime("%Y-%m")
        row = self.one("SELECT COALESCE(SUM(cost), 0) AS c FROM usage WHERE day LIKE ?", prefix + "%")
        return float(row["c"])
