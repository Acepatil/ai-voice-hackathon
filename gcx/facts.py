"""Turn a GLID's events + profile into a small, structured fact set (JSON-serialisable).

Pure and deterministic given (events, profile, as_of, config); only the optional digest line uses the LLM.
Windows: events newer than `recent_days` are listed one by one, events up to `lookback_days` are summarised,
anything older is dropped except durable facts (flags, KYC, last bot outcome, language).
"""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter

from .config import Config
from .db import DB
from .llm import Sarvam
from .util import ago, clip, detect_lang, truthy, vote_lang

MONTH = "%b %d"
PENDING_INTENTS = {"Photo uploaded but not added as a product as ..": "a product photo you uploaded isn't added as a product yet",
                   "size of image is less than 500*500": "an image you sent was too small (needs 500x500)",
                   "Your document has been uploaded on your Selle..": "a document you uploaded is under review",
                   "WA_Callback_Connect_Q": "a callback request is open"}
PENDING_SHORT = {"Photo uploaded but not added as a product as ..": "product photo", "size of image is less than 500*500": "image upload",
                 "Your document has been uploaded on your Selle..": "document upload", "WA_Callback_Connect_Q": "callback request"}


def _d(as_of: dt.datetime, e: dict) -> float:
    return max(0.0, (as_of - e["dt"]).total_seconds() / 86400) if e.get("dt") else 9999.0


def _fmt(e: dict) -> str:
    return e["dt"].strftime(MONTH) if e.get("dt") else "?"


def _top(counter: Counter, n: int) -> list[str]:
    return [k for k, _ in counter.most_common(n) if k]


def _add_recent(recent: list, used: Counter, caps: dict, e: dict) -> None:
    """Add one event to the 'recent' list, folding identical consecutive lines into '(×n)'."""
    for r in recent:
        if r["text"].split(" (×")[0] == e["summary"] and r["channel"] == e["channel"]:
            r["n"] = r.get("n", 1) + 1
            r["text"] = f"{e['summary']} (×{r['n']})"
            return
    if used[e["etype"]] >= caps.get(e["etype"], 1):
        return
    used[e["etype"]] += 1
    recent.append({"ts": e["ts"], "channel": e["channel"], "text": e["summary"]})


# ------------------------------------------------------------------------------------- language
def _lang_votes(role: str, evs: list[dict], as_of: dt.datetime) -> list[tuple[str | None, float]]:
    votes = []
    for e in evs[:200]:
        w = 1.0 / (1.0 + _d(as_of, e) / 30)
        d = e["data"]
        if e["etype"] == "voice_bot_call":
            for sp, tx in d.get("turns", []):
                if sp == "seller":
                    votes.append((detect_lang(tx), w))
        elif e["etype"] == "wa_chat_turn":
            votes.append((detect_lang(d.get("user") or (d.get("text") if d.get("sender") == "USER" else "")), w))
        elif e["etype"] == "thread_msg_seller" and role == "seller" and not d.get("template"):
            votes.append((detect_lang(d.get("text")), w))
        elif e["etype"] == "thread_msg_buyer" and role == "buyer":
            votes.append((detect_lang(d.get("text")), w * 0.5))
        elif e["etype"] == "call_extraction":
            langs = [x.lower() for x in d.get("langs", [])]
            votes.append(("hinglish" if "hindi" in langs else "en" if "english" in langs else None, w * 2))
    return votes


