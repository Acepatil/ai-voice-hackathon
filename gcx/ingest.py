"""Source adapters: turn the raw IndiaMART exports into normalised events + profiles.

Every adapter is idempotent (stable event keys) so re-running ingest, or replaying a live feed,
never duplicates anything. Free text is redacted (phones/e-mails/links) at this boundary.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from urllib.parse import unquote_plus
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from .config import Config
from .db import DB
from .util import clip, parse_ts, redact, sha, to_int, truthy, ts_str

csv.field_size_limit(10**9)

GENERIC_ENQ = re.compile(r"^\s*i am interested in\b", re.I)
BUYER_INTENTS = ("Msite_BL", "Buy_Product", "liveCustomer_Buyer", "Enquiry_Media_Capture")


def _read(path: Path, **kw) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8", encoding_errors="replace", **kw)


def _clean_kw(k: str) -> str:
    return re.split(r"_p\d+_mn\d+", k or "")[0].strip()


def _ev(glid, role, ts, channel, etype, summary, data, key):
    return (str(glid), role, ts, channel, etype, clip(redact(summary), 260), data, key)


# ===================================================================== seller dataset
def ingest_seller_dataset(db: DB, d: Path, log=print) -> dict:
    stats: Counter = Counter()
    f = lambda n: d / n  # noqa: E731

    # ---- lookups used across files
    enq = _read(f("gc_enquiries_received.csv"))
    msgs = _read(f("gc_enquiry_messages.csv"))
    replied = set(msgs.loc[msgs.message_from == "seller", "query_id"])
    q_info = {r.query_id: (r.product_name, r.seller_company, r.buyer_company, r.seller_city, r.buyer_city)
              for r in enq.itertuples()}
    mcat_name = enq[enq.product_name != ""].groupby("mcat_id").product_name.agg(lambda s: s.mode().iat[0]).to_dict()

    # ---- seller profiles
    sp = _read(f("gc_seller_profile.csv"))
    rows = []
    for r in sp.to_dict("records"):
        g = r["fk_glusr_usr_id"]
        p = {k: v for k, v in r.items() if k != "fk_glusr_usr_id" and v != ""}
        rows.append((g, "seller", p))
    db.set_profiles_bulk(rows)
    stats["seller_profiles"] = len(rows)
    log(f"  seller_profile: {len(rows)}")

    # ---- enquiries (seller receives, buyer sends)
    ev, bprof = [], {}
    for r in enq.itertuples():
        ts = ts_str(parse_ts(r.enquiry_date))
        if not ts:
            continue
        text = "" if (not r.message or GENERIC_ENQ.match(r.message)) else redact(r.message)
        who = r.buyer_company or "a buyer"
        loc = f" ({r.buyer_city})" if r.buyer_city else ""
        s = f"{who}{loc} enquired for {r.product_name or 'a product'}" + (f": “{clip(text, 90)}”" if text else "")
        unread = not r.first_read_date
        ev.append(_ev(r.seller_glid, "seller", ts, "enquiry", "enquiry_received", s + ("; unread" if unread else ""),
                      {"query_id": r.query_id, "buyer_glid": r.buyer_glid, "buyer_company": r.buyer_company,
                       "buyer_city": r.buyer_city, "product": r.product_name, "replied": r.query_id in replied,
                       "unread": unread, "module": r.source_module, "kw": r.search_keyword},
                      f"enq:{r.query_id}:s"))
        ev.append(_ev(r.buyer_glid, "buyer", ts, "enquiry", "enquiry_sent",
                      f"Enquired to {r.seller_company or 'a seller'}{(' (' + r.seller_city + ')') if r.seller_city else ''} "
                      f"for {r.product_name or 'a product'}" + (f": “{clip(text, 90)}”" if text else ""),
                      {"query_id": r.query_id, "seller_glid": r.seller_glid, "seller_company": r.seller_company,
                       "product": r.product_name, "replied": r.query_id in replied, "module": r.source_module,
                       "kw": r.search_keyword}, f"enq:{r.query_id}:b"))
        bprof[r.buyer_glid] = {"company_name": r.buyer_company, "city": r.buyer_city, "state": r.buyer_state,
                               "designation": r.buyer_designation, "country": r.buyer_country}
    db.add_events(ev, mark_dirty=False)
    db.set_profiles_bulk([(g, "buyer", {k: v for k, v in p.items() if v}) for g, p in bprof.items()])
    stats["enquiries"] = len(enq)
    log(f"  enquiries: {len(enq)}")

    # ---- enquiry thread messages
    ev = []
    for r in msgs.itertuples():
        ts = ts_str(parse_ts(r.reply_date))
        if not ts:
            continue
        product, s_co, b_co, *_ = q_info.get(r.query_id, ("", "", "", "", ""))
        from_seller = r.message_from == "seller"
        buyer_glid = r.receiver_glid if from_seller else r.sender_glid
        text = clip(redact(r.reply_text), 110)
        tmpl = r.template_flag not in ("", "0")
        ev.append(_ev(r.seller_glid, "seller", ts, "enquiry", "thread_msg_seller" if from_seller else "thread_msg_buyer",
                      (f"You replied{' (template)' if tmpl else ''} on {product or 'an enquiry'}: “{text}”" if from_seller
                       else f"{b_co or 'Buyer'} wrote on {product or 'an enquiry'}: “{text}”"),
                      {"query_id": r.query_id, "from": r.message_from, "template": tmpl, "text": text},
                      f"msg:{r.reply_id}:s"))
        ev.append(_ev(buyer_glid, "buyer", ts, "enquiry", "thread_msg_seller" if from_seller else "thread_msg_buyer",
                      (f"{s_co or 'Seller'} replied on {product or 'your enquiry'}: “{text}”" if from_seller
                       else f"You wrote on {product or 'an enquiry'}: “{text}”"),
                      {"query_id": r.query_id, "from": r.message_from, "template": tmpl, "text": text,
                       "seller_company": s_co}, f"msg:{r.reply_id}:b"))
    db.add_events(ev, mark_dirty=False)
    stats["enquiry_messages"] = len(msgs)
    log(f"  enquiry_messages: {len(msgs)}")

    # ---- buyer phone calls to seller
    bc = _read(f("gc_buyer_calls_received.csv"))
    ev = []
    for r in bc.itertuples():
        ts = ts_str(parse_ts(r.call_datetime))
        if not ts:
            continue
        ok = r.call_status.lower().startswith("connected")
        prod = mcat_name.get(r.mcat_id, "")
        s = f"Buyer call {'connected, talk ' + r.talk_sec + 's' if ok else 'NOT connected (missed)'}" + (f" on {prod}" if prod else "")
        ev.append(_ev(r.seller_glid, "seller", ts, "call", "buyer_call", s,
                      {"connected": ok, "talk_sec": to_int(r.talk_sec), "product": prod, "buyer_glid": r.buyer_glid,
                       "circle": r.caller_circle}, f"bc:{r.call_id}:s"))
        ev.append(_ev(r.buyer_glid, "buyer", ts, "call", "call_made",
                      f"Called a seller{' on ' + prod if prod else ''} — {'connected' if ok else 'not connected'}",
                      {"connected": ok, "seller_glid": r.seller_glid, "product": prod}, f"bc:{r.call_id}:b"))
    db.add_events(ev, mark_dirty=False)
    stats["buyer_calls"] = len(bc)
    log(f"  buyer_calls: {len(bc)}")

    # ---- buy-leads bought
    bl = _read(f("gc_buyleads_bought.csv"))
    ev = []
    for r in bl.itertuples():
        ts = ts_str(parse_ts(r.purchase_date))
        if not ts:
            continue
        kw = _clean_kw(r.keyword)
        ev.append(_ev(r.seller_glid, "seller", ts, "app", "buylead_bought",
                      f"Bought buy-lead “{kw}” ({r.credits_used} credits via {r.purchase_mode})",
                      {"keyword": kw, "credits": to_int(r.credits_used), "buyer_glid": r.buyer_glid,
                       "buylead_id": r.buylead_id}, f"bl:{r.purchase_id}:s"))
        if r.buyer_glid:
            ev.append(_ev(r.buyer_glid, "buyer", ts, "app", "requirement_bought_by_seller",
                          f"A seller bought your requirement “{kw}”", {"keyword": kw, "seller_glid": r.seller_glid},
                          f"bl:{r.purchase_id}:b"))
    db.add_events(ev, mark_dirty=False)
    stats["buyleads"] = len(bl)
    log(f"  buyleads: {len(bl)}")

    # ---- WhatsApp delivery log (campaign messages, no text)
    wm = _read(f("gc_whatsapp_messages.csv"))
    ev = []
    for r in wm.itertuples():
        ts = ts_str(parse_ts(r.entry_date) or parse_ts(r.sent_at))
        if not ts:
            continue
        read = bool(r.read_at)
        who = r.message_sender
        if who == "USER":
            s = "Seller sent a WhatsApp message to IndiaMART"
        else:
            s = f"WhatsApp “{r.campaign_name or 'message'}” from IndiaMART — {'read' if read else (r.message_status or 'sent').lower()}"
        ev.append(_ev(r.glid, "seller", ts, "whatsapp", "wa_message", s,
                      {"campaign": r.campaign_name, "sender": who, "read": read, "status": r.message_status},
                      f"wam:{r.message_id}"))
    db.add_events(ev, mark_dirty=False)
    stats["whatsapp_messages"] = len(wm)
    log(f"  whatsapp_messages: {len(wm)}")

    # ---- WhatsApp chatbot turns (text) — role by intent family
    wc = _read(f("gc_whatsapp_chatbot_conversations.csv"))
    ev = []
    for r in wc.itertuples():
        ts = ts_str(parse_ts(r.entry_date))
        if not ts:
            continue
        role = "buyer" if r.intent.startswith(BUYER_INTENTS) else "seller"
        um, br = clip(redact(r.user_message), 100), clip(redact(r.bot_reply), 120)
        m = re.match(r"\s*Enquiry for \*?(.+?)\*? has been sent", r.bot_reply or "")
        if role == "buyer" and m:  # the WhatsApp requirement flow sent an enquiry on the user's behalf
            ev.append(_ev(r.glid, role, ts, "whatsapp", "enquiry_sent", f"Sent an enquiry for {clip(m.group(1), 60)} via WhatsApp",
                          {"kw": clip(m.group(1), 60), "product": clip(m.group(1), 60), "session": r.session_id}, f"wac:{r.chat_id}"))
            continue
        s = (f"WhatsApp [{r.intent or 'chat'}] " + (f"user: “{um}” → " if um else "") + (f"bot: “{br}”" if br else "")).strip()
        ev.append(_ev(r.glid, role, ts, "whatsapp", "wa_chat_turn", s,
                      {"intent": r.intent, "user": um, "bot": br, "session": r.session_id, "action": r.action_type,
                       "feedback": r.feedback}, f"wac:{r.chat_id}"))
    db.add_events(ev, mark_dirty=False)
    stats["whatsapp_chat_turns"] = len(wc)
    log(f"  whatsapp_chatbot: {len(wc)}")

    # ---- AI-extracted phone-call details (role depends on which side the GLID was on)
    ex = _read(f("gc_buyer_call_extractions.csv"))
    ev = []
    for r in ex.itertuples():
        ts = ts_str(parse_ts(r.call_date))
        if not ts:
            continue
        role = "buyer" if r.seller_role_on_call.upper() == "BUYER" else "seller"
        langs = re.findall(r"[A-Za-z]+", r.languages or "")
        bits = [f"Phone call{' as buyer' if role == 'buyer' else ' as seller'} ({r.file_duration_sec}s)"]
        for lab, v in (("about", r.products_discussed), ("category", r.categories), ("intent", r.file_intent),
                       ("price", r.prices_quoted), ("specs", r.specs_discussed), ("stock", r.stock_status)):
            if v:
                bits.append(f"{lab}: {clip(redact(v), 60)}")
        ev.append(_ev(r.glid, role, ts, "call", "call_extraction", "; ".join(bits),
                      {"products": clip(r.products_discussed, 80), "categories": clip(r.categories, 80),
                       "intent": r.file_intent, "langs": langs, "price": clip(r.prices_quoted, 60),
                       "duration": to_int(r.file_duration_sec)}, f"ext:{r.file_id}"))
    db.add_events(ev, mark_dirty=False)
    stats["call_extractions"] = len(ex)
    log(f"  call_extractions: {len(ex)}")

    # ---- VANI bot calls (+ transcripts where present)
    turns = _read(f("gc_bot_call_turns.csv"))
    tr = defaultdict(list)
    for r in turns.itertuples():
        tr[r.attempt_id].append((to_int(r.turn_no), r.speaker, clip(redact(r.text), 140)))
    bcalls = _read(f("gc_bot_calls.csv"))
    ev = []
    for r in bcalls.itertuples():
        ts = ts_str(parse_ts(r.call_start_time))
        if not ts:
            continue
        t = [(sp, tx) for _, sp, tx in sorted(tr.get(r.attempt_id, []))]
        s = f"VANI bot call #{r.call_attempt_count}: {r.disposition_label or 'unknown'}, {r.lead_call_duration}s — {clip(redact(r.lead_call_summary), 150)}"
        ev.append(_ev(r.fk_glusr_usr_id, "seller", ts, "voice_bot", "voice_bot_call", s,
                      {"attempt_id": r.attempt_id, "disposition": r.disposition_label, "duration": r.lead_call_duration,
                       "meeting_fixed": truthy(r.meeting_fixed), "attempt_no": to_int(r.call_attempt_count),
                       "bucket": r.redis_bucket, "summary": clip(redact(r.lead_call_summary), 300),
                       "turns": t[:14]}, f"vani:{r.attempt_id}"))
    db.add_events(ev, mark_dirty=False)
    stats["bot_calls"] = len(bcalls)
    log(f"  bot_calls: {len(bcalls)} (+{len(turns)} turns)")

    # ---- IndiaMART executive calls
    xc = _read(f("gc_executive_calls.csv"))
    ev = []
    for r in xc.itertuples():
        ts = ts_str(parse_ts(r.call_start_time))
        if not ts:
            continue
        ans = r.status.lower() == "answered"
        ev.append(_ev(r.fk_glusr_usr_id, "seller", ts, "executive", "executive_call",
                      f"IndiaMART executive call {'answered, ' + r.call_duration_customer + 's talk' if ans else 'not answered'}",
                      {"answered": ans, "talk_sec": to_int(r.call_duration_customer)}, f"exec:{r.click_to_call_id}"))
    db.add_events(ev, mark_dirty=False)
    stats["executive_calls"] = len(xc)
    log(f"  executive_calls: {len(xc)}")
    return dict(stats)


# ===================================================================== buyer getContext logs
BUYER_ACT = {"Search": ("search", "app"), "ENQ": ("enquiry_sent", "enquiry"), "C2C": ("call_made", "call"),
             "BL": ("requirement_posted", "app"), "Browse": ("browse", "app"), "Others": ("browse", "app")}


def _wa_text(chat_content: str) -> str:
    try:
        j = json.loads(chat_content)
        m = j.get("message_content", {}) if isinstance(j, dict) else {}
        return m.get("text") or m.get("caption") or ""
    except Exception:
        return chat_content or ""


def parse_getcontext(glid: str, data: dict, snap_ts: str) -> tuple[dict, list]:
    """One getContext response -> (profile dict, events). Shared by the log adapter and the live client."""
    k = data.get("kycdetails", {}) or {}
    a = data.get("activitydetails", {}) or {}
    bl = data.get("BLdetails", {}) or {}
    t = data.get("ticketsdetails", {}) or {}
    c = data.get("connectdetails", {}) or {}
    clean = lambda v: "" if v in (None, "--", "-", "NA", "NULL") else v  # noqa: E731
    prof = {
        "first_name": clean(k.get("first_name")) or clean(k.get("customer_name")),
        "company_name": clean(k.get("company_name")), "city": clean(k.get("city")),
        "customer_type": clean(k.get("customer_type")), "plan": clean(k.get("glusr_customer_type")),
        "vintage": clean(k.get("customer_vintage")) or clean(k.get("registered_since_with_indiamart")),
        "mobile_verified": clean(k.get("input_mobile_verification_status")),
        "email_verified": clean(k.get("input_email_verification_status")),
        "gst": clean(k.get("gst_availability")), "high_churn": clean(k.get("high_churn")),
        "enq_30d": a.get("enq_last_30_days"), "enq_90d": a.get("enq_last_90_days"),
        "pns_recv_30d": a.get("pns_received_30_days"),
        "active_requirements": [x.get("ETO_OFR_TITILE") for x in (bl.get("active_bl_details") or []) if x.get("ETO_OFR_TITILE")][:5],
        "snapshot": snap_ts,
    }
    ev = []
    for x in a.get("BUYER_ACTIVITY") or []:
        ts = ts_str(parse_ts(x.get("ACTIVITY_TIME")))
        et, ch = BUYER_ACT.get(x.get("ACTIVITY_TYPE"), ("browse", "app"))
        if not ts:
            continue
        kw = unquote_plus(clean(x.get("KEYWORD")) or clean(x.get("CATEGORY_NAME")) or "")
        label = {"search": "Searched for", "enquiry_sent": "Sent an enquiry for", "call_made": "Called a seller about",
                 "requirement_posted": "Posted a requirement for", "browse": "Browsed"}[et]
        ev.append(_ev(glid, "buyer", ts, ch, et, f"{label} {kw or 'IndiaMART'}",
                      {"kw": kw, "category": clean(x.get("CATEGORY_NAME")), "seller_glid": clean(x.get("SELLER_GLUSR_ID")) if x.get("SELLER_GLUSR_ID") != "0" else "",
                       "act": x.get("ACTIVITY_TYPE")}, f"gc:{glid}:{x.get('ACTIVITY_TIME')}:{x.get('ACTIVITY_ID')}:{sha(kw)[:6]}"))
    le = a.get("LAST_ENQUIRY") or {}
    if le.get("ENQUIRY_ID"):
        ev.append(_ev(glid, "buyer", snap_ts, "enquiry", "enquiry_sent",
                      f"Last enquiry: {clip(le.get('TITLE') or le.get('PRODUCT_NAME'), 70)}"
                      + (f" — “{clip(redact(le.get('DESCRIPTION')), 80)}”" if le.get("DESCRIPTION") and not GENERIC_ENQ.match(le["DESCRIPTION"]) else ""),
                      {"kw": le.get("TITLE"), "query_id": le["ENQUIRY_ID"]}, f"gc:{glid}:enq:{le['ENQUIRY_ID']}"))
    for lst in ("LAST_WA9696_CHAT", "LAST_WA8181_CHAT"):
        for m in a.get(lst) or []:
            ts = ts_str(parse_ts(m.get("ENTRY_DATE")))
            txt = redact(_wa_text(m.get("CHAT_CONTENT", "")))
            if ts and txt:
                who = m.get("MESSAGE_SENDER", "")
                ev.append(_ev(glid, "buyer", ts, "whatsapp", "wa_chat_turn",
                              f"WhatsApp {'from IndiaMART' if who == 'API' else 'from user'}: “{clip(txt, 130)}”",
                              {"sender": who, "text": clip(txt, 200)}, f"gc:{glid}:wa:{ts}:{sha(txt)[:6]}"))
    for n in k.get("last_call_details") or []:
        ts = ts_str(parse_ts(n.get("ENTERED_ON")))
        if ts and n.get("COMMENTS"):
            ev.append(_ev(glid, "buyer", ts, "executive", "executive_note", f"Executive note: “{clip(redact(n['COMMENTS']), 130)}”",
                          {"text": clip(redact(n["COMMENTS"]), 200)}, f"gc:{glid}:note:{ts}:{sha(n['COMMENTS'])[:6]}"))
    for tk in (t.get("wip_tickets") or [])[:2] if isinstance(t.get("wip_tickets"), list) else []:
        ts = ts_str(parse_ts(tk.get("ticket_issue_date")))
        if ts:
            ev.append(_ev(glid, "buyer", ts, "app", "ticket_open", f"Open ticket: {clip(redact(tk.get('ticket_opening_comments') or tk.get('ticket_type')), 100)}",
                          {"status": tk.get("ticket_status")}, f"gc:{glid}:tk:{tk.get('ticket_id')}"))
    return prof, ev


_ACT = re.compile(r'\{"ACTIVITY_ID":"(\d+)","ACTIVITY_TIME":"(\d+)","ACTIVITY_TYPE":"(\w+)","CATEGORY_ID":"(\d+)",'
                  r'"CATEGORY_NAME":"([^"]*)","KEYWORD":"([^"]*)","PRODUCT_ID":"(\d+)","SELLER_GLUSR_ID":"(\d+)"\}')


def salvage_getcontext(raw: str) -> tuple[str, dict, list] | None:
    """The export truncates very large responses at ~32k chars, which breaks the JSON. Those are exactly the
    most active users, so recover the glid, KYC basics and every complete BUYER_ACTIVITY item with regexes."""
    t = raw.replace('\\\\"', '"').replace('\\"', '"').replace('\\"', '"')
    m = re.search(r'"glid":"(\d+)"', t)
    if not m:
        return None
    glid = m.group(1)
    up = re.search(r'"upload_time":"([^"]+)"', t)
    snap = ts_str(parse_ts(up.group(1))) if up else ""
    grab = lambda k: ("" if (v := (re.search(rf'"{k}":"([^"]*)"', t) or [None, ""])[1]) in ("--", "-", "NA") else v)  # noqa: E731
    prof = {"first_name": grab("first_name"), "company_name": grab("company_name"), "city": grab("city"),
            "customer_type": grab("customer_type"), "snapshot": snap, "partial": True}
    acts = [{"ACTIVITY_ID": a, "ACTIVITY_TIME": b, "ACTIVITY_TYPE": c, "CATEGORY_ID": d, "CATEGORY_NAME": e,
             "KEYWORD": f, "PRODUCT_ID": g, "SELLER_GLUSR_ID": h} for a, b, c, d, e, f, g, h in _ACT.findall(t)]
    _, ev = parse_getcontext(glid, {"activitydetails": {"BUYER_ACTIVITY": acts}}, snap)
    return glid, prof, ev


def ingest_getcontext_log(db: DB, path: Path, log=print) -> dict:
    """The buyer-side export is a log of getContext API calls: GLUSR_ID in col 2, JSON in RESPONSE (col 6)."""
    ok = bad = salvaged = 0
    best: dict[str, tuple[str, dict]] = {}
    events = []
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        rd = csv.reader(fh)
        next(rd, None)
        for r in rd:
            if len(r) < 7:
                continue
            try:
                outer = json.loads(r[6])
                inner = json.loads(outer["API_RESPONSE_JSON"])
                data = inner["data"]
                glid = str(data["ActualData"]["glid"])
            except Exception:
                sv = salvage_getcontext(r[6]) if r[6] else None
                if sv:
                    glid, prof, ev = sv
                    salvaged += 1
                    if glid not in best or prof["snapshot"] >= best[glid][0]:
                        best[glid] = (prof["snapshot"], prof)
                    events.extend(ev)
                elif r[6]:
                    bad += 1
                continue
            snap = ts_str(parse_ts(data["ActualData"]["upload_metadata"]["upload_time"])) or ts_str(parse_ts(r[0].replace("@", ""))) or ""
            prof, ev = parse_getcontext(glid, data, snap)
            ok += 1
            if glid not in best or snap >= best[glid][0]:
                best[glid] = (snap, prof)
            events.extend(ev)
    db.add_events(events, mark_dirty=False)
    db.set_profiles_bulk([(g, "buyer", {k: v for k, v in p.items() if v not in ("", None, [])}) for g, (_, p) in best.items()])
    log(f"  getContext log: {ok} parsed, {salvaged} truncated rows salvaged by regex, {bad} unusable, {len(best)} GLIDs, {len(events)} events")
    return {"getcontext_ok": ok, "getcontext_salvaged": salvaged, "getcontext_bad": bad, "getcontext_glids": len(best), "getcontext_events": len(events)}


def ingest_all(cfg: Config, db: DB, log=print) -> dict:
    out = {}
    sd, bc = cfg.path("seller_dir"), cfg.path("buyer_csv")
    if sd.exists():
        log(f"Ingesting seller dataset from {sd}")
        out.update(ingest_seller_dataset(db, sd, log))
    else:
        log(f"! seller_dir not found: {sd}")
    if bc.exists():
        log(f"Ingesting getContext log from {bc}")
        out.update(ingest_getcontext_log(db, bc, log))
    else:
        log(f"! buyer_csv not found: {bc}")
    # everything is new: mark every GLID dirty once
    with db.lock:
        db.conn.execute("INSERT OR IGNORE INTO dirty(glid,role,since) SELECT DISTINCT glid,role,datetime('now') FROM events")
        db.conn.execute("INSERT OR IGNORE INTO dirty(glid,role,since) SELECT glid,role,datetime('now') FROM profiles")
    return out


if __name__ == "__main__":
    from .config import load
    c = load()
    ingest_all(c, DB(c.path("db")))
