"""SQLite store: normalised events, static profiles, rendered-file registry, LLM cache."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path

from .util import parse_ts, ts_str

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  glid TEXT NOT NULL, role TEXT NOT NULL,           -- role: buyer | seller
  ts TEXT NOT NULL,                                 -- event time, IST, 'YYYY-MM-DD HH:MM:SS'
  channel TEXT NOT NULL,                            -- voice_bot | whatsapp | chat | app | call | enquiry | executive | getcontext
  etype TEXT NOT NULL,
  summary TEXT NOT NULL,                            -- short, redacted, human-readable
  data TEXT NOT NULL DEFAULT '{}',
  key TEXT NOT NULL UNIQUE,                         -- idempotency key
  ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ev_glid ON events(glid, role, ts);
CREATE TABLE IF NOT EXISTS profiles(
  glid TEXT NOT NULL, role TEXT NOT NULL, data TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(glid, role));
CREATE TABLE IF NOT EXISTS files(
  glid TEXT NOT NULL, role TEXT NOT NULL, path TEXT NOT NULL, version INTEGER NOT NULL,
  content_hash TEXT NOT NULL, rendered_at TEXT NOT NULL, data_through TEXT, bytes INTEGER, words INTEGER,
  PRIMARY KEY(glid, role));
CREATE TABLE IF NOT EXISTS dirty(
  glid TEXT NOT NULL, role TEXT NOT NULL, since TEXT NOT NULL, PRIMARY KEY(glid, role));
CREATE TABLE IF NOT EXISTS llm_cache(hash TEXT PRIMARY KEY, out TEXT NOT NULL, model TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS refresh_log(
  glid TEXT, role TEXT, event_ingested_at TEXT, rendered_at TEXT, latency_ms REAL);
CREATE TABLE IF NOT EXISTS ab_log(
  glid TEXT, role TEXT, arm TEXT, channel TEXT, ts TEXT, outcome TEXT, meta TEXT);
"""