# ------------------------------------------------------------------------------------- seller
def seller_facts(db: DB, glid: str, evs: list[dict], prof: dict, as_of: dt.datetime, cfg: Config, llm: Sarvam | None) -> dict:
    R, L = cfg.recent_days, cfg.lookback_days
    inwin = [e for e in evs if _d(as_of, e) <= L]
    by = lambda et, days=L: [e for e in evs if e["etype"] == et and _d(as_of, e) <= days]  # noqa: E731

    ident = {
        "name": prof.get("company_name", ""), "city": prof.get("seller_city", ""), "state": prof.get("seller_state", ""),
        "business_type": prof.get("business_type", ""), "turnover": prof.get("annual_turnover", ""),
        "nature": prof.get("nature_of_business", ""),
        "categories": [prof[k] for k in ("top_category_1", "top_category_2", "top_category_3") if prof.get(k)],
        "plan": "paid" if truthy(prof.get("is_paid")) else (prof.get("customer_type") or "free"),
        "listing": prof.get("listing_status", ""),
        "kyc": {"mobile": truthy(prof.get("mobile_verified_flag")), "email": truthy(prof.get("email_verified_flag")),
                "gst": truthy(prof.get("gst_verified_flag")), "pan": truthy(prof.get("is_pan_number_available"))},
        "products": prof.get("eng_product_count", ""), "catalog_score": prof.get("eng_cqs", ""),
    }

    flags = []
    if truthy(prof.get("do_not_call_requested")):
        flags.append("DO NOT CALL — seller asked not to be called. Do not start a call; chat/WhatsApp replies only if they write first.")
    if truthy(prof.get("asked_if_talking_to_bot")):
        flags.append("Has asked whether they are talking to a bot — say upfront you are IndiaMART's AI assistant.")
    if truthy(prof.get("showed_frustration")):
        flags.append("Showed frustration on a past call — keep it short, lead with value, offer a human callback.")
    if truthy(prof.get("already_in_touch_with_executive")):
        flags.append("Already in touch with an IndiaMART executive — don't pitch as if new; reference that.")
    if prof.get("past_objections"):
        flags.append("Past objections: " + clip(prof["past_objections"], 110))

    # ---- open items
    open_items = []
    unrep = [e for e in by("enquiry_received") if not e["data"].get("replied")]
    if unrep:
        top = unrep[0]["data"]
        open_items.append({"kind": "unreplied_enquiries", "n": len(unrep),
                           "text": f"{len(unrep)} enquiries without your reply in {L}d; latest {_fmt(unrep[0])}: "
                                   f"{top.get('buyer_company') or 'a buyer'}{' (' + top['buyer_city'] + ')' if top.get('buyer_city') else ''}"
                                   f" for {top.get('product') or 'a product'}"})
    O = int(cfg.raw["window"].get("open_item_days", 60))
    old_unrep = [e for e in evs if e["etype"] == "enquiry_received" and L < _d(as_of, e) <= O and not e["data"].get("replied")]
    if old_unrep:
        open_items.append({"kind": "older_unreplied", "n": len(old_unrep), "text": f"{len(old_unrep)} older enquiries ({L + 1}–{O}d) are also still unreplied"})
    missed = [e for e in by("buyer_call") if not e["data"].get("connected")]
    if missed:
        open_items.append({"kind": "missed_buyer_calls", "n": len(missed),
                           "text": f"{len(missed)} buyer calls missed in {L}d (latest {_fmt(missed[0])})"})
    bot = [e for e in evs if e["etype"] == "voice_bot_call"]
    last_bot = bot[0] if bot else None
    if last_bot:
        dsp = last_bot["data"].get("disposition") or "unknown"
        age = _d(as_of, last_bot)
        if age <= L and dsp.startswith("Call Later"):
            open_items.append({"kind": "call_later", "text": f"On the {_fmt(last_bot)} bot call they asked to be called later"})
        if age <= L and dsp == "Meeting Fixed":
            open_items.append({"kind": "meeting", "text": f"A meeting was fixed on the {_fmt(last_bot)} bot call — confirm, don't re-pitch"})
    pend = [e for e in by("wa_chat_turn") if e["data"].get("intent") in PENDING_INTENTS]
    if pend:
        open_items.append({"kind": "wa_pending", "text": f"WhatsApp {_fmt(pend[0])}: {PENDING_INTENTS[pend[0]['data']['intent']]}"})
    xc = [e for e in by("executive_call")]
    wa_fresh = [e for e in evs if e["etype"] == "wa_chat_turn" and (e["data"].get("user") or "") and _d(as_of, e) <= 3
               and (len(e["data"]["user"]) >= 12 or e["data"].get("intent") == "live_chat")]
    if xc and not xc[0]["data"].get("answered"):
        open_items.append({"kind": "exec_missed", "text": f"IndiaMART executive tried calling on {_fmt(xc[0])}; not answered"})

    # ---- recent (≤ R days), capped per channel
    caps = {"enquiry_received": 3, "thread_msg_buyer": 1, "thread_msg_seller": 1, "buyer_call": 2, "buylead_bought": 2,
            "wa_chat_turn": 3, "voice_bot_call": 2, "executive_call": 1, "wa_message": 2, "call_extraction": 1}
    used, recent = Counter(), []
    for e in evs:
        if _d(as_of, e) > R:
            continue
        if e["etype"] == "wa_message" and e["data"].get("sender") != "USER" and not e["data"].get("read"):
            continue  # undelivered/unread campaigns are noise
        _add_recent(recent, used, caps, e)

    # ---- window (R..L days): counts + digest
    older = [e for e in inwin if _d(as_of, e) > R]
    cnt = Counter(e["etype"] for e in older)
    parts = []
    if cnt["enquiry_received"]:
        un = sum(1 for e in older if e["etype"] == "enquiry_received" and not e["data"].get("replied"))
        tops = _top(Counter(e["data"].get("product") for e in older if e["etype"] == "enquiry_received"), 2)
        parts.append(f"{cnt['enquiry_received']} enquiries ({un} unreplied){' — mostly ' + ', '.join(tops) if tops else ''}")
    if cnt["buyer_call"]:
        ms = sum(1 for e in older if e["etype"] == "buyer_call" and not e["data"].get("connected"))
        parts.append(f"{cnt['buyer_call']} buyer calls ({ms} missed)")
    if cnt["buylead_bought"]:
        kws = _top(Counter(e["data"].get("keyword") for e in older if e["etype"] == "buylead_bought"), 2)
        parts.append(f"{cnt['buylead_bought']} buy-leads bought{' (' + ', '.join(kws) + ')' if kws else ''}")
    wa_read = [e for e in older if e["etype"] == "wa_message" and e["data"].get("sender") != "USER"]
    if wa_read:
        parts.append(f"{len(wa_read)} WhatsApp campaign msgs ({sum(1 for e in wa_read if e['data'].get('read'))} read)")
    wa_user = [e for e in older if e["etype"] == "wa_chat_turn" and e["data"].get("user")]
    if wa_user:
        parts.append(f"{len(wa_user)} WhatsApp chats with the bot, topics: " + ", ".join(_top(Counter(e['data'].get('intent') for e in wa_user), 2)))
    if cnt["voice_bot_call"]:
        parts.append(f"{cnt['voice_bot_call']} bot calls")
    window_line = "; ".join(parts)

    digest_items = [e["data"].get("summary") or e["summary"] for e in inwin
                    if e["etype"] in ("voice_bot_call", "wa_chat_turn", "thread_msg_buyer", "call_extraction")][:8]
    lang_votes = _lang_votes("seller", evs, as_of)
    lang = vote_lang(lang_votes + [(detect_lang(prof.get("past_questions_asked")), 0.5)])
    digest = llm.digest("recent seller interactions", digest_items, lang) if (llm and len(digest_items) >= 3) else None

    # ---- history (durable)
    history = []
    if last_bot and _d(as_of, last_bot) > R:  # inside the recent window it is already listed under "Last N days"
        d = last_bot["data"]
        history.append(f"Last VANI bot call {_fmt(last_bot)} (#{d.get('attempt_no', '?')}): {d.get('disposition') or 'unknown'}, "
                       f"{d.get('duration', '?')}s — {clip(d.get('summary'), 130)}")
    na = prof.get("bot_attempts")
    if na:
        history.append(f"Bot calls so far: {prof.get('bot_answered', '?')} answered of {na}; "
                       f"meetings fixed {prof.get('calls_meeting_fixed', 0)}, not interested {prof.get('calls_not_interested', 0)}, "
                       f"call-later {prof.get('calls_call_later', 0)}")
    if prof.get("eng_enq_received_90d"):
        history.append(f"90d: {prof['eng_enq_received_90d']} enquiries received, {prof.get('eng_enq_replies_90d', 0)} replied; "
                       f"pickup ratio {prof.get('eng_pickup_ratio_90d', '?')}")

    data_through = evs[0]["ts"] if evs else None
    return {"identity": ident, "flags": flags, "open_items": open_items, "recent": recent, "window": window_line,
            "digest": digest, "history": history, "language": lang, "data_through": data_through,
            "do_not_call": truthy(prof.get("do_not_call_requested")),
            "signals": {"unreplied": len(unrep), "missed_calls": len(missed), "last_bot": (last_bot["data"].get("disposition") if last_bot else None),
                        "last_bot_age": round(_d(as_of, last_bot), 1) if last_bot else None},
            "_unrep": [e["data"] | {"age": _d(as_of, e)} for e in unrep[:2]],
            "_missed": [round(_d(as_of, e), 1) for e in missed[:1]],
            "_wa_user": [e["data"] | {"age": _d(as_of, e)} for e in wa_fresh[:1]],
            "_pend": [PENDING_SHORT[e["data"]["intent"]] for e in pend[:1]],
            "_bl": [e["data"] | {"age": _d(as_of, e)} for e in by("buylead_bought")[:1]]}


