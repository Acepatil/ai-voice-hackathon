# Sources used, lookback window, refresh logic

## Sources → what they feed

| Source (file) | Becomes (role: event) | Used for |
|---|---|---|
| `gc_seller_profile.csv` | seller profile | identity, categories, KYC, plan, engagement counters, bot-call history, **flags** (`do_not_call_requested`, `asked_if_talking_to_bot`, `showed_frustration`, `already_in_touch_with_executive`, `past_objections`) |
| `gc_enquiries_received.csv` | seller: `enquiry_received` · buyer: `enquiry_sent` · buyer profile (company, city, designation) | unreplied-enquiry open items, "who enquired about what", buyer's enquiries/interests |
| `gc_enquiry_messages.csv` | both roles: thread messages | `replied` flag (a seller message exists on that `query_id`), last words exchanged, seller-language signal |
| `gc_buyer_calls_received.csv` | seller: `buyer_call` (connected / missed) · buyer: `call_made` | missed-call open item |
| `gc_buyleads_bought.csv` | seller: `buylead_bought` · buyer: `requirement_bought_by_seller` | "did the buyer get back about the lead you bought?"; buyer "sellers picked up your requirement" (16 Aug–1 Oct only) |
| `gc_whatsapp_messages.csv` | seller: `wa_message` (delivery/read, no text) | campaign read-rate in the 30-day summary |
| `gc_whatsapp_chatbot_conversations.csv` | `wa_chat_turn`; **role by intent**: `Msite_BL*`, `Buy_Product`, `liveCustomer_Buyer*`, `Enquiry_Media_Capture` → buyer, rest → seller; "Enquiry for X has been sent" → buyer `enquiry_sent` | unfinished-step open items (photo not added, image too small, callback request), language signal, buyer requirements made over WhatsApp |
| `gc_buyer_call_extractions.csv` | role = `seller_role_on_call`: `call_extraction` | what they discussed/bought on phone (products, prices, stock), language |
| `gc_bot_calls.csv` + `gc_bot_call_turns.csv` | seller: `voice_bot_call` (+ up to 14 turns) | last outcome (call-later / meeting fixed / not interested), language from the seller's own words |
| `gc_executive_calls.csv` | seller: `executive_call` | "executive tried, not answered" |
| getContext log (buyer CSV) | buyer profile + events (`search`, `enquiry_sent`, `call_made`, `requirement_posted`, WhatsApp, executive notes, tickets) | buyer interests, active requirements, last WhatsApp. 26 truncated rows are salvaged by regex. |

All free text is redacted (phones, e-mails, links) before it is stored; summaries are ≤ 260 chars. Not used: recording URLs, evaluator output, KYC identifiers.

## Lookback window — what and why

| Window | Treatment | Why |
|---|---|---|
| **0–7 days** | one line per event, capped per type, duplicates folded ("×2") | "yesterday / 3 din pehle" phrasing is only natural for fresh events; median gap between a bot call and the seller's previous other-channel activity is 19 days, so the recent window is where a personalised line has the most to say |
| **8–30 days** | counts + top products (+ optional Sarvam one-line gist) | enough to say "5 enquiries, 3 still unreplied, mostly X" without listing |
| **31–60 days** | **only** "N older enquiries still unreplied" | one line of cost; actionable-item coverage keeps rising ~4 points per extra week out to 90 days (8.4 % @3d → 12.7 @7 → 18.1 @14 → 26.0 @30 → 35.6 @60 → 42.1 @90 of past bot calls had one) |
| **> 60 days** | dropped, except durable facts (flags, KYC, last bot outcome, language, history tallies) | |

The curve has **no knee** and history shows **no outcome gradient by age** (the old bot never used these facts), so the choice is a judgement,
not a fitted optimum: it's all in `config.yaml` (`window.*`), and `python -m gcx eval window` prints the table to re-decide with new data. The right way to tune it
is the A/B readout once the bot is live.

## Refresh logic

1. **Event-driven (default path).** Every producer (Sarvam on-end hook, chat/WhatsApp turn, `/events`) calls `ContextService.ingest_event()`:
   insert event (idempotent key) → mark `(glid, role)` dirty → re-render synchronously → atomic file replace (`tmp` → `rename`) → log latency. Version bumps only if content
   changed (hash excludes `as_of`/`version`). Measured p50 33 ms, p95 47 ms.
2. **Read-through.** `get()` re-renders a missing or dirty file before returning it, so a reader can never see a file older than the event store.
3. **Batch drain.** After bulk loads or a nightly job: `python -m gcx ingest && python -m gcx build --dirty` (≈ 300 files/s). A nightly `build --all`
   also lets time-based text ("3 din pehle") and the 7/30/60-day windows roll forward for users with no new events — set `as_of: now` in production.
4. **Concurrency.** SQLite WAL + a process lock; one server process is enough for tens of thousands of GLIDs. For multiple workers, move the store to Postgres (the `DB` class is the only SQL).
5. **Safety.** Unknown GLID or any failure inside the hook still returns a generic opening; the LLM is never on the critical path of a file build.
