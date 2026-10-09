"""HTTP surface: context API, Sarvam Voice Agent hooks, generic event feed, chat channels, metrics.

Run:  uvicorn gcx.server:app --port 8088        (or: python -m gcx serve)
The Sarvam console's on-start hook should point at  POST /hooks/on-start  and the on-end hook at /hooks/on-end.
"""
from __future__ import annotations

import statistics
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from . import chat as chatmod
from .service import ContextService
from .util import TTS_CODE, clip, sha

svc = ContextService()
app = FastAPI(title="Global Context — memory across channels", version="1.0")


def auth(authorization: str | None = Header(None), x_hook_token: str | None = Header(None)) -> None:
    tok = svc.cfg.hook_token
    if not tok:
        return
    given = (authorization or "").removeprefix("Bearer ").strip() or (x_hook_token or "")
    if given != tok:
        raise HTTPException(401, "bad token")


def find_glid(payload: dict[str, Any], query_glid: str | None = None) -> str | None:
    """Sarvam lets you map arbitrary fields; accept the common spellings and nested containers."""
    if query_glid:
        return str(query_glid)
    keys = ("glid", "GLID", "glusr_id", "GLUSR_ID", "user_id", "customer_id")
    stack = [payload]
    while stack:
        d = stack.pop(0)
        if isinstance(d, dict):
            for k in keys:
                if d.get(k) not in (None, ""):
                    return str(d[k]).strip()
            stack.extend(v for v in d.values() if isinstance(v, dict))
    return None


def as_vars(ctx: dict, arm: str) -> dict:
    """Flat key/value map: easy to map into Sarvam agent variables (@glid, @opening_line, ...)."""
    md = ctx["markdown"]
    body = md.split("\n---\n", 1)[-1].strip()
    f = ctx.get("facts") or {}
    opening = (f.get("opening") if arm == "personalised" else f.get("opening_generic")) if f else None
    if opening is None:  # served from cache: recover from the file text
        import re
        m = re.search(r"\*\*Open with:\*\* “(.*)”", md)
        opening = m.group(1) if m and arm == "personalised" else ""
    from .render import parse_front_matter
    fm = parse_front_matter(md)
    dnc = fm.get("do_not_call") == "true"
    if dnc:
        opening = ""  # never hand a speakable placeholder to the voice agent
    items = [ln[2:] for ln in body.split("## Open items", 1)[-1].split("\n## ")[0].splitlines() if ln.startswith("- ")] if "## Open items" in body else []
    lang = ctx.get("language") or fm.get("language", "hinglish")
    return {"glid": ctx["glid"], "role": ctx["role"], "language": lang, "language_code": TTS_CODE.get(lang, "hi-IN"),
            "opening_line": opening or "", "open_items": " | ".join(items[:3]), "do_not_call": dnc,
            "ai_disclosure_required": "asked whether they are talking to a bot" in md,
            "context_md": clip(body, 2600), "file_version": ctx["version"], "ab_arm": arm}


@app.get("/health")
def health():
    return {"ok": True, **svc.stats()}


@app.get("/context/{glid}", dependencies=[Depends(auth)])
def get_context(glid: str, role: str = "auto", fmt: str = Query("md", pattern="^(md|json|vars)$")):
    ctx = svc.get(glid, role)
    if not ctx:
        raise HTTPException(404, "no data for this GLID")
    if fmt == "md":
        return PlainTextResponse(ctx["markdown"], media_type="text/markdown; charset=utf-8",
                                 headers={"X-Context-Version": str(ctx["version"]), "X-Context-Role": ctx["role"],
                                          "X-Other-Roles": ",".join(ctx.get("other_roles", []))})
    if fmt == "vars":
        return as_vars(ctx, "personalised")
    f = ctx.get("facts")
    if not f:
        r = svc.render(glid, ctx["role"])
        f = r["facts"] if r else {}
    return {k: v for k, v in f.items() if not k.startswith("_")}