# ------------------------------------------------------------------------------------- buyer
def buyer_facts(db: DB, glid: str, evs: list[dict], prof: dict, alt: dict, as_of: dt.datetime, cfg: Config, llm: Sarvam | None) -> dict:
    R, L = cfg.recent_days, cfg.lookback_days
    I = int(cfg.raw["window"].get("interest_days", 90))
    by = lambda et, days=L: [e for e in evs if e["etype"] == et and _d(as_of, e) <= days]  # noqa: E731

    ident = {"name": prof.get("first_name") or "", "company": prof.get("company_name") or alt.get("company_name", ""),
             "city": prof.get("city") or alt.get("seller_city", ""), "designation": prof.get("designation", ""),
             "plan": prof.get("plan") or prof.get("customer_type", ""), "vintage": prof.get("vintage", ""),
             "kyc": {k: prof.get(k) for k in ("mobile_verified", "email_verified", "gst") if prof.get(k)},
             "active_requirements": prof.get("active_requirements", [])}

    flags = []
    if prof.get("high_churn") in ("1", "Y", "Yes", "true"):
        flags.append("Flagged high-churn — keep it helpful, not salesy.")

    # ---- interests over a longer window
    interest = Counter()
    for e in evs:
        if _d(as_of, e) > I:
            continue
        d = e["data"]
        if e["etype"] in ("search", "requirement_posted", "enquiry_sent") and (d.get("kw") or d.get("product")):
            interest[(d.get("kw") or d.get("product")).strip().lower()[:50]] += 1
        elif e["etype"] == "requirement_bought_by_seller" and d.get("keyword"):
            interest[d["keyword"].strip().lower()[:50]] += 1
        elif e["etype"] == "call_extraction" and d.get("products"):
            interest[d["products"].strip().lower()[:50]] += 1
        elif e["etype"] == "wa_chat_turn" and d.get("bot", "").startswith("Enquiry for"):
            interest[d["bot"][len("Enquiry for"):].split("has been")[0].strip(" *").lower()[:50]] += 1
    interests = [k for k in _top(interest, 5) if k and k not in ("iimart", "indiamart")]

    # ---- open items
    open_items = []
    enq = [e for e in by("enquiry_sent") if e["data"].get("query_id")]
    unrep = [e for e in enq if not e["data"].get("replied")]
    if unrep:
        d = unrep[0]["data"]
        open_items.append({"kind": "unanswered_enquiries", "n": len(unrep),
                           "text": f"{len(unrep)} enquiries with no seller reply in {L}d; latest {_fmt(unrep[0])}: {d.get('product') or d.get('kw') or 'a product'}"
                                   f"{' to ' + d['seller_company'] if d.get('seller_company') else ''}"})
    replied = [e for e in enq if e["data"].get("replied")]
    if replied:
        d = replied[0]["data"]
        open_items.append({"kind": "seller_replied", "n": len(replied),
                           "text": f"{len(replied)} enquiries got a seller reply; latest {_fmt(replied[0])}: {d.get('product') or d.get('kw') or 'a product'}"
                                   f"{' from ' + d['seller_company'] if d.get('seller_company') else ''}"})
    bought = by("requirement_bought_by_seller")
    if bought:
        open_items.append({"kind": "requirement_taken", "n": len(bought),
                           "text": f"{len(bought)} sellers bought your requirement(s) in {L}d, latest {_fmt(bought[0])}: “{bought[0]['data'].get('keyword')}”"})
    for r in ident["active_requirements"][:2]:
        open_items.append({"kind": "active_requirement", "text": f"Active requirement on IndiaMART: {clip(r, 70)}"})
    tk = by("ticket_open")
    if tk:
        open_items.append({"kind": "ticket", "text": tk[0]["summary"]})
    xn = by("executive_note")
    if xn:
        open_items.append({"kind": "executive_note", "text": xn[0]["summary"]})
    lastwa = [e for e in by("wa_chat_turn")
              if e["data"].get("user") or (e["data"].get("bot") and not re.match(r"\s*(thank|thanks|aapka dhanyavaad)", e["data"]["bot"], re.I))]
    if lastwa:
        open_items.append({"kind": "wa_last", "text": f"Last WhatsApp ({_fmt(lastwa[0])}): " + clip(lastwa[0]["summary"].replace("WhatsApp ", ""), 130)})

    # ---- recent
    caps = {"search": 3, "enquiry_sent": 3, "call_made": 2, "requirement_posted": 2, "thread_msg_seller": 2, "thread_msg_buyer": 1,
            "wa_chat_turn": 3, "requirement_bought_by_seller": 1, "call_extraction": 2, "executive_note": 1, "ticket_open": 1, "browse": 1}
    used, recent = Counter(), []
    for e in evs:
        if _d(as_of, e) > R:
            continue
        _add_recent(recent, used, caps, e)

    older = [e for e in evs if R < _d(as_of, e) <= L]
    cnt = Counter(e["etype"] for e in older)
    parts = []
    for et, lab in (("search", "searches"), ("enquiry_sent", "enquiries sent"), ("call_made", "seller calls"),
                    ("requirement_posted", "requirements posted"), ("wa_chat_turn", "WhatsApp turns"), ("browse", "browsing sessions")):
        if cnt[et]:
            parts.append(f"{cnt[et]} {lab}")
    window_line = "; ".join(parts)

    lang = vote_lang(_lang_votes("buyer", evs, as_of))
    items = [e["summary"] for e in evs if _d(as_of, e) <= L and e["etype"] in ("wa_chat_turn", "call_extraction", "thread_msg_seller")][:8]
    digest = llm.digest("recent buyer interactions", items, lang) if (llm and len(items) >= 3) else None

    history = []
    if ident["vintage"]:
        history.append(f"With IndiaMART since {ident['vintage']}")
    ncalls = len([e for e in evs if e["etype"] == "call_made"])
    if ncalls:
        history.append(f"Calls made to sellers in data: {ncalls}")

    return {"identity": ident, "flags": flags, "open_items": open_items, "recent": recent, "window": window_line,
            "digest": digest, "history": history, "language": lang, "interests": interests,
            "data_through": evs[0]["ts"] if evs else None, "do_not_call": False,
            "signals": {"unanswered": len(unrep), "replied": len(replied), "bought": len(bought)},
            "_unrep": [e["data"] | {"age": _d(as_of, e)} for e in unrep[:1]],
            "_replied": [e["data"] | {"age": _d(as_of, e)} for e in replied[:1]],
            "_search": [e["data"] | {"age": _d(as_of, e)} for e in by("search", R + 7)[:1]],
            "_bought": [e["data"] | {"age": _d(as_of, e)} for e in bought[:1]],
            "_wa": [e["data"] | {"age": _d(as_of, e)} for e in lastwa[:1]],
            "_wa_user": [e["data"] | {"age": _d(as_of, e)} for e in evs if e["etype"] == "wa_chat_turn" and (e["data"].get("user") or "")
                         and _d(as_of, e) <= 3 and (len(e["data"]["user"]) >= 12 or e["data"].get("intent") == "live_chat")][:1]}


