# skills.md — build journey and tools used

**Team:** <TEAM NAME> · **Members:** <NAME 1>, <NAME 2>, … · **Problem chosen:** 2 of 6 — *Global Context: Memory Across Channels*
**Sarvam agent ID / live link:** <PASTE FROM CONSOLE>

## The challenge in one line
Make the voice bot already know the buyer or seller, and their past history, before it speaks.

## Build journey
1. **Read the data before designing.** Seller dataset = 11 tables keyed by GLID (enquiries, buyer calls, buy-leads, WhatsApp, earlier VANI bot calls, executive calls, AI call extractions).
   The buyer file turned out to be a *log of the getContext API*, not a table — each row holds a JSON response with activity, KYC, tickets and last WhatsApp chats.
   Findings that shaped the design: the 271 buyer GLIDs never overlap the seller dataset; the buyer export is truncated at ~32k characters for the busiest users (recovered 26 rows with regexes);
   message text contains phone numbers although the dictionary says they are removed (redacted at ingest).
2. **One event model for every channel.** Everything becomes `(glid, role, ts, channel, type, short summary, data)` in SQLite. A GLID can be seller *and* buyer; the WhatsApp bot's requirement flow (`Msite_BL*`) marks a seller as acting as a buyer.
3. **Facts, then text.** Deterministic code computes open items (unreplied enquiries, missed buyer calls, call-later, meeting fixed, unfinished WhatsApp steps), flags (do-not-call, asked-if-bot, frustration) and the language (voted from what the person wrote or said).
   The LLM is optional and only compresses long histories — never on the critical path.
4. **Short, structured files** (`seller.md` / `buyer.md`): YAML front matter, an "Open with" line, open items, last 7 days, 30-day summary, history; hard budget of 320 words, lowest-priority lines dropped first.
5. **Lookback window chosen from data:** coverage of actionable items rises ~4 points per extra week with no knee, and history shows no outcome gradient by age → 7 days line-by-line, 30 days summarised, 60 days for open items (one line).
6. **Freshness by construction:** every producer writes an event, which re-renders that GLID's file immediately (≈33 ms); readers re-render dirty files before returning them.
7. **Sarvam wiring:** voice agent built on the Sarvam platform; greeting = `@opening_line`; the record is injected as variables via campaign columns or the on-start hook; the on-end hook writes the call outcome back, so a WhatsApp/chat turn minutes later already knows it.
8. **Tested, then broke it on purpose:** 22 tests; a smoke test against a real server caught a speakable placeholder for do-not-call users; an end-to-end scenario caught a falsy-zero bug that ignored a call that had just ended.

## Tools used
| Tool | Used for |
|---|---|
| **Sarvam Voice Agents** (indus.sarvam.ai) | the phone/web agent: prompt, variables, hooks, campaigns, test agent, analytics |
| **Sarvam Saaras v4** | speech-to-text, Hindi/Hinglish phone audio (inside the agent) |
| **Sarvam Bulbul v3/v4** | text-to-speech voice (inside the agent) |
| **Sarvam-105B** (`sarvam-105b`, `-conversations` for voice) | chat/WhatsApp replies and optional history digests via the Sarvam API (`sarvamai` SDK) |
| Python 3.12, pandas | ingest of ~400k events from the CSV exports |
| SQLite | event store, file registry, LLM cache, A/B log |
| FastAPI + uvicorn | `/hooks/on-start`, `/hooks/on-end`, `/context/{glid}`, chat webhook, metrics |
| pytest | 22 tests (redaction, parsing, truncated-JSON salvage, budget, versions, do-not-call, hooks, auth, regressions) |
| Claude Code | AI pair-programmer used to write and test the code *(edit this line to match what your team actually did)* |

## What the numbers say (reports/*.json)
Freshness p50 33 ms · seller file median 170 words (~330 tokens), 100% of 8,278 files valid · 76% of historical bot calls started cold with a generic identity check ·
lift of the personalised opening needs live A/B (harness built; `ab.holdout_pct`).

## Honest limits
Sarvam's hook request/response format is not in the public docs — verify in the console. No phone→GLID map in the data. Buyer files are sparse for ~81% of buyers. Lift is unmeasured until live calls.

## Reproduce
`pip install -r requirements.txt` → copy `.env.example` to `.env` (add `SARVAM_API_KEY`) → data in `data/` → `python -m gcx ingest && python -m gcx build --cohort` → `python -m gcx serve`. See README.md.
