---
name: indiamart-global-context
description: Load a buyer's or seller's cross-channel history (buyer.md / seller.md) before speaking to an IndiaMART user on voice, WhatsApp or chat, personalise the opening, never re-ask known facts, and write the outcome back so the next channel knows. Use when an agent is about to contact or reply to an IndiaMART GLID.
---

# IndiaMART Global Context

One short markdown file per GLID (`seller.md` / `buyer.md`) holds what IndiaMART already knows: profile and KYC, open items
(unreplied enquiries, missed buyer calls, "call me later", unfinished WhatsApp steps), the last 7 days line by line, a 30-day
summary, past bot/executive call outcomes, flags, preferred language, and a ready opening line.

## Before you speak

1. **Fetch the file** (any one of):
   - HTTP: `GET {BASE}/context/{glid}?role=auto` → markdown (`fmt=json` for structured facts, `fmt=vars` for flat agent variables)
   - Voice agent hook: `POST {BASE}/hooks/on-start {"glid": "..."}` → `opening_line`, `language`, `open_items`, `do_not_call`, `context_md`
   - Files: `out/seller/{glid}.md`, `out/buyer/{glid}.md`
   `role=auto` picks the role with the most recent activity; a GLID can be both (a seller who also buys) — the `X-Other-Roles` response header (or `other_roles` in the service) lists the rest; request one explicitly with `role=buyer|seller`.
2. **Read the front matter first.** If `do_not_call: true`, do **not** initiate a call; reply only if the person writes first. If `as_of` is old
   (more than a day behind `now` in a live system) treat "Open items" as possibly stale and confirm instead of asserting.
3. **Open with the `Open with:` line** (already in the user's language: `language: en | hinglish | hi`). Do not open with a generic
   "am I speaking to <company>?" check — the file already tells you who this is. If `language_basis: default`, the language is a guess: mirror what the person speaks.

## While you talk

- **Never ask for what the file states** (company, city, categories, last enquiry, last call outcome). Confirm instead: "I see you deal in X — right?"
- Work the **Open items** top-down; they are listed in a fixed order (reply pending → missed call → call-later / meeting → unfinished WhatsApp step → executive tried).
- Respect **Flags**: if it says they asked whether they are talking to a bot, say you are IndiaMART's AI assistant up front; if frustrated or already
  with an executive, be brief and reference that. Honour any request to stop immediately.
- Don't invent facts, prices, leads or promises not in the file; if unsure, offer a callback from an IndiaMART executive.
- Never read out phone numbers, e-mails or IDs (they are redacted in the file anyway).
- Voice: ≤ 2 short sentences per turn, one question at a time. WhatsApp: ≤ 3 short lines, one next step.

## After you talk — write it back (this is what makes memory cross channels)

- Voice agent: configure the **on-end hook** → `POST {BASE}/hooks/on-end {"glid", "call_id", "disposition", "summary", "transcript":[{"speaker","text"}]}`
- Chat/WhatsApp: `POST {BASE}/channels/whatsapp/incoming {"glid","text"}` (also writes the turn), or any other fact via
  `POST {BASE}/events {"glid","role","channel","etype","summary","data","ts","key"}` (idempotent on `key`).
- The file is re-rendered on write (≈ 30 ms); the next channel reads the new version. Don't cache a file across turns longer than a session.

## Guarantees and limits

- Files are ≤ 320 words (≈ 330 tokens median) — safe to put whole into a prompt.
- Window: last 7 days individually, 30 days summarised, unreplied enquiries counted back 60 days; older events are not in the file.
- Sparse GLIDs (≈ 80 % of buyers) have little history; the file says so — fall back to a short discovery question.
- Source of truth is the event store; the markdown is a derived view. Don't edit files by hand — post events instead.