# ------------------------------------------------------------------------------------- opening lines
T = {
 "seller": {
  "call_later": {"en": "Hello {name}, last time you asked us to call back later — is this a good time?",
                 "hinglish": "Namaste {name}, pichli baar aapne baad mein call karne ko kaha tha — abhi baat kar sakte hain?",
                 "hi": "नमस्ते {name}, पिछली बार आपने बाद में कॉल करने को कहा था — क्या अभी बात कर सकते हैं?"},
  "unreplied": {"en": "Hello {name}, {buyer} enquired about {product} {ago} and it is still waiting for your reply.",
                "hinglish": "Namaste {name}, {buyer} ka {product} ke liye enquiry {ago} aaya tha aur abhi tak aapka reply pending hai.",
                "hi": "नमस्ते {name}, {buyer} की {product} की इन्क्वायरी {ago} आई थी और आपका जवाब अभी बाकी है।"},
  "missed": {"en": "Hello {name}, you missed {n} buyer call(s) recently — I can help you follow up.",
             "hinglish": "Namaste {name}, haal hi mein aapke {n} buyer call miss hue hain — main follow-up mein madad kar sakta hoon.",
             "hi": "नमस्ते {name}, हाल ही में आपके {n} बायर कॉल मिस हुए हैं — मैं फॉलो-अप में मदद कर सकता हूँ।"},
  "meeting": {"en": "Hello {name}, following up on the meeting we fixed — shall I confirm the time?",
              "hinglish": "Namaste {name}, jo meeting fix hui thi uske baare mein — kya main time confirm kar doon?",
              "hi": "नमस्ते {name}, जो मीटिंग तय हुई थी उसके बारे में — क्या मैं समय कन्फर्म कर दूँ?"},
  "buylead": {"en": "Hello {name}, you bought the lead for “{kw}” {ago} — did the buyer get back to you?",
              "hinglish": "Namaste {name}, aapne “{kw}” ki lead {ago} kharidi thi — kya buyer ka jawab aaya?",
              "hi": "नमस्ते {name}, आपने “{kw}” की लीड {ago} खरीदी थी — क्या बायर का जवाब आया?"},
  "wa_pending": {"en": "Hello {name}, your {what} on WhatsApp was left unfinished — shall we complete it now?",
                 "hinglish": "Namaste {name}, WhatsApp par aapka {what} adhoora reh gaya tha — chaliye ise abhi poora kar lein?",
                 "hi": "नमस्ते {name}, WhatsApp पर आपका {what} अधूरा रह गया था — चलिए इसे अभी पूरा कर लें?"},
  "wa_recent": {"en": "Hello {name}, you messaged us on WhatsApp {ago}: “{text}” — shall we pick that up?",
                "hinglish": "Namaste {name}, aapne {ago} WhatsApp par likha tha: “{text}” — chaliye wahin se aage badhte hain?",
                "hi": "नमस्ते {name}, आपने {ago} WhatsApp पर लिखा था: “{text}” — चलिए वहीं से आगे बढ़ते हैं?"},
  "generic": {"en": "Hello {name}, this is IndiaMART's assistant — I see you deal in {cat}. How can I help today?",
              "hinglish": "Namaste {name}, main IndiaMART ka assistant hoon — aap {cat} ka kaam karte hain. Aaj kya madad kar sakta hoon?",
              "hi": "नमस्ते {name}, मैं IndiaMART का असिस्टेंट हूँ — आप {cat} का काम करते हैं। आज मैं क्या मदद कर सकता हूँ?"}},
 "buyer": {
  "replied": {"en": "Hello {name}, {seller} replied to your {product} enquiry {ago} — did you get what you needed?",
              "hinglish": "Namaste {name}, {product} ke enquiry par {seller} ka reply {ago} aaya tha — kya aapko jo chahiye tha mil gaya?",
              "hi": "नमस्ते {name}, आपकी {product} की इन्क्वायरी पर {seller} का जवाब {ago} आया था — क्या आपको जो चाहिए था मिल गया?"},
  "unanswered": {"en": "Hello {name}, you enquired about {product} {ago} — have any sellers got back to you yet?",
                 "hinglish": "Namaste {name}, aapne {product} ke liye {ago} enquiry bheji thi — kya kisi seller ne reply kiya?",
                 "hi": "नमस्ते {name}, आपने {product} के लिए {ago} इन्क्वायरी भेजी थी — क्या किसी सेलर ने जवाब दिया?"},
  "bought": {"en": "Hello {name}, sellers have picked up your requirement for “{kw}” — have they contacted you?",
             "hinglish": "Namaste {name}, aapki “{kw}” ki requirement par sellers ne interest dikhaya hai — kya unhone contact kiya?",
             "hi": "नमस्ते {name}, आपकी “{kw}” की ज़रूरत पर सेलर्स ने रुचि दिखाई है — क्या उन्होंने संपर्क किया?"},
  "search": {"en": "Hello {name}, I saw you were looking for {kw} {ago} — shall I help you find suppliers?",
             "hinglish": "Namaste {name}, aap {ago} {kw} dhoondh rahe the — kya main suppliers dhoondhne mein madad karoon?",
             "hi": "नमस्ते {name}, आप {ago} {kw} खोज रहे थे — क्या मैं सप्लायर ढूँढने में मदद करूँ?"},
  "wa_recent": {"en": "Hello {name}, you messaged us on WhatsApp {ago}: “{text}” — shall we pick that up?",
                "hinglish": "Namaste {name}, aapne {ago} WhatsApp par likha tha: “{text}” — chaliye wahin se aage badhte hain?",
                "hi": "नमस्ते {name}, आपने {ago} WhatsApp पर लिखा था: “{text}” — चलिए वहीं से आगे बढ़ते हैं?"},
  "generic": {"en": "Hello {name}, this is IndiaMART's assistant. What are you looking to buy today?",
              "hinglish": "Namaste {name}, main IndiaMART ka assistant hoon. Aaj aap kya kharidna chahte hain?",
              "hi": "नमस्ते {name}, मैं IndiaMART का असिस्टेंट हूँ। आज आप क्या खरीदना चाहते हैं?"}}}


