"""ContextService: the one object everything else (CLI, HTTP hooks, chat, evals) talks to.

Freshness model
  * every new event is written to the event store and its (glid, role) marked dirty
  * `ingest_event()` re-renders that file synchronously (hooks path, ~ms)  -> event-to-file latency is logged
  * `refresh_dirty()` drains the queue in batch (nightly / after bulk loads)
  * `get()` is read-through: a dirty or missing file is rebuilt before it is returned
"""
from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

from . import render as R
from .config import Config, load
from .db import DB, now_iso
from .facts import build_facts
from .llm import Sarvam
from .util import clip, now_ist, parse_ts, redact, sha, ts_str


class ContextService:
    def __init__(self, cfg: Config | None = None, db: DB | None = None):
        self.cfg = cfg or load()
        self.db = db or DB(self.cfg.path("db"))
        self.llm = Sarvam(self.cfg, self.db)
        self.out = self.cfg.path("out")
        self.clock = now_ist  # injectable: tests and the scenario script replace it with a fixed timeline

    # ---------------------------------------------------------------- time
    def as_of(self) -> dt.datetime:
        m = self.cfg.as_of_mode()
        if m == "now":
            return self.clock()
        if isinstance(m, dt.datetime):
            return m
        return self.db.data_end() or self.clock()

    def path_for(self, glid: str, role: str) -> Path:
        return self.out / role / f"{glid}.md"

    # -------------------------------------------------------------- render
    def render(self, glid: str, role: str, use_llm: bool | None = None, as_of: dt.datetime | None = None) -> dict | None:
        """Rebuild one file. Returns file metadata, or None if there is nothing known about this GLID/role."""
        as_of = as_of or self.as_of()
        llm = self.llm if (self.llm.enabled if use_llm is None else use_llm) else None
        facts = build_facts(self.db, glid, role, as_of, self.cfg, llm)
        if facts is None:
            self.db.clear_dirty(glid, role)
            return None
        prev = self.db.file_row(glid, role)
        md = R.render(facts, self.cfg, version=(prev["version"] if prev else 0), as_of=as_of)
        h = sha(R.body_for_hash(md))
        version = prev["version"] if prev else 0
        path = self.path_for(glid, role)
        if not prev or prev["content_hash"] != h or not path.exists():
            version += 1
            md = R.render(facts, self.cfg, version=version, as_of=as_of)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(md, encoding="utf-8")
            tmp.replace(path)  # atomic: a reader never sees half a file
        rendered = now_iso()
        words = len(md.split())
        self.db.set_file(glid, role, path, version, h, rendered, facts["data_through"], len(md.encode()), words)
        self.db.clear_dirty(glid, role)
        return {"glid": glid, "role": role, "path": str(path), "version": version, "words": words, "rendered_at": rendered,
                "language": facts["language"], "opening": facts["opening"], "facts": facts, "markdown": md}

    def refresh_dirty(self, limit: int | None = None, use_llm: bool | None = None, log=None) -> int:
        n = 0
        rows = self.db.dirty(limit or 10**9)
        as_of = self.as_of()
        for glid, role, since in rows:
            r = self.render(glid, role, use_llm=use_llm, as_of=as_of)
            if r:
                n += 1
                t0 = dt.datetime.strptime(since, "%Y-%m-%d %H:%M:%S.%f") if "." in since else dt.datetime.strptime(since, "%Y-%m-%d %H:%M:%S")
                self.db.log_refresh(glid, role, since, r["rendered_at"], (dt.datetime.strptime(r["rendered_at"], "%Y-%m-%d %H:%M:%S.%f") - t0).total_seconds() * 1000)
            if log and n and n % 2000 == 0:
                log(f"  rendered {n}/{len(rows)}")
        return n

    # ----------------------------------------------------------------- read
    def resolve_roles(self, glid: str, role: str = "auto") -> list[str]:
        if role in ("buyer", "seller"):
            return [role]
        return self.db.roles_for(glid)

    def get(self, glid: str, role: str = "auto", use_llm: bool | None = None) -> dict | None:
        """Read-through. role='auto' picks the role with the most recent activity."""
        glid = str(glid).strip()
        roles = self.resolve_roles(glid, role)
        if not roles:
            return None
        role = roles[0]
        row = self.db.file_row(glid, role)
        path = self.path_for(glid, role)
        if row and path.exists() and not self.db.is_dirty(glid, role):
            md = path.read_text(encoding="utf-8")
            fm = R.parse_front_matter(md)
            return {"glid": glid, "role": role, "path": str(path), "version": row["version"], "words": row["words"],
                    "language": fm.get("language", ""), "markdown": md, "rendered_at": row["rendered_at"], "cached": True,
                    "other_roles": roles[1:]}
        r = self.render(glid, role, use_llm=use_llm)
        if r:
            r["other_roles"] = roles[1:]
            r["cached"] = False
        return r

    # ---------------------------------------------------------------- write
    def ingest_event(self, glid: str, role: str, channel: str, etype: str, summary: str, data: dict | None = None,
                     ts: dt.datetime | str | None = None, key: str | None = None, refresh: bool = True) -> dict | None:
        """Append one live event (from a hook / webhook / chat turn) and refresh the file. Idempotent via `key`."""
        t = ts if isinstance(ts, dt.datetime) else (parse_ts(ts) if ts else None)
        t = t or self.clock()
        summary = clip(redact(summary), 260)
        key = key or f"live:{glid}:{role}:{channel}:{etype}:{ts_str(t)}:{sha(summary)[:8]}"
        t0 = time.perf_counter()
        new = self.db.add_events([(str(glid), role, ts_str(t), channel, etype, summary, data or {}, key)])
        if new == 0:
            return None
        if not refresh:
            return {"queued": True}
        r = self.render(str(glid), role)
        if r:
            self.db.log_refresh(str(glid), role, now_iso(), r["rendered_at"], (time.perf_counter() - t0) * 1000)
            r["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return r

    def stats(self) -> dict:
        c = self.db.conn
        with self.db.lock:
            return {"events": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                    "files": c.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                    "dirty": c.execute("SELECT COUNT(*) FROM dirty").fetchone()[0],
                    "data_end": str(self.db.data_end()), "llm": self.llm.enabled}
