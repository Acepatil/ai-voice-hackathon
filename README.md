# Global Context — memory across channels (IndiaMART × Sarvam)

Make the voice bot **already know the buyer or seller, and their history, before it speaks.**

This project turns IndiaMART's scattered activity (enquiries, buyer calls, buy-leads, WhatsApp, earlier bot calls, executive calls,
the `getContext` API) into one short, structured, always-current file per GLID — `seller.md` / `buyer.md` — and serves it to
**Sarvam** voice agents (before the first word), to chat/WhatsApp bots, and to any other agent (see [`SKILL.md`](SKILL.md)).

```
 CSV exports / getContext / live events
          │  ingest.py   (redacts phones, e-mails, links at the boundary; idempotent keys)
          ▼
   SQLite event store  ──►  facts.py (windows, open items, flags, language, opening line)
          ▲                         │
          │ new event               ▼
  hooks / chat turns          render.py  →  out/seller/<glid>.md , out/buyer/<glid>.md   (≤ 320 words, YAML front matter)
          │                         │
          │                         ▼
 Sarvam on-end hook ◄── Sarvam Voice Agent ──►  /hooks/on-start  → opening_line + context as agent variables
 WhatsApp / chat bot ◄────────────────────────  /context/{glid}   → same file as markdown / json / vars
```

## Quick start (local)

```bash
cd global_context
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. API key — copy the template and fill it in. .env is git-ignored; never paste keys into chat or code.
cp .env.example .env            # then edit:  SARVAM_API_KEY=sk_...
# 2. Put the data where config.yaml expects it (or set GCX_SELLER_DIR / GCX_BUYER_CSV in .env)
#      data/seller/gc_*.csv                      (the unzipped seller dataset)
#      data/buyer_getcontext.csv                 (the buyer-side getContext log)
python -m gcx ingest            # ~30 s, ~400k events, 53k GLIDs
python -m gcx build --cohort    # renders 8,278 files in ~15 s  → out/seller, out/buyer
python -m gcx show 100145113    # print one file
python -m gcx serve --port 8088 # hooks + context API
python -m pytest -q tests       # 22 tests, no network, no real data needed
```

Without `SARVAM_API_KEY` everything except the model replies works (files are built with deterministic text). With it, Sarvam adds
(a) one-line digests of long histories when you build with `--llm`, (b) chat/WhatsApp replies, (c) STT/TTS for the local voice loop.

| Command | What it does |
|---|---|
| `python -m gcx ingest` | CSV exports → event store (idempotent, safe to re-run) |
| `python -m gcx build --cohort / --all / --dirty / --glid X [--llm]` | render files (cohort = the 5,000 sellers + buyers) |
| `python -m gcx serve` | FastAPI server (below) |
| `python -m gcx chat <glid> [--channel whatsapp]` | talk to the bot with that user's file loaded; turns are written back |
| `python -m gcx voice <glid> in.wav --out reply.wav` | one voice turn: Saaras STT → bot → Bulbul TTS |
| `python -m gcx export-campaign --n 20` | writes a Sarvam campaign CSV (glid + opening line + open items …) — personalise the voice agent **without any server** |
| `python -m gcx eval all` | window study, compactness, freshness, re-ask, lift, coverage → `reports/*.json` |
| `python examples/resume_across_channels.py` | WhatsApp → voice → call outcome → WhatsApp, through the real hooks → `examples/resume_session.md` |

## HTTP API

| Endpoint | Purpose |
|---|---|
| `POST/GET /hooks/on-start` | **Sarvam on-start hook.** Body can carry `glid` anywhere (`{"glid":..}`, `{"contact":{"glid":..}}`, query `?glid=`). Returns flat variables: `opening_line, language, language_code, open_items, do_not_call, ai_disclosure_required, context_md, file_version, ab_arm`. Unknown GLID → still 200 with a generic opening (a call never fails because memory is missing). |
| `POST /hooks/on-end` | **Sarvam on-end hook.** `glid, call_id, disposition, summary, transcript[], duration` → event → file re-rendered before anyone reads it again. Replays (same `call_id`) are no-ops. |
| `POST /channels/{whatsapp\|chat}/incoming` | `{glid, text}` → reply grounded in the file; the turn is written back. 503 + the turn still recorded if no model is available. |
| `POST /events` | generic feed for any other system (app, CRM, executive notes) |
| `GET /context/{glid}?role=auto\|seller\|buyer&fmt=md\|json\|vars` | the file itself |
| `GET /metrics`, `GET /health`, `POST /refresh` | observability, batch drain |

Set `GCX_HOOK_TOKEN` in `.env` to require `Authorization: Bearer <token>` (or `X-Hook-Token`) on everything except `/health` and `/metrics`.

## The file

```markdown
---
glid: 100145113   role: seller   as_of: 2026-10-02T09:57:30+05:30   data_through: 2026-09-28 13:01:23
language: hinglish   language_basis: observed|default   do_not_call: false   version: 4   sources: [call, enquiry, profile, voice_bot, whatsapp]
---
# Papi Enterprises — Badlapur, Maharashtra
Proprietorship · … · Categories: … · KYC: mobile ✔ email ✔ GST ✔ PAN ✔
**Open with:** “Namaste Papi Enterprises, Sahyadri Enterprises ka Amba Poli ke liye enquiry 3 din pehle aaya tha aur abhi tak aapka reply pending hai.”
## Flags        (do-not-call, "are you a bot?", frustration, already with an executive, past objections)
## Open items   (unreplied enquiries, missed buyer calls, "call later", meeting fixed, unfinished WhatsApp step, …)
## Last 7 days  (one line per event, duplicates folded "×2")
## Days 8–30    (counts + top products [+ one-line Sarvam gist with --llm])
## History      (last bot call, bot-call tallies, 90-day engagement)
```

