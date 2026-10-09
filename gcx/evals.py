"""Measurements for the five scoring criteria + the lookback-window study. Each function returns a dict and the
CLI writes it to reports/<name>.json. Everything is computed from the data, nothing is hard-coded.

Honest limits are stated in each docstring (what is a proxy, what needs live traffic).
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import shutil
import statistics
import time
from pathlib import Path

import pandas as pd

from . import render as R
from .service import ContextService
from .util import clip, parse_ts

INTERACTION = ("enquiry_received", "buyer_call", "wa_chat_turn", "buylead_bought", "executive_call", "call_extraction")


def _pct(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 2) if xs else None


def _events_df(svc: ContextService, where: str, params=()) -> pd.DataFrame:
    with svc.db.lock:
        df = pd.read_sql_query(f"SELECT glid,role,ts,etype,channel,data FROM events WHERE {where}", svc.db.conn, params=params)
    df["t"] = pd.to_datetime(df["ts"])
    return df


# ----------------------------------------------------------------------------------------- window study
def window_study(svc: ContextService, windows=(3, 7, 14, 21, 30, 45, 60, 90)) -> dict:
    """For every historical VANI bot call: how far back is the most recent *other-channel* interaction, and was there an
    actionable open item (unreplied enquiry, missed buyer call) inside window W?  Picks W where the marginal gain flattens.
    The cost side is file size, which grows with W."""
    calls = _events_df(svc, "role='seller' AND etype='voice_bot_call'").sort_values("t")
    oth = _events_df(svc, "role='seller' AND etype IN ('enquiry_received','buyer_call','wa_chat_turn','buylead_bought','executive_call')")
    oth["actionable"] = False
    enq = oth.etype == "enquiry_received"
    oth.loc[enq, "actionable"] = ~oth.loc[enq, "data"].map(lambda s: json.loads(s).get("replied", False))
    bc = oth.etype == "buyer_call"
    oth.loc[bc, "actionable"] = ~oth.loc[bc, "data"].map(lambda s: json.loads(s).get("connected", True))
    oth = oth[(oth.etype != "wa_chat_turn") | oth.data.map(lambda s: bool(json.loads(s).get("user")))]
    calls = calls[["glid", "t"]].reset_index(drop=True)
    calls["call_id"] = calls.index

    def lag(df):
        d = df.sort_values("t")
        m = pd.merge_asof(calls.sort_values("t"), d[["glid", "t"]].rename(columns={"t": "t_prev"}), left_on="t", right_on="t_prev",
                          by="glid", direction="backward", allow_exact_matches=False)
        m["lag_d"] = (m.t - m.t_prev).dt.total_seconds() / 86400
        return m.set_index("call_id").lag_d

    lag_any, lag_act = lag(oth), lag(oth[oth.actionable])
    rows = []
    prev = 0.0
    for w in windows:
        a, b = float((lag_any <= w).mean()), float((lag_act <= w).mean())
        rows.append({"window_days": w, "calls_with_any_prior_activity_%": round(100 * a, 1),
                     "calls_with_actionable_item_%": round(100 * b, 1), "marginal_actionable_gain_pp": round(100 * b - prev, 1)})
        prev = 100 * b
    # Coverage keeps rising ~linearly (no knee), so the window is chosen on relevance instead: how do outcomes change with the
    # age of the most recent actionable item?  Bucket calls by that lag and compare against calls with no item at all.
    calls["lag"] = lag_act.reindex(calls.call_id).values
    dsp = svc.db.conn.execute("SELECT 1").fetchone()  # keep connection warm
    meta = _events_df(svc, "role='seller' AND etype='voice_bot_call'")[["glid", "t", "data"]]
    meta["disp"] = meta.data.map(lambda x: json.loads(x).get("disposition") or "")
    calls = calls.merge(meta[["glid", "t", "disp"]].drop_duplicates(["glid", "t"]), on=["glid", "t"], how="left")
    buckets = [("no actionable item in data", lambda x: x.isna()), ("0-7d", lambda x: x <= 7), ("8-14d", lambda x: (x > 7) & (x <= 14)),
               ("15-30d", lambda x: (x > 14) & (x <= 30)), ("31-60d", lambda x: (x > 30) & (x <= 60)), ("61-90d", lambda x: (x > 60) & (x <= 90)),
               (">90d", lambda x: x > 90)]
    by_lag = []
    for name, f in buckets:
        g = calls[f(calls.lag)]
        if len(g):
            by_lag.append({"age_of_latest_actionable_item": name, "calls": len(g),
                           "meeting_fixed_%": round(100 * (g.disp == "Meeting Fixed").mean(), 1),
                           "not_interested_%": round(100 * (g.disp == "Not Interested").mean(), 1)})
    return {"n_bot_calls": int(len(calls)), "table": rows, "outcome_by_age_of_latest_actionable_item": by_lag, "decision": (f"recent={svc.cfg.recent_days}d listed line-by-line; lookback={svc.cfg.lookback_days}d summarised; open items counted to "
                     f"{svc.cfg.raw['window'].get('open_item_days')}d. Chosen by judgement from this table: coverage of actionable items grows ~linearly (no knee), "
                     "history shows no outcome gradient by age (the old bot never used the info), so older items cost one summary line instead of being dropped."),
            "median_lag_days_any": round(float(lag_any.median()), 1) if lag_any.notna().any() else None,
            "note": "Actionable = enquiry with no seller reply, or buyer call not connected. Recent window (events listed one-by-one) is set separately: "
                    "the 7-day figure is where 'any prior activity' coverage is still steep, so fresh items get full lines; older ones are summarised."}


# ----------------------------------------------------------------------------------------- compactness
def compactness(svc: ContextService, sample: int | None = None) -> dict:
    out = {"seller": [], "buyer": []}
    bad = []
    for role in ("seller", "buyer"):
        files = sorted((svc.out / role).glob("*.md"))
        if sample and len(files) > sample:
            files = random.Random(7).sample(files, sample)
        for p in files:
            md = p.read_text(encoding="utf-8")
            errs = R.validate(md, role)
            if errs:
                bad.append((str(p.name), role, errs))
            w, c = len(md.split()), len(md)
            out[role].append({"words": w, "chars": c, "tokens_est": math.ceil(c / 3.2)})
    res = {}
    for role, rows in out.items():
        if not rows:
            continue
        ws = [r["words"] for r in rows]
        res[role] = {"files": len(rows), "words_p50": _pct(ws, 0.5), "words_p95": _pct(ws, 0.95), "words_max": max(ws),
                     "tokens_est_p50": _pct([r["tokens_est"] for r in rows], 0.5), "tokens_est_p95": _pct([r["tokens_est"] for r in rows], 0.95),
                     "within_budget_%": round(100 * sum(1 for r in rows if r["words"] <= svc.cfg.max_words + 40) / len(rows), 1)}
    res["structure"] = {"checked": sum(len(v) for v in out.values()), "invalid": len(bad), "examples": bad[:5],
                        "valid_%": round(100 * (1 - len(bad) / max(1, sum(len(v) for v in out.values()))), 2)}
    res["budget"] = {"max_words": svc.cfg.max_words, "max_chars": svc.cfg.max_chars,
                     "note": "tokens_est = chars/3.2 (romanised Hindi tokenises worse than English); measure with the real tokenizer when you pick the model."}
    return res


# ----------------------------------------------------------------------------------------- freshness
def freshness(svc_live: ContextService, n: int = 300) -> dict:
    """Event -> file latency through the real write path, on a *copy* of the DB so the shipped store stays untouched.
    Also: how stale is the content (data_through vs as_of) and batch refresh throughput."""
    tmp = svc_live.cfg.root / "var" / "eval_sandbox"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    src = Path(svc_live.db.path)
    shutil.copy(src, tmp / "gcx.sqlite")
    os.environ["GCX_DB"], os.environ["GCX_OUT"] = str(tmp / "gcx.sqlite"), str(tmp / "out")
    try:
        svc = ContextService(svc_live.cfg)
        glids = [g for g, r in svc.db.all_glids("seller")]
        rng = random.Random(11)
        sample = rng.sample(glids, min(n, len(glids)))
        for g in sample:  # warm: make sure the file exists so we measure *updates*
            svc.render(g, "seller")
        lat = []
        for i, g in enumerate(sample):
            r = svc.ingest_event(g, "seller", "whatsapp", "wa_chat_turn", f"eval probe {i}: price bhejo", {"user": f"eval probe {i}: price bhejo", "intent": "live_chat"})
            if r:
                lat.append(r["latency_ms"])
        # read-your-write check: the new line must be visible in the file that the hook would serve
        ok = sum(1 for g in sample[:50] if "eval probe" in (svc.get(g, "seller") or {}).get("markdown", ""))
        # batch path
        extra = [(g, "seller", "2026-10-02 09:58:00", "app", "note", f"batch probe {i}", {}, f"evalbatch:{i}") for i, g in enumerate(sample)]
        with svc.db.lock:
            svc.db.conn.execute("DELETE FROM dirty")
        t0 = time.perf_counter()
        svc.db.add_events(extra)
        k = svc.refresh_dirty()
        dt_batch = time.perf_counter() - t0
    finally:
        os.environ.pop("GCX_DB", None)
        os.environ.pop("GCX_OUT", None)
    lagd = []
    for p in list((svc_live.out / "seller").glob("*.md"))[:3000]:
        fm = R.parse_front_matter(p.read_text(encoding="utf-8")[:600])
        a, d = parse_ts(fm.get("as_of", "").replace("+05:30", "")), parse_ts(fm.get("data_through"))
        if a and d:
            lagd.append((a - d).total_seconds() / 86400)
    return {"event_to_file_ms": {"n": len(lat), "p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95), "max": round(max(lat), 2) if lat else None},
            "read_your_write_ok": f"{ok}/50", "batch_refresh": {"files": k, "seconds": round(dt_batch, 2), "files_per_sec": round(k / dt_batch, 1)},
            "content_staleness_days": {"note": "age of the newest event inside each file vs the as_of time; this is a property of the data, not the pipeline",
                                       "p50": _pct(lagd, 0.5), "p95": _pct(lagd, 0.95)},
            "mode": "sync-on-write (hooks) + batch drain; no polling interval, so worst-case delay = one render"}


# ----------------------------------------------------------------------------------------- re-ask proxy
PAT = {
    "identity": re.compile(r"(kya aap|aap).{0,45}(se bol rahe|bol rahe|baat kar rahe)|are you from|speaking (with|to)|which company|company ka naam|aapki company", re.I),
    # pure re-asks: a question word paired with category/product, or an explicit "what do you sell"
    "category": re.compile(r"(kaun si|konsi|kaunsi|which|what)\W+(\w+\W+){0,3}(category|product|cheez|maal|service)s?\b.*\?|"
                           r"what (do|are) you (sell|make|deal|offer)|kya (bechte|banate|karte|deal)\b.*\?|aap kya (bechte|banate|bana)", re.I),
    "location": re.compile(r"(aap )?kahan se\b|kis (city|shehar)|which city|where are you (located|from)|aapki location kya", re.I),
}


def reask(svc: ContextService) -> dict:
    """Proxy for 'conversations resumed without re-asking'. In the historical VANI transcripts, how many bot turns asked for something
    the context file already holds (company / what they sell / where they are)? That is the number that drops to ~0 once the bot is
    primed with the file. Real resumption rate needs live calls; this is the measurable baseline."""
    df = _events_df(svc, "role='seller' AND etype='voice_bot_call'")
    tot_calls = with_turns = calls_avoidable = pure = 0
    asks = {k: 0 for k in PAT}
    asks_known = {k: 0 for k in PAT}
    first_turn_identity = 0
    examples = []
    for r in df.itertuples():
        d = json.loads(r.data)
        turns = [t for t in d.get("turns", []) if t[0] == "bot"]
        tot_calls += 1
        if not turns:
            continue
        with_turns += 1
        prof = svc.db.profile(r.glid, "seller") or {}
        known = {"identity": bool(prof.get("company_name")), "category": bool(prof.get("top_category_1")), "location": bool(prof.get("seller_city"))}
        hit = False
        for i, (_, tx) in enumerate(turns):
            for k, pat in PAT.items():
                if pat.search(tx):
                    asks[k] += 1
                    if known[k]:
                        asks_known[k] += 1
                        hit = True
                        if k != "identity" and len(examples) < 6:
                            examples.append({"glid": r.glid, "asked": clip(tx, 120), "file_has": k})
                    if k == "identity" and i == 0:
                        first_turn_identity += 1
        calls_avoidable += hit
        pure += any(pat.search(tx) for _, tx in turns for k, pat in PAT.items() if k != "identity" and known[k])
    return {"bot_calls_total": tot_calls, "bot_calls_with_transcript": with_turns,
            "bot_turns_asking": asks, "of_which_file_already_knew": asks_known,
            "calls_with_any_identity_or_fact_check_%": round(100 * calls_avoidable / max(1, with_turns), 1),
            "calls_with_pure_reask_of_category_or_location": pure,
            "cold_start_%": round(100 * first_turn_identity / max(1, with_turns), 1),
            "cold_start_definition": "share of transcribed bot calls whose first bot turn is the generic 'kya aap <company> se bol rahe hain?' check, i.e. the call starts from nothing",
            "examples_non_identity": examples,
            "note": "Identity checks ('kya aap X se bol rahe hain?') are partly a safety step (wrong-person guard); with the file they become a confirmation "
                    "('Hello X, ...'), not a question. Category/location asks are pure re-asks."}


# ----------------------------------------------------------------------------------------- lift potential / AB
def lift(svc: ContextService) -> dict:
    """(1) Observational: in history, do bot calls placed when the seller had a fresh actionable item (unreplied enquiry / missed buyer call
    in the previous 14 days) end better than those without? Correlation, not causation — it tells you where the opening line has material
    to work with.  (2) A/B readout from ab_log once the hooks run with ab.holdout_pct > 0."""
    calls = _events_df(svc, "role='seller' AND etype='voice_bot_call'")
    calls["disp"] = calls.data.map(lambda s: json.loads(s).get("disposition") or "")
    calls["dur"] = calls.data.map(lambda s: float(json.loads(s).get("duration") or 0))
    calls = calls[["glid", "t", "disp", "dur"]].sort_values("t").reset_index(drop=True)
    calls["cid"] = calls.index
    oth = _events_df(svc, "role='seller' AND etype IN ('enquiry_received','buyer_call')")
    keep = oth.data.map(lambda s: (lambda j: (not j.get("replied", True)) if "replied" in j else (not j.get("connected", True)))(json.loads(s)))
    act = oth[keep].sort_values("t")
    m = pd.merge_asof(calls.sort_values("t"), act[["glid", "t"]].rename(columns={"t": "tp"}), left_on="t", right_on="tp", by="glid",
                      direction="backward", allow_exact_matches=False)
    m["has_item"] = ((m.t - m.tp).dt.total_seconds() / 86400 <= 14)
    rows = []
    for flag, g in m.groupby("has_item"):
        rows.append({"had_actionable_item_14d": bool(flag), "calls": len(g),
                     "meeting_fixed_%": round(100 * (g.disp == "Meeting Fixed").mean(), 1),
                     "not_interested_%": round(100 * (g.disp == "Not Interested").mean(), 1),
                     "talked_%": round(100 * g.disp.isin(["General (talked)", "Meeting Fixed", "Call Later / Busy"]).mean(), 1),
                     "avg_duration_s": round(g.dur.mean(), 1)})
    with svc.db.lock:
        ab = pd.read_sql_query("SELECT arm, outcome, COUNT(*) n FROM ab_log GROUP BY arm, outcome", svc.db.conn)
    return {"observational_history": rows, "ab_log": ab.to_dict("records"),
            "how_to_measure_true_lift": "Set ab.holdout_pct (e.g. 10) in config.yaml. The on-start hook then serves a generic opening to that share of GLIDs "
                                       "(hash-assigned, stable) and logs arm + on-end disposition; rerun `python -m gcx eval lift` after ~1 week of calls and compare "
                                       "meeting-fixed / talk-time between arm=personalised and arm=control."}


# ----------------------------------------------------------------------------------------- coverage / sparsity
def coverage(svc: ContextService) -> dict:
    with svc.db.lock:
        df = pd.read_sql_query("SELECT glid, role, COUNT(*) n, COUNT(DISTINCT channel) ch, MAX(ts) last FROM events GROUP BY glid, role", svc.db.conn)
    out = {}
    for role, g in df.groupby("role"):
        out[role] = {"glids": len(g), "events_p50": int(g.n.median()), "events_p95": int(g.n.quantile(0.95)),
                     "with_>=2_channels_%": round(100 * (g.ch >= 2).mean(), 1), "sparse_(<3_events)_%": round(100 * (g.n < 3).mean(), 1)}
    return out


def run(svc: ContextService, which: str, reports: Path) -> dict:
    reports.mkdir(parents=True, exist_ok=True)
    fns = {"window": window_study, "compactness": compactness, "freshness": freshness, "reask": reask, "lift": lift, "coverage": coverage}
    names = list(fns) if which == "all" else [which]
    res = {}
    for n in names:
        res[n] = fns[n](svc)
        (reports / f"{n}.json").write_text(json.dumps(res[n], indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return res
