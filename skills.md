# skills.md — build journey and tools used

**Project:** VANI SAATH - Seller And buyer Across Touchpoints, Held together

**Org-Id** 01a11058-8b0c-77c7-9674-390686a508c7

**Team:** Dragon Slayers · 

**Members:** Sourabh Patil, Srishti, Simran Varshney· 

**Problem chosen:** 2 of 6 — *Global Context: Memory Across Channels*

**Org-Id** 01a11058-8b0c-77c7-9674-390686a508c7

**Sarvam agent ID / live link:** IM-Voice-As-4fb34110-7d3d

## The challenge in one line
Make the voice bot already know the buyer or seller, and their past history, before it speaks.

## Build journey
1. **Read the data before designing.** The seller dataset is 11 tables keyed by GLID (enquiries, buyer calls, buy-leads, WhatsApp, earlier VANI bot calls, executive calls, AI call extractions).
   The buyer file turned out to be a *log of the getContext API*, not a table: each row holds a JSON response with activity, KYC, tickets and last WhatsApp chats.
   Findings that shaped the design: the 271 buyer GLIDs never overlap the seller dataset; the buyer export is truncated at ~32k characters for the busiest users (26 rows recovered with regexes, 47 unusable);
   message text contains phone numbers although the dictionary says they are removed (redacted at ingest, with a test).

2. **One event model for every channel.** Everything becomes `(glid, role, ts, channel, type, short summary, data)` in SQLite. A GLID can be both seller and buyer; the WhatsApp bot's requirement flow (`Msite_BL*`) marks a seller as acting as a buyer.
3. **Facts, then text.** Deterministic code computes open items (unreplied enquiries, missed buyer calls, call-later, meeting fixed, unfinished WhatsApp steps), flags (do-not-call, asked-if-bot, frustration) and the language (voted from what the person wrote or said).
   The LLM is optional and only compresses long histories — never on the critical path.
4. **Short, structured files** (`seller.md` / `buyer.md`): YAML front matter, an "Open with" line, open items, last 7 days, 30-day summary, history; hard budget of 320 words, lowest-priority lines dropped first.
5. **Lookback window chosen with data and judgement:** coverage of actionable items rises ~4 points per extra week with no knee, and history shows no outcome gradient by age → 7 days line by line, 30 days summarised, 60 days for open items (one line).
6. **Freshness by construction:** every producer writes an event, which re-renders that GLID's file immediately (p50 ≈ 33 ms); readers re-render dirty files before returning them.
7. **Sarvam voice agent.** Built on the Sarvam Voice Agents platform: the greeting is the `@opening_line` variable and the record is injected as agent variables (`glid, company, opening_line, language, open_items, flags, context_brief, do_not_call, ai_disclosure_required`),
   filled from campaign CSV columns (`python -m gcx export-campaign`) or the variable defaults. This path needs no server, so the agent works whenever judges test it.
8. **Prompt tuning from real test calls.** Our first version drifted into profile-confirmation calls ("confirm your category / city"), because the prompt told the agent to confirm known facts. We added a purpose statement (follow up on enquiries, missed calls and call-later requests; never verify profile data)
   and rewrote the rule so the agent spends the call on open items.
9. **Live hooks (optional layer).** We deployed the FastAPI server (`/hooks/on-start`, `/hooks/on-end`, `/context/{glid}`) on Render with a small demo database (about 20 GLIDs, not the full dataset) and tried Sarvam API tools to fetch context at call start and save the outcome at call end.
   What we learned: Sarvam's tool form only accepts bearer tokens as stored secrets, so we pass our token in an `X-Hook-Token` header instead; a tool the agent does not call, or a cold-starting free server, makes the first seconds of the call silent — which is why the base agent does not depend on any API call.
10. **Tested, then broke it on purpose:** 22 tests; a smoke test against a real server caught a speakable placeholder for do-not-call users; an end-to-end scenario caught a falsy-zero bug that ignored a call that had just ended.

## Tools used
| Tool | Used for |
|---|---|
| **Sarvam Voice Agents** (indus.sarvam.ai) | the agent: prompt, variables, greeting, voice, API tools, test agent, analytics |
| **Sarvam Saaras v4** | speech-to-text for Hindi/Hinglish phone audio (inside the agent) |
| **Sarvam Bulbul v3/v4** | text-to-speech voice (inside the agent) |
| **Sarvam-105B** (`sarvam-105b`) via the `sarvamai` SDK | optional chat/WhatsApp replies and history digests (not exercised against the live API in our build environment) |
| Python 3.12, pandas | ingest of ~400k events from the CSV exports |
| SQLite | event store, file registry, LLM cache, A/B log |
| FastAPI + uvicorn | `/hooks/on-start`, `/hooks/on-end`, `/context/{glid}`, chat webhook, metrics |
| Docker + Render | optional hosting of the hooks server with a small demo database |
| pytest | 22 tests (redaction, parsing, truncated-JSON salvage, budget, versions, do-not-call, hooks, auth, regressions) |
| Claude (AI pair-programmer) | wrote and tested the code and helped debug the Sarvam console setup *(edit this line to match what your team actually did)* |

## What the numbers say (reports/*.json)
Freshness p50 33 ms, p95 47 ms · seller file median 170 words (~330 tokens), p95 287; 100% of 8,278 generated files valid · 76% of 455 transcribed historical bot calls started cold with a generic identity check; only 1 re-asked a fact the file holds ·
the lift of the personalised opening needs live A/B (harness built; `ab.holdout_pct`) and is **not** claimed.

## Honest limits
Sarvam's hook request/response format is not in the public docs, so the live hooks were not verified end to end. The base agent runs from variables, not from the hooks. No phone→GLID map in the data.
Buyer files are sparse for ~81% of buyers. Lift is unmeasured until live calls. The hosted demo database holds only ~20 demo GLIDs.