Fourteen real examples covering every situation (do-not-call, call-later, Hindi, English, sparse, truncated getContext, …) are in
[`samples/`](samples/INDEX.md); all 8,278 generated files are in `out/`.

## What was measured (on this dataset, `reports/*.json`)

| Criterion | Result | How it was measured |
|---|---|---|
| **Freshness** — delay between new activity and updated file | event → file rewritten: **p50 33 ms, p95 47 ms** (300 probes through the real write path); read-your-write 50/50; batch drain ≈ 300 files/s | `eval freshness` on a copy of the store |
| **Compactness & structure** | seller **p50 170 words (~330 tokens), p95 287**; buyer p50 58, p95 144; 100 % within the 320-word budget; **100 % of 8,278 files parse** (front matter + required sections) | `eval compactness` |
| **Resumed without re-asking** | Honest baseline: only **1 of 455** transcribed historical bot calls re-asked a fact the file holds (what they sell / where they are). The real "restart" problem is cold start: **76 %** of those calls open with the generic *"kya aap X se bol rahe hain?"* — the file turns that into a personalised opening. | `eval reask` (regex proxy on the 455 calls that have transcripts) |
| **Lift from the personalised opening** | **Not measurable offline — needs live calls.** The A/B harness is built (`ab.holdout_pct`, hash-assigned arms, outcomes logged by the on-end hook, readout in `eval lift`). Context for your hypothesis: in history, calls to sellers with a fresh unreplied enquiry/missed call converted *worse* (meeting-fixed 7.4 % vs 12.0 %, n=1,749 vs 7,908) under the old *generic* opener — a gap the personalised line is designed to close, not evidence that it will. | `eval lift` |
| **Reusability beyond the bot prompt** | Plain markdown + YAML front matter; same file via HTTP (`md/json/vars`), `SKILL.md` for any agent, event-write API for any producer; no dependency on Sarvam to read it | — |

## Sources, window, refresh

See [`docs/sources_and_refresh.md`](docs/sources_and_refresh.md) — which source feeds which section, why 7 / 30 / 60 days, and how the file stays current.
Wiring Sarvam's console (variables, hooks, campaigns, the ready-to-paste agent prompt): [`docs/sarvam_setup.md`](docs/sarvam_setup.md).

## What I could not verify — read before relying on it

- **Not exercised against the live Sarvam API** (no key in the build environment). Method names/params were checked against the installed `sarvamai` 0.1.36 SDK
  signatures (`chat.completions`, `speech_to_text.transcribe`, `text_to_speech.convert`) — but chat, STT, TTS and `--llm` digests are untested live. Failures degrade gracefully (digest → deterministic text; chat → 503 with the turn still recorded).
- **Sarvam's hook request/response schema is not public in the docs I could read.** The server accepts the GLID from many places and returns a flat JSON map; check the console's hook screen and adjust the field mapping (one place: `gcx/server.py::as_vars`, `find_glid`).
- **No phone → GLID map in the data** (phones are excluded from the export). In production the campaign contact row / telephony metadata must carry the GLID, or you need IndiaMART's resolver in front of `find_glid`.
- **The getContext export is truncated** (~32 k chars) for the most active users: 26 such rows are salvaged by regex (GLID, KYC basics, every complete activity item); 47 rows are unusable. A live getContext client would not have this problem — plug it in at `ingest.parse_getcontext`.
- **Buyer side is thin by nature:** the 271 getContext GLIDs do not overlap the seller dataset. Buyer files for the other ~51k buyers (from enquiries/calls sent to the sampled sellers) have a median of 1 event (81 % have < 3); they are valid but sparse, and say so.
- **Language** is voted from what the person wrote/said (Devanagari → `hi`, romanised Hindi → `hinglish`, else `en`); with no evidence it defaults to `hinglish` and says `language_basis: default`.
- **Message bodies in the export contain real names and phone numbers** despite the data dictionary; phones/e-mails/links are redacted at ingest (tested), names are kept (internal use only).

## Layout

```
gcx/        ingest.py  facts.py  render.py  service.py  server.py  chat.py  llm.py  evals.py  cli.py  db.py  util.py  config.py
tests/      22 tests (redaction, parsing, salvage, budget, versions, DNC, hooks, auth, regression for the 0-age bug)
samples/    14 curated files + INDEX.md        out/   all generated files        reports/   eval JSON
examples/   resume_across_channels.py → resume_session.md
docs/       sources_and_refresh.md  sarvam_setup.md  agent_prompt.md  SUBMISSION.md  DEPLOY.md
deploy/     demo.sqlite (built by scripts/make_demo_db.py), Dockerfile + render.yaml at the root
SKILL.md    how any agent should read/write these files        skills.md   build journey + tools (submission)
```
