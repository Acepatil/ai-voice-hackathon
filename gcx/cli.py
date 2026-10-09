from __future__ import annotations

import argparse
import json
import random
import sys
import time

from . import evals
from .config import load
from .db import DB
from .ingest import ingest_all
from .service import ContextService


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("gcx", description="Global Context: buyer.md / seller.md memory layer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest", help="load the CSV exports into the event store")
    b = sub.add_parser("build", help="render context files")
    b.add_argument("--glid"); b.add_argument("--role", default="auto")
    b.add_argument("--sample", type=int, help="render N random GLIDs per role"); b.add_argument("--all", action="store_true")
    b.add_argument("--cohort", action="store_true", help="the dataset cohort: 5,000 sellers + getContext buyers + sellers that also act as buyers");
    b.add_argument("--dirty", action="store_true", help="only GLIDs with new events"); b.add_argument("--llm", action="store_true", help="use Sarvam digests")
    s = sub.add_parser("show", help="print one context file"); s.add_argument("glid"); s.add_argument("--role", default="auto")
    sv = sub.add_parser("serve", help="start the HTTP hooks server"); sv.add_argument("--port", type=int, default=8088); sv.add_argument("--host", default="127.0.0.1")
    c = sub.add_parser("chat", help="talk to the bot on WhatsApp/chat with a user's context loaded")
    c.add_argument("glid"); c.add_argument("--role", default="auto"); c.add_argument("--channel", default="whatsapp")
    v = sub.add_parser("voice", help="one voice turn from an audio file (Saaras -> bot -> Bulbul)")
    v.add_argument("glid"); v.add_argument("audio_in"); v.add_argument("--out", default="reply.wav"); v.add_argument("--role", default="auto")
    x = sub.add_parser("export-campaign", help="write a Sarvam campaign contact CSV (glid + personalised variables) — no server/hook needed")
    x.add_argument("--glids", help="comma-separated GLIDs, or a text file with one per line"); x.add_argument("--n", type=int, default=20, help="if no --glids: N sellers that have something to say")
    x.add_argument("--role", default="seller"); x.add_argument("--out", default="campaign_contacts.csv")
    e = sub.add_parser("eval", help="run measurements"); e.add_argument("which", choices=["all", "window", "compactness", "freshness", "reask", "lift", "coverage"])
    a = ap.parse_args(argv)

    cfg = load()
    if a.cmd == "ingest":
        db = DB(cfg.path("db")); t = time.time(); ingest_all(cfg, db); print(f"done in {time.time() - t:.1f}s")
        return 0
    svc = ContextService(cfg)
    if a.cmd == "build":
        if a.glid:
            for r in svc.resolve_roles(a.glid, a.role):
                x = svc.render(a.glid, r, use_llm=a.llm or None); print(x["path"] if x else f"no data for {a.glid} as {r}")
        elif a.cohort:
            with svc.db.lock:
                rows = svc.db.conn.execute(
                    "SELECT glid,'seller' FROM profiles WHERE role='seller' UNION "
                    "SELECT glid,'buyer' FROM profiles WHERE role='buyer' AND data LIKE '%\"snapshot\"%' UNION "
                    "SELECT e.glid,'buyer' FROM (SELECT DISTINCT glid FROM events WHERE role='buyer') e JOIN profiles p ON p.glid=e.glid AND p.role='seller'").fetchall()
            t = time.time(); as_of = svc.as_of()
            for i, (g, r) in enumerate(rows):
                svc.render(g, r, use_llm=a.llm or None, as_of=as_of)
                if i % 1000 == 999: print(f"  {i + 1}/{len(rows)}  {time.time() - t:.0f}s", flush=True)
            print(f"rendered cohort of {len(rows)} files in {time.time() - t:.0f}s")
        elif a.sample:
            rng = random.Random(42)
            for role in ("seller", "buyer"):
                pool = sorted(g for g, r in svc.db.all_glids(role))
                for g in rng.sample(pool, min(a.sample, len(pool))):
                    svc.render(g, role, use_llm=a.llm or None)
            print(f"rendered sample of {a.sample} per role into {svc.out}")
        else:
            if a.all:
                svc.db.conn.execute("INSERT OR IGNORE INTO dirty(glid,role,since) SELECT DISTINCT glid,role,datetime('now') FROM events")
            t = time.time(); n = svc.refresh_dirty(use_llm=a.llm or None, log=print); print(f"rendered {n} files in {time.time() - t:.1f}s")
        return 0
    if a.cmd == "export-campaign":
        import csv, os
        from .server import as_vars
        if a.glids and os.path.exists(a.glids):
            ids = [l.strip() for l in open(a.glids) if l.strip()]
        elif a.glids:
            ids = [g.strip() for g in a.glids.split(",") if g.strip()]
        else:  # sellers with an open item in the last weeks, not do-not-call, richest first
            rows = svc.db.conn.execute("SELECT glid FROM files WHERE role=? ORDER BY words DESC", (a.role,)).fetchall()
            ids = []
            for (g,) in rows:
                c = svc.get(g, a.role)
                if c and "do_not_call: true" not in c["markdown"] and "## Open items" in c["markdown"] and "Open with:** “(no" not in c["markdown"]:
                    ids.append(g)
                if len(ids) >= a.n: break
        cols = ["glid", "company", "opening_line", "language", "open_items", "flags", "context_brief", "do_not_call"]
        n = 0
        with open(a.out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(cols)
            for g in ids:
                c = svc.get(g, a.role)
                if not c: continue
                v = as_vars(c, "personalised")
                md = c["markdown"]
                title = next((l[2:] for l in md.splitlines() if l.startswith("# ")), g)
                flags = " | ".join(l[2:] for l in md.split("## Flags", 1)[-1].split("\n## ")[0].splitlines() if l.startswith("- ")) if "## Flags" in md else ""
                recent = [l[2:] for l in md.split("## Last", 1)[-1].split("\n## ")[0].splitlines() if l.startswith("- ")][:3] if "## Last" in md else []
                brief = f"{title}. " + (f"Open items: {v['open_items']}. " if v["open_items"] else "") + ("Recent: " + " ; ".join(recent) if recent else "")
                w.writerow([g, title.split(" — ")[0], v["opening_line"], v["language"], v["open_items"], flags, brief[:700], str(v["do_not_call"]).lower()]); n += 1
        print(f"wrote {n} contacts to {a.out}"); return 0
    if a.cmd == "show":
        r = svc.get(a.glid, a.role); print(r["markdown"] if r else "no data"); return 0 if r else 1
    if a.cmd == "serve":
        import uvicorn
        uvicorn.run("gcx.server:app", host=a.host, port=a.port); return 0
    if a.cmd == "chat":
        from .chat import repl
        repl(svc, a.glid, a.role, a.channel); return 0
    if a.cmd == "voice":
        from .chat import voice_turn
        print(json.dumps(voice_turn(svc, a.glid, a.audio_in, a.out, a.role), indent=2, ensure_ascii=False)); return 0
    if a.cmd == "eval":
        res = evals.run(svc, a.which, cfg.root / "reports"); print(json.dumps(res, indent=2, ensure_ascii=False, default=str)); return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