@app.api_route("/hooks/on-start", methods=["GET", "POST"], dependencies=[Depends(auth)])
async def on_start(payload: dict | None = None, glid: str | None = None, role: str = "auto"):
    """Sarvam on-start hook: called before the agent speaks. Returns flat variables incl. the opening line.
    Unknown caller -> still 200 with a generic opening so a call never fails because memory is missing."""
    payload = payload or {}
    g = find_glid(payload, glid)
    role = payload.get("role", role) if isinstance(payload, dict) else role
    ctx = svc.get(g, role) if g else None
    if not ctx:
        return {"glid": g or "", "role": role if role != "auto" else "seller", "language": "hinglish", "language_code": "hi-IN",
                "opening_line": "Namaste, main IndiaMART ka assistant hoon. Aaj main aapki kya madad kar sakta hoon?",
                "open_items": "", "do_not_call": False, "ai_disclosure_required": False, "context_md": "",
                "file_version": 0, "ab_arm": "generic_unknown"}
    pct = svc.cfg.holdout_pct
    arm = "control" if pct and int(sha(ctx["glid"])[:6], 16) % 100 < pct else "personalised"
    svc.db.ab_add(ctx["glid"], ctx["role"], arm, "voice", "started")
    if arm == "control" and "facts" not in ctx:
        ctx = svc.render(ctx["glid"], ctx["role"]) or ctx
    return as_vars(ctx, arm)


@app.post("/hooks/on-end", dependencies=[Depends(auth)])
async def on_end(payload: dict):
    """Sarvam on-end hook: transcript + outcome of a finished call -> event -> file refreshed before the next channel reads it."""
    g = find_glid(payload)
    if not g:
        raise HTTPException(422, "glid missing")
    role = payload.get("role") or "seller"
    tr = payload.get("transcript") or []
    turns = []
    if isinstance(tr, list):
        for t in tr[:30]:
            if isinstance(t, dict):
                turns.append(((t.get("speaker") or t.get("role") or "?").lower().replace("agent", "bot").replace("user", "seller"),
                              clip(t.get("text") or t.get("content") or "", 140)))
    elif isinstance(tr, str):
        turns = [("transcript", clip(tr, 600))]
    disp = payload.get("disposition") or payload.get("outcome") or ""
    summ = payload.get("summary") or (turns[-1][1] if turns else "")
    channel = payload.get("channel") or "voice_bot"
    r = svc.ingest_event(g, role, channel, "voice_bot_call",
                         f"Voice bot call: {disp or 'ended'} — {clip(summ, 150)}",
                         {"disposition": disp, "summary": clip(summ, 300), "turns": turns,
                          "duration": payload.get("duration"), "collected": payload.get("collected_fields") or {},
                          "meeting_fixed": str(disp).lower().startswith("meeting")},
                         key=f"hook:{payload.get('call_id') or sha(g, summ, disp)}")
    svc.db.ab_add(g, role, payload.get("ab_arm", ""), "voice", str(disp), "")
    return {"ok": True, "refreshed": bool(r), "version": (r or {}).get("version"), "latency_ms": (r or {}).get("latency_ms")}


@app.post("/events", dependencies=[Depends(auth)])
async def post_event(payload: dict):
    """Generic feed for any other system (app, CRM, WhatsApp delivery, executive notes)."""
    g = find_glid(payload)
    if not g or not payload.get("summary"):
        raise HTTPException(422, "glid and summary required")
    r = svc.ingest_event(g, payload.get("role", "seller"), payload.get("channel", "app"), payload.get("etype", "note"),
                         payload["summary"], payload.get("data"), payload.get("ts"), payload.get("key"))
    return {"ok": True, "duplicate": r is None, "version": (r or {}).get("version"), "latency_ms": (r or {}).get("latency_ms")}


@app.post("/channels/{channel}/incoming", dependencies=[Depends(auth)])
async def incoming(channel: str, payload: dict):
    """Chat / WhatsApp webhook: {glid, text} -> model reply grounded in the context file; the turn is written back."""
    if channel not in ("whatsapp", "chat"):
        raise HTTPException(404, "unknown channel")
    g, text = find_glid(payload), payload.get("text") or payload.get("message")
    if not g or not text:
        raise HTTPException(422, "glid and text required")
    r = chatmod.reply(svc, g, text, role=payload.get("role", "auto"), channel=channel, session=payload.get("session"))
    r.pop("prompt", None) if r.get("reply") else None
    return JSONResponse(r, status_code=200 if r.get("reply") else 503)


@app.post("/refresh", dependencies=[Depends(auth)])
def refresh(limit: int | None = None):
    return {"rendered": svc.refresh_dirty(limit)}


@app.get("/metrics")
def metrics():
    with svc.db.lock:
        lat = [r[0] for r in svc.db.conn.execute("SELECT latency_ms FROM refresh_log ORDER BY rowid DESC LIMIT 2000")]
    q = lambda p: round(sorted(lat)[min(len(lat) - 1, int(len(lat) * p))], 2) if lat else None  # noqa: E731
    return {**svc.stats(), "refresh_latency_ms": {"n": len(lat), "p50": q(0.5), "p95": q(0.95), "mean": round(statistics.mean(lat), 2) if lat else None}}