def now_iso() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------- events
    def add_events(self, rows, mark_dirty: bool = True) -> int:
        """rows: (glid, role, ts, channel, etype, summary, data_dict, key). Returns #new events."""
        ingested = now_iso()
        with self.lock:
            c = self.conn
            before = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            c.execute("BEGIN")
            try:
                c.executemany(
                    "INSERT OR IGNORE INTO events(glid,role,ts,channel,etype,summary,data,key,ingested_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    [(g, r, t, ch, et, s, json.dumps(d, ensure_ascii=False, separators=(",", ":")), k, ingested)
                     for g, r, t, ch, et, s, d, k in rows])
                if mark_dirty:
                    c.executemany("INSERT OR IGNORE INTO dirty(glid,role,since) VALUES(?,?,?)",
                                  [(g, r, ingested) for g, r in {(x[0], x[1]) for x in rows}])
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
            return c.execute("SELECT COUNT(*) FROM events").fetchone()[0] - before

    def events(self, glid: str, role: str, since: str | None = None) -> list[dict]:
        q = "SELECT ts,channel,etype,summary,data,ingested_at FROM events WHERE glid=? AND role=?"
        a: list = [glid, role]
        if since:
            q += " AND ts>=?"
            a.append(since)
        q += " ORDER BY ts DESC, id DESC"
        with self.lock:
            out = []
            for r in self.conn.execute(q, a):
                d = dict(r)
                d["data"] = json.loads(d["data"] or "{}")
                d["dt"] = parse_ts(d["ts"])
                out.append(d)
            return out

    def data_end(self) -> dt.datetime | None:
        with self.lock:
            r = self.conn.execute("SELECT MAX(ts) FROM events").fetchone()
        return parse_ts(r[0]) if r and r[0] else None

    # ----------------------------------------------------------- profiles
    def set_profile(self, glid: str, role: str, data: dict, merge: bool = True) -> None:
        with self.lock:
            if merge:
                old = self.profile(glid, role) or {}
                old.update({k: v for k, v in data.items() if v not in (None, "")})
                data = old
            self.conn.execute(
                "INSERT INTO profiles(glid,role,data,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(glid,role) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (glid, role, json.dumps(data, ensure_ascii=False), now_iso()))

    def set_profiles_bulk(self, rows) -> None:
        with self.lock:
            self.conn.execute("BEGIN")
            self.conn.executemany(
                "INSERT INTO profiles(glid,role,data,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(glid,role) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                [(g, r, json.dumps(d, ensure_ascii=False), now_iso()) for g, r, d in rows])
            self.conn.execute("COMMIT")

    def profile(self, glid: str, role: str) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT data FROM profiles WHERE glid=? AND role=?", (glid, role)).fetchone()
        return json.loads(r[0]) if r else None

    def roles_for(self, glid: str) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT role, MAX(ts) m FROM events WHERE glid=? GROUP BY role "
                "UNION SELECT role, '' FROM profiles WHERE glid=?", (glid, glid)).fetchall()
        best: dict[str, str] = {}
        for r in rows:
            best[r[0]] = max(best.get(r[0], ""), r[1] or "")
        return [k for k, _ in sorted(best.items(), key=lambda kv: kv[1], reverse=True)]

    # ------------------------------------------------------------- dirty
    def mark_dirty(self, glid: str, role: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR IGNORE INTO dirty(glid,role,since) VALUES(?,?,?)", (glid, role, now_iso()))

    def dirty(self, limit: int = 100000) -> list[tuple[str, str, str]]:
        with self.lock:
            return [tuple(r) for r in self.conn.execute(
                "SELECT glid,role,since FROM dirty ORDER BY since LIMIT ?", (limit,))]

    def clear_dirty(self, glid: str, role: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM dirty WHERE glid=? AND role=?", (glid, role))

    def is_dirty(self, glid: str, role: str) -> bool:
        with self.lock:
            return self.conn.execute("SELECT 1 FROM dirty WHERE glid=? AND role=?", (glid, role)).fetchone() is not None

    # ------------------------------------------------------------- files
    def file_row(self, glid: str, role: str) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM files WHERE glid=? AND role=?", (glid, role)).fetchone()
        return dict(r) if r else None

    def set_file(self, glid, role, path, version, content_hash, rendered_at, data_through, nbytes, words) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(glid,role) DO UPDATE SET "
                "path=excluded.path,version=excluded.version,content_hash=excluded.content_hash,"
                "rendered_at=excluded.rendered_at,data_through=excluded.data_through,bytes=excluded.bytes,words=excluded.words",
                (glid, role, str(path), version, content_hash, rendered_at, data_through, nbytes, words))

    def log_refresh(self, glid, role, event_ingested_at, rendered_at, latency_ms) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO refresh_log VALUES(?,?,?,?,?)",
                              (glid, role, event_ingested_at, rendered_at, latency_ms))

    # --------------------------------------------------------------- llm
    def cache_get(self, h: str) -> str | None:
        with self.lock:
            r = self.conn.execute("SELECT out FROM llm_cache WHERE hash=?", (h,)).fetchone()
        return r[0] if r else None

    def cache_put(self, h: str, out: str, model: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO llm_cache VALUES(?,?,?,?)", (h, out, model, now_iso()))

    # ---------------------------------------------------------------- ab
    def ab_add(self, glid, role, arm, channel, outcome="", meta="") -> None:
        with self.lock:
            self.conn.execute("INSERT INTO ab_log VALUES(?,?,?,?,?,?,?)",
                              (glid, role, arm, channel, now_iso(), outcome, meta))

    def all_glids(self, role: str | None = None) -> list[tuple[str, str]]:
        q = "SELECT DISTINCT glid, role FROM events" + (" WHERE role=?" if role else "")
        with self.lock:
            return [tuple(r) for r in self.conn.execute(q, (role,) if role else ())]
