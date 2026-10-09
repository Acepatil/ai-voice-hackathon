# Submission guide — Problem 2, on the Sarvam platform

Checklist from the slide: **demo video (5–7 min)** · **problem statement + approach note** · **Agent ID / live link** · **solution folder (code + README + sample outputs)** · **skills.md** · **team name + members**.
Submission window: 2:00 PM – 11:59 PM, Day 2. Upload once per team.

## Part 1 — Build the agent on Sarvam (≈ 40 min, no server needed: "Path A")

Console: **indus.sarvam.ai** (login with the team credentials from dashboard.sarvam.ai). Names below follow Sarvam's docs; menu labels may differ slightly.

1. **Build → Agents → Create from Scratch.** Name it `IndiaMART Global Context`.
2. **Variables** (type `@` in the canvas → create *agent variables*, label **Input**): `glid`, `company`, `opening_line`, `language`, `open_items`, `flags`, `context_brief`, `do_not_call`.
   Give each a **default** (copy one row of `examples/demo_campaign_contacts.csv`) so *test agent* works immediately.
3. **Instructions tab.**
   - Greeting: `@opening_line`
   - System prompt: paste from `docs/agent_prompt.md`, then replace `@context_md` with `@context_brief` and add the line `Flags: @flags` under it.
4. **Settings → Language personalisation:** start language Hindi; allow switching; allowed: Hindi, English (Hinglish speech works through Saaras v4). **Speakers & voice:** Bulbul (v3 or v4), a Hindi speaker (e.g. `shubh`), pace ≈ 1.0.
5. **Test agent** (microphone). Say as the seller: *"haan bolo"*, *"abhi busy hoon, shaam ko call karo"*, *"mere enquiry ka kya hua?"*. Check: the first sentence is the personalised line; the bot never asks what you sell or where you are; it ends politely on "call later".
6. **Swap the variable defaults** to a second contact (row 3 is English, row 1 Hinglish) and test again — this shows memory differs per user.
7. **Commit the version** (deploys use a committed version, not the draft). Copy the **Agent ID** / link → put it in `skills.md` and the form.
8. **Outbound proof (optional):** *Deploy → Outbound Campaigns* → upload `examples/demo_campaign_contacts.csv` (or regenerate: `python -m gcx export-campaign --n 20`), map columns to the variables above, call your own number. Review under *Monitor → Agent Analytics*.

## Part 2 — Cross-channel + live memory ("Path B", +30 min, strongest demo)
1. `python -m gcx serve --port 8088` and expose it: `cloudflared tunnel --url http://localhost:8088` (copy the https URL). Add `GCX_HOOK_TOKEN` in `.env`.
2. In the agent: **On-start hook** → `POST <url>/hooks/on-start` with body `{"glid": "@glid"}`; map response fields `opening_line, language, open_items, context_md…` into the variables. **On-end hook** → `POST <url>/hooks/on-end` with `glid, call_id, disposition, summary, transcript`. (Add the `Authorization: Bearer <token>` header.)
   *If the hook screen wants a different shape, adjust `find_glid` / `as_vars` in `gcx/server.py` — 10-minute change.*
3. WhatsApp/chat side: `python -m gcx chat <glid> --channel whatsapp` (needs `SARVAM_API_KEY`) — type a message; it is written to memory.
4. Call the agent again → the opening now continues that message. `python examples/resume_across_channels.py <glid>` prints the same flow as a transcript if the live path misbehaves during recording.

## Part 3 — Approach note (paste into the form, ~120 words)
> **Problem 2 — Global Context: Memory Across Channels.** We built a context layer keyed by GLID that turns enquiries, buyer calls, buy-leads, WhatsApp, earlier VANI calls, executive calls and the getContext API into one short, always-current `seller.md` / `buyer.md` (median ~170 words). Facts (open items, flags, language) are computed deterministically; Sarvam-105B only compresses long histories. The Sarvam Voice Agent loads the file before it speaks — greeting is a personalised opening line — and resumes on WhatsApp/chat via the same file. Every call or chat turn writes back through on-end/webhook events and re-renders the file in ~33 ms. Lookback: 7 days detailed, 30 summarised, 60 for open items. Files are plain markdown + a SKILL for reuse beyond the bot.

## Part 4 — Demo video storyboard (6 min)
| Time | Show | Say |
|---|---|---|
| 0:00–0:40 | Problem slide → one cold historical call transcript (`gc_bot_call_turns`, "kya aap X se bol rahe hain?") | "76% of past calls started from nothing." |
| 0:40–1:40 | `samples/seller/…` file side by side with raw CSV rows it came from; point at *Open with*, *Open items*, flags | "One short file per GLID; every line traces to a source." |
| 1:40–3:10 | Sarvam console **test agent** with seller A (Hinglish) then seller B (English): different first sentence, no re-asking | "Same agent, memory differs per user." |
| 3:10–4:40 | Cross-channel: send WhatsApp text via `gcx chat` → call agent → opening continues it → call ends "call later" → show file updated (version bump, ≈33 ms) | "Memory crosses channels and stays fresh." |
| 4:40–5:30 | `reports/*.json`: freshness, compactness, window table | "Measured on 8,278 files." |
| 5:30–6:15 | Limits slide: lift needs live A/B (harness ready), buyer sparsity, hook schema | "What we'd do next." |

Tips: record at 1080p, mic close, keep terminal font large; do one dry run; prepare both GLIDs' files in tabs beforehand.

## Part 5 — Package & submit
- Zip the folder (already has code, README.md, skills.md, SKILL.md, samples, out/, reports/, docs/). Don't include `.env`, `data/`, `var/`.
- Fill team name/members in `skills.md`; paste the Agent ID/link.
- Upload video + zip + approach note via the Day-1 link before 11:59 PM.
