# Memory across channels — GLID 100145113 (seller)

_Model replies: skipped (no SARVAM_API_KEY) — the memory/hook behaviour below does not depend on it_

## 0. Before anything: what the voice bot knows when the call connects

**on-start → opening_line** (file v2, language `hinglish`):

> Namaste Papi Enterprises, Sahyadri Enterprises ka Amba Poli ke liye enquiry 3 din pehle aaya tha aur abhi tak aapka reply pending hai.

open_items: `4 enquiries without your reply in 30d; latest Sep 28: Sahyadri Enterprises (Nashik) for Amba Poli | 3 buyer calls missed in 30d (latest Sep 28)`

## 1. Channel A — the seller writes on WhatsApp

seller: `Amba Poli ka 500 kg ka best rate bhejiye, kal tak chahiye`

bot: `(no model reply: SARVAM_API_KEY not set)`  — turn written back, context now v2

## 2. Channel B — the voice bot calls a minute later (fresh on-start hook)

**on-start → opening_line** (file v3):

> Namaste Papi Enterprises, aapne aaj WhatsApp par likha tha: “Amba Poli ka 500 kg ka best rate bhejiye, kal tak chahiye” — chaliye wahin se aage badhte hain?

✅ the voice bot's first sentence continues the WhatsApp conversation; nothing was re-asked.

## 3. The call ends — Sarvam's on-end hook reports the outcome

on-end → `{"ok": true, "refreshed": true, "version": 4, "latency_ms": 8.68}`

## 4. Channel A again — the next WhatsApp turn already knows the call

**opening for the next contact** (file v4):

> Namaste Papi Enterprises, pichli baar aapne baad mein call karne ko kaha tha — abhi baat kar sakte hain?

**Prompt the WhatsApp bot would receive now (context file):**

```markdown
---
glid: 100145113
role: seller
as_of: 2026-10-02T10:10:00+05:30
data_through: 2026-10-02 10:09:00
language: hinglish
language_basis: observed
do_not_call: false
window: {recent_days: 7, lookback_days: 30}
version: 4
sources: [call, enquiry, profile, voice_bot, whatsapp]
---
# Papi Enterprises — Badlapur, Maharashtra
Proprietorship · Service Provider and Others · turnover 0 - 40 L · vgFCPplus with PNS
Categories: Aam Papad, Cashew Nuts, Coconut Burfi
KYC: mobile ✔ email ✔ GST ✔ PAN ✔ · products 21 · catalog score 32.0

**Open with:** “Namaste Papi Enterprises, pichli baar aapne baad mein call karne ko kaha tha — abhi baat kar sakte hain?”

## Open items
- 4 enquiries without your reply in 30d; latest Sep 28: Sahyadri Enterprises (Nashik) for Amba Poli
- 3 buyer calls missed in 30d (latest Sep 28)
- On the Oct 02 bot call they asked to be called later

## Last 7 days
- 10-02 [voice_bot] Voice bot call: Call Later / Busy — Seller is on the shop floor; wants the Amba Poli 500 kg quote on WhatsApp and a call back at 6pm.
- 10-02 [whatsapp] whatsapp [live] user: “Amba Poli ka 500 kg ka best rate bhejiye, kal tak chahiye”
- 09-28 [enquiry] Sahyadri Enterprises (Nashik) enquired for Amba Poli
- 09-28 [call] Buyer call NOT connected (missed) on Amba Poli (×2)
- 09-27 [enquiry] You replied (template) on Malvani Khaja (200g): “How much quantity you want sir”
- 09-27 [enquiry] a buyer (Thane) enquired for Malvani Khaja (200g)

## Days 8–30
- 6 enquiries (3 unreplied) — mostly Coconut Barfi / Khobra Wadi, Shengdana Ladoo (25pcs); 1 buyer calls (1 missed)

## History
- Bot calls so far: 3 answered of 7; meetings fixed 0, not interested 2, call-later 1
- 90d: 3 enquiries received, 5 replied; pickup ratio 33.33
```

## Check

- event → refreshed file latency in this run: p50 12.49 ms, p95 12.49 ms
- file size: 294 words
