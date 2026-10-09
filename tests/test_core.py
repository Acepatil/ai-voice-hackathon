import datetime as dt
import json

import pytest

from gcx import render as R
from gcx.ingest import parse_getcontext, salvage_getcontext
from gcx.util import clip, detect_lang, parse_ts, redact, vote_lang

T0 = dt.datetime(2026, 10, 2, 12, 0, 0)


def seed_seller(s, glid="1", **prof):
    base = {"company_name": "Acme Foods", "seller_city": "Pune", "seller_state": "Maharashtra", "top_category_1": "Namkeen",
            "is_paid": "1", "mobile_verified_flag": "1"} | prof
    s.db.set_profile(glid, "seller", base)


def ev(s, glid, role, days_ago, channel, etype, summary, data=None, key=None):
    ts = (T0 - dt.timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    s.db.add_events([(glid, role, ts, channel, etype, summary, data or {}, key or f"{glid}{role}{days_ago}{etype}{summary}")])


# ---------------------------------------------------------------- helpers
def test_redact_strips_phone_email_url():
    t = redact("call me 98155 22953 or +91-9815522953, mail a.b@x.com see https://x.co/a?b=1")
    assert "9815" not in t and "@" not in t and "http" not in t
    assert t.count("[phone]") == 2


def test_redact_handles_json_blobs_and_escaped_urls():
    assert redact('{"queryImage":{"img_src":"http:\\/\\/5.imimg.com\\/a.jpg"}}') == "[structured message]"
    assert "imimg" not in redact("see http:\\/\\/5.imimg.com\\/a.jpg now")


def test_parse_ts_formats():
    assert parse_ts("2026-08-13 06:17:42.465") == dt.datetime(2026, 8, 13, 6, 17, 42, 465000)
    assert parse_ts("20260929163757") == dt.datetime(2026, 9, 29, 16, 37, 57)
    assert parse_ts("16-Sep-2026") == dt.datetime(2026, 9, 16)
    assert parse_ts("2026-10-01T10:26:32.5414822+05:30") == dt.datetime(2026, 10, 1, 10, 26, 32, 541482)
    assert parse_ts("") is None and parse_ts("nan") is None


def test_language_detection_and_vote():
    assert detect_lang("mujhe rate bhejo kitna hai") == "hinglish"
    assert detect_lang("मुझे रेट भेजिए") == "hi"
    assert detect_lang("please send the price list") == "en"
    assert detect_lang("ok") is None
    assert vote_lang([("en", 1), ("hinglish", 2.5)]) == "hinglish"
    assert vote_lang([(None, 1)]) == "hinglish"  # documented default


# ---------------------------------------------------------------- getContext
def test_getcontext_salvages_truncated_export():
    raw = ('{"API_RESPONSE_JSON":"{\\"code\\":200,\\"data\\":{\\"ActualData\\":{\\"glid\\":\\"270227757\\",\\"upload_metadata\\":'
           '{\\"upload_time\\":\\"2026-10-01T10:26:32+05:30\\"}},\\"activitydetails\\":{\\"BUYER_ACTIVITY\\":[{\\"ACTIVITY_ID\\":\\"677\\",'
           '\\"ACTIVITY_TIME\\":\\"20260925101542\\",\\"ACTIVITY_TYPE\\":\\"Search\\",\\"CATEGORY_ID\\":\\"1\\",\\"CATEGORY_NAME\\":\\"Chana Dal\\",'
           '\\"KEYWORD\\":\\"chana%20dal\\",\\"PRODUCT_ID\\":\\"0\\",\\"SELLER_GLUSR_ID\\":\\"0\\"},{\\"ACTIVITY_ID\\":\\"20\\",\\"ACTIVITY_TIME\\":\\"2026')
    glid, prof, evs = salvage_getcontext(raw)
    assert glid == "270227757" and len(evs) == 1
    assert evs[0][4] == "search" and "chana dal" in evs[0][5]  # %20 decoded, truncated tail ignored


def test_getcontext_full_parse_builds_events():
    data = {"ActualData": {"glid": "5"}, "kycdetails": {"first_name": "Asha", "city": "Pune", "customer_type": "Free"},
            "activitydetails": {"BUYER_ACTIVITY": [{"ACTIVITY_ID": "20", "ACTIVITY_TIME": "20260930101010", "ACTIVITY_TYPE": "ENQ",
                                                    "CATEGORY_ID": "1", "CATEGORY_NAME": "-", "KEYWORD": "Steel Pipe", "PRODUCT_ID": "9", "SELLER_GLUSR_ID": "77"}],
                                "LAST_WA9696_CHAT": [{"CHAT_CONTENT": json.dumps({"message_content": {"text": "Call 9815522953 now"}}),
                                                      "ENTRY_DATE": "2026-10-01 08:55:33", "MESSAGE_SENDER": "API"}]}}
    prof, evs = parse_getcontext("5", data, "2026-10-01 10:00:00")
    assert prof["first_name"] == "Asha"
    kinds = {e[4] for e in evs}
    assert {"enquiry_sent", "wa_chat_turn"} <= kinds
    assert all("9815" not in e[5] for e in evs)


# ---------------------------------------------------------------- store / render
def test_ingest_is_idempotent(svc):
    seed_seller(svc)
    ev(svc, "1", "seller", 1, "enquiry", "enquiry_received", "X enquired", {"replied": False}, key="k1")
    n = svc.db.add_events([("1", "seller", "2026-10-01 10:00:00", "enquiry", "enquiry_received", "X enquired", {}, "k1")])
    assert n == 0


def test_render_has_structure_and_budget(svc):
    seed_seller(svc)
    for i in range(60):  # a very busy seller must still fit the budget
        ev(svc, "1", "seller", i % 6, "enquiry", "enquiry_received", f"Buyer {i} enquired for Namkeen {i}",
           {"replied": False, "product": f"Namkeen {i}", "buyer_company": f"B{i}", "buyer_city": "Pune"})
    r = svc.render("1", "seller")
    assert R.validate(r["markdown"], "seller") == []
    assert r["words"] <= svc.cfg.max_words + 40
    assert "Open items" in r["markdown"] and "60 enquiries" in r["markdown"]
    assert "(×" in r["markdown"] or r["markdown"].count("enquired for") <= 4  # recent list is capped / folded


def test_version_only_bumps_on_real_change(svc):
    seed_seller(svc)
    ev(svc, "1", "seller", 1, "enquiry", "enquiry_received", "X enquired", {"replied": False, "product": "Namkeen"})
    v1 = svc.render("1", "seller")["version"]
    assert svc.render("1", "seller")["version"] == v1          # same facts -> same version
    ev(svc, "1", "seller", 0, "call", "buyer_call", "missed", {"connected": False})
    assert svc.render("1", "seller")["version"] == v1 + 1


def test_do_not_call_is_enforced(svc):
    seed_seller(svc, do_not_call_requested="1")
    ev(svc, "1", "seller", 1, "enquiry", "enquiry_received", "X", {"replied": False})
    r = svc.render("1", "seller")
    assert "DO NOT CALL" in r["markdown"] and r["opening"] == "" and "do_not_call: true" in r["markdown"]


def test_old_events_outside_lookback_are_not_listed(svc):
    seed_seller(svc)
    ev(svc, "1", "seller", 200, "enquiry", "enquiry_received", "Ancient enquiry for Gold", {"replied": False, "product": "Gold"})
    md = svc.render("1", "seller")["markdown"]
    assert "Ancient" not in md and "Gold" not in md


def test_buyer_file_from_events_only(svc):
    ev(svc, "9", "buyer", 1, "enquiry", "enquiry_sent", "Enquired to Sun Traders for Steel Pipe",
       {"replied": True, "product": "Steel Pipe", "seller_company": "Sun Traders", "query_id": "q"})
    r = svc.render("9", "buyer")
    assert "Sun Traders" in r["markdown"] and R.validate(r["markdown"], "buyer") == []


def test_unknown_glid_returns_none(svc):
    assert svc.get("424242") is None


# ---------------------------------------------------------------- live path
def test_live_event_updates_file_and_opening(svc):
    seed_seller(svc)
    ev(svc, "1", "seller", 1, "enquiry", "enquiry_received", "X enquired", {"replied": False, "product": "Namkeen", "buyer_company": "X"})
    before = svc.get("1")["markdown"]
    svc.tick()
    r = svc.ingest_event("1", "seller", "whatsapp", "wa_chat_turn", "WhatsApp user: price bhejo namkeen 50 kg", {"user": "price bhejo namkeen 50 kg", "intent": "live_chat"})
    assert r["version"] > 1 and r["latency_ms"] < 2000
    assert "namkeen 50 kg" in r["opening"] and r["markdown"] != before
    assert svc.ingest_event("1", "seller", "whatsapp", "wa_chat_turn", "WhatsApp user: price bhejo namkeen 50 kg",
                            key="same", ts=svc.clock()) is not None
    assert svc.ingest_event("1", "seller", "whatsapp", "wa_chat_turn", "again", key="same") is None  # duplicate key ignored


# ---------------------------------------------------------------- http hooks
@pytest.fixture()
def client(svc, monkeypatch):
    from fastapi.testclient import TestClient
    from gcx import server
    monkeypatch.setattr(server, "svc", svc)
    return TestClient(server.app)


def test_on_start_known_unknown_and_no_llm_needed(client, svc):
    seed_seller(svc)
    ev(svc, "1", "seller", 1, "enquiry", "enquiry_received", "X", {"replied": False, "product": "Namkeen", "buyer_company": "X"})
    j = client.post("/hooks/on-start", json={"contact": {"glid": "1"}}).json()
    assert j["glid"] == "1" and j["opening_line"] and j["file_version"] >= 1 and j["do_not_call"] is False
    u = client.post("/hooks/on-start", json={"glid": "does-not-exist"}).json()
    assert u["ab_arm"] == "generic_unknown" and u["opening_line"]       # never fail a call because memory is missing


def test_on_end_then_next_read_sees_outcome(client, svc):
    seed_seller(svc)
    svc.tick()
    j = client.post("/hooks/on-end", json={"glid": "1", "call_id": "c1", "disposition": "Call Later / Busy", "summary": "busy, call at 6pm",
                                          "transcript": [{"speaker": "bot", "text": "hello"}, {"speaker": "user", "text": "baad mein"}]}).json()
    assert j["ok"] and j["refreshed"]
    assert "called later" in client.get("/context/1").text
    assert client.post("/hooks/on-end", json={"glid": "1", "call_id": "c1", "disposition": "x"}).json()["refreshed"] is False  # replay is a no-op


def test_token_auth(client, svc, monkeypatch):
    monkeypatch.setenv("GCX_HOOK_TOKEN", "s3cret")
    assert client.post("/hooks/on-start", json={"glid": "1"}).status_code == 401
    assert client.post("/hooks/on-start", json={"glid": "1"}, headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_chat_without_key_still_records_the_turn(client, svc):
    seed_seller(svc)
    r = client.post("/channels/whatsapp/incoming", json={"glid": "1", "text": "mujhe 100 kg ka quote chahiye jaldi"})
    assert r.status_code == 503  # no model available, honest error
    assert "100 kg" in client.get("/context/1").text  # ...but the memory write still happened


def test_call_that_just_ended_drives_next_opening(svc):
    """Regression: age 0.0 was treated as 'missing' and the call-later outcome was ignored."""
    seed_seller(svc)
    svc.tick()
    r = svc.ingest_event("1", "seller", "voice_bot", "voice_bot_call", "bot call", {"disposition": "Call Later / Busy", "summary": "busy"})
    assert "baad mein call" in r["opening"]


def test_opening_quote_is_speakable():
    from gcx.facts import _quote
    assert _quote("[link] Hello *A One Collection*, Need best price for Hot And Cold Water Bottle, 750 ml") == "best price for Hot And Cold Water Bottle"


def test_do_not_call_hook_never_returns_speakable_placeholder(client, svc):
    seed_seller(svc, do_not_call_requested="1")
    j = client.post("/hooks/on-start", json={"glid": "1"}).json()
    assert j["do_not_call"] is True and j["opening_line"] == ""


def test_meeting_fixed_outranks_unreplied_enquiry(svc):
    seed_seller(svc)
    ev(svc, "1", "seller", 2, "enquiry", "enquiry_received", "X", {"replied": False, "product": "Namkeen", "buyer_company": "X"})
    ev(svc, "1", "seller", 3, "voice_bot", "voice_bot_call", "call", {"disposition": "Meeting Fixed", "summary": "ok", "attempt_no": 1})
    assert "meeting" in svc.render("1", "seller")["opening"].lower()