def _age(sig: dict) -> float:
    a = sig.get("last_bot_age")
    return 9999.0 if a is None else a  # 0.0 (a call that just ended) is a valid age, not "missing"


def _quote(text: str) -> str:
    """Make quoted user text speakable: drop link/phone placeholders and markdown, shorten WhatsApp 'need best price' templates."""
    t = re.sub(r"\[(link|phone|email)\]|[*_]", "", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    m = re.search(r"need best price for (.+?)(?:,|\.|$)", t, re.I)
    return clip(("best price for " + m.group(1)) if m else t, 70)


def make_opening(role: str, f: dict, lang: str, generic: bool = False) -> str:
    if f.get("do_not_call"):
        return ""
    t = T[role]
    ident = f["identity"]
    name = ident.get("name") or ident.get("company") or ("there" if lang == "en" else "ji")
    s = f["signals"]

    def pick(kind, **kw):
        return t[kind][lang].format(name=name, **kw)

    if generic:
        return pick("generic", cat=(ident.get("categories") or ["business"])[0] if role == "seller" else "")
    wa = f.get("_wa_user") or []
    # a bot call that happened after that WhatsApp message supersedes it (the call outcome is the newer context)
    if wa and not (s.get("last_bot_age") is not None and s["last_bot_age"] < wa[0]["age"]):
        return pick("wa_recent", text=_quote(wa[0]["user"]), ago=ago(wa[0]["age"], lang))
    if role == "seller":
        if s.get("last_bot", "") and s["last_bot"].startswith("Call Later") and _age(s) <= 14:
            return pick("call_later")
        if s.get("last_bot") == "Meeting Fixed" and _age(s) <= 14:
            return pick("meeting")
        if f["_unrep"] and f["_unrep"][0]["age"] <= 14:
            u = f["_unrep"][0]
            return pick("unreplied", buyer=u.get("buyer_company") or "ek buyer" if lang != "en" else u.get("buyer_company") or "a buyer",
                        product=u.get("product") or "your product", ago=ago(u["age"], lang))
        if f["_missed"] and f["_missed"][0] <= 7:
            return pick("missed", n=s["missed_calls"])
        if f["_bl"] and f["_bl"][0]["age"] <= 7:
            return pick("buylead", kw=f["_bl"][0]["keyword"], ago=ago(f["_bl"][0]["age"], lang))
        if f["_pend"]:
            return pick("wa_pending", what=f["_pend"][0])
        return pick("generic", cat=(ident.get("categories") or ["business"])[0])
    # buyer
    if f["_replied"] and f["_replied"][0]["age"] <= 14:
        r = f["_replied"][0]
        return pick("replied", seller=r.get("seller_company") or ("a seller" if lang == "en" else "seller"),
                    product=r.get("product") or r.get("kw") or "your product", ago=ago(r["age"], lang))
    if f["_bought"] and f["_bought"][0]["age"] <= 14:
        return pick("bought", kw=f["_bought"][0].get("keyword", ""))
    if f["_unrep"] and f["_unrep"][0]["age"] <= 14:
        u = f["_unrep"][0]
        return pick("unanswered", product=u.get("product") or u.get("kw") or "a product", ago=ago(u["age"], lang))
    if f["_search"]:
        sr = f["_search"][0]
        return pick("search", kw=sr.get("kw") or sr.get("category"), ago=ago(sr["age"], lang))
    return pick("generic")


def build_facts(db: DB, glid: str, role: str, as_of: dt.datetime, cfg: Config, llm: Sarvam | None = None) -> dict | None:
    evs = db.events(glid, role)
    prof = db.profile(glid, role) or {}
    other = db.profile(glid, "seller" if role == "buyer" else "buyer") or {}
    if not evs and not prof:
        return None
    f = seller_facts(db, glid, evs, prof or {}, as_of, cfg, llm) if role == "seller" else buyer_facts(db, glid, evs, prof, other, as_of, cfg, llm)
    if role == "seller" and not f["identity"]["name"]:
        f["identity"]["name"] = other.get("company_name", "")
    f["sources"] = sorted({e["channel"] for e in evs if _d(as_of, e) <= cfg.lookback_days} | ({"profile"} if prof else set()))
    f["lang_observed"] = any(l for l, _ in _lang_votes(role, evs, as_of))
    f["opening"] = make_opening(role, f, f["language"])
    f["opening_generic"] = make_opening(role, f, f["language"], generic=True)
    f["glid"], f["role"] = glid, role
    return f
