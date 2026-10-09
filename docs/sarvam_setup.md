# Wiring Sarvam's console to this server

Docs read for this: Sarvam *Voice Agents* (indus.sarvam.ai → Build → Agents; variables, on-start/on-end hooks, campaigns, deploy).
Variable syntax is the `@` picker; Sarvam's public pages do **not** document the exact hook request/response format, so confirm on the hook screen (see "Unverified").

1. **Run the server somewhere Sarvam can reach**: `python -m gcx serve --host 0.0.0.0 --port 8088` behind HTTPS (cloudflared/ngrok for a trial, a real host for production). Set `GCX_HOOK_TOKEN` in `.env`; in the console add header `Authorization: Bearer <token>` to both hooks.
2. **Create the agent**: Build → Agents → Create from scratch. Paste the greeting and prompt from [`agent_prompt.md`](agent_prompt.md). Voice: Bulbul (v3 or v4) with a Hindi/Hinglish speaker such as `shubh`; language personalisation: start Hindi, allow English + Hinglish switching; ASR is Saaras v4 (tuned for 8 kHz phone audio).
3. **Agent variables (inputs)**: `glid`, `opening_line`, `language`, `open_items`, `do_not_call`, `ai_disclosure_required`, `context_md`, `file_version`, `ab_arm`. Leave `context_md` toggled **on** for the LLM; turn `glid` / `file_version` / `ab_arm` **off** for the LLM (tools/hook only) to save tokens.
4. **On-start hook** → `POST https://<host>/hooks/on-start`. Send `glid` (from the campaign row / telephony metadata) in the body; map the response fields above 1-to-1 into the variables. The server returns a generic opening for unknown GLIDs, so a miss never breaks a call.
5. **On-end hook** → `POST https://<host>/hooks/on-end`. Send `glid`, `call_id`, `disposition`, `summary`, `callback_time`, `transcript` (list of `{speaker,text}`), `duration`, `ab_arm`. The file is re-rendered immediately; the WhatsApp/chat bot's next turn sees the outcome.
6. **Outbound**: Deploy → Campaigns → upload a contact list with a `glid` column, map it to the `glid` variable, set the schedule. Skip rows where the file says `do_not_call: true` (`GET /context/{glid}?fmt=vars` → `do_not_call`), and honour Sarvam's DND settings.
7. **Chat / WhatsApp**: point your WhatsApp provider's inbound webhook at `POST /channels/whatsapp/incoming {glid,text}` (Sarvam's docs I could read do not cover WhatsApp for Voice Agents, so this server provides the brain; the reply text is returned in JSON for you to send).
8. **Measure lift**: set `ab.holdout_pct: 10` in `config.yaml`; after a week `python -m gcx eval lift` compares `personalised` vs `control` dispositions.

## Unverified (check in the console)
- exact JSON shape Sarvam sends to the on-start hook / expects back, timeout, retry (adjust `find_glid` / `as_vars` in `gcx/server.py`)
- on-end payload field names (the server accepts `transcript` as list or string and several spellings of the GLID/outcome)
- whether `context_md` (~2.6 k chars max) fits the variable size limit — if not, shorten `budget.max_chars` or keep only `opening_line` + `open_items` in variables and fetch the rest with an API tool (`GET /context/{glid}?fmt=json`)
