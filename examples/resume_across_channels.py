"""End-to-end proof that memory crosses channels, through the real HTTP hooks (no mocks).

  WhatsApp message  ->  event  ->  file refreshed  ->  next voice call's on-start hook already knows it
  voice call ends   ->  on-end hook -> file refreshed  ->  next WhatsApp/chat turn already knows the outcome

Runs on a *copy* of the event store (var/resume_sandbox) so your real store is untouched.
With SARVAM_API_KEY set, the WhatsApp step also gets a real model reply grounded in the file.

    python examples/resume_across_channels.py [GLID]  ->  writes examples/resume_session.md
"""
import os, shutil, sys, json, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sb = ROOT / "var" / "resume_sandbox"
shutil.rmtree(sb, ignore_errors=True); sb.mkdir(parents=True)
shutil.copy(ROOT / "var" / "gcx.sqlite", sb / "gcx.sqlite")
os.environ["GCX_DB"], os.environ["GCX_OUT"] = str(sb / "gcx.sqlite"), str(sb / "out")

from fastapi.testclient import TestClient  # noqa: E402
from gcx import server  # noqa: E402

import datetime as dt  # noqa: E402
# Pin a fake wall clock that starts just after the dataset ends (2 Oct 2026) and advances 1 minute per step,
# and use "as_of: now" semantics so live events and history share one timeline.
_t = [dt.datetime(2026, 10, 2, 10, 5, 0)]
def _clock(): _t[0] += dt.timedelta(minutes=1); return _t[0]
server.svc.clock = _clock
server.svc.cfg.raw["as_of"] = "now"
c = TestClient(server.app)
GLID = sys.argv[1] if len(sys.argv) > 1 else "100145113"
log = []
def say(s=""): log.append(s); print(s)
def body(md): return md.split("\n---\n", 1)[-1].strip()

say(f"# Memory across channels — GLID {GLID} (seller)\n")
say(f"_Model replies: {'Sarvam ' + server.svc.cfg.llm('chat_model') if server.svc.llm.enabled else 'skipped (no SARVAM_API_KEY) — the memory/hook behaviour below does not depend on it'}_\n")

say("## 0. Before anything: what the voice bot knows when the call connects\n")
v0 = c.post("/hooks/on-start", json={"glid": GLID}).json()
say(f"**on-start → opening_line** (file v{v0['file_version']}, language `{v0['language']}`):\n\n> {v0['opening_line']}\n")
say(f"open_items: `{v0['open_items']}`\n")

say("## 1. Channel A — the seller writes on WhatsApp\n")
msg = "Amba Poli ka 500 kg ka best rate bhejiye, kal tak chahiye"
r = c.post("/channels/whatsapp/incoming", json={"glid": GLID, "text": msg})
say(f"seller: `{msg}`\n")
j = r.json()
say(f"bot: `{j.get('reply') or '(no model reply: ' + str(j.get('llm_error')) + ')'}`  — turn written back, context now v{j.get('context_version', '?')}\n")

say("## 2. Channel B — the voice bot calls a minute later (fresh on-start hook)\n")
v1 = c.post("/hooks/on-start", json={"glid": GLID}).json()
say(f"**on-start → opening_line** (file v{v1['file_version']}):\n\n> {v1['opening_line']}\n")
assert "WhatsApp" in v1["opening_line"] and "500 kg" in v1["opening_line"], "voice call did not pick up the WhatsApp message"
assert v1["opening_line"] != v0["opening_line"]
say("✅ the voice bot's first sentence continues the WhatsApp conversation; nothing was re-asked.\n")

say("## 3. The call ends — Sarvam's on-end hook reports the outcome\n")
e = c.post("/hooks/on-end", json={"glid": GLID, "call_id": "demo-1", "disposition": "Call Later / Busy", "duration": 41,
        "summary": "Seller is on the shop floor; wants the Amba Poli 500 kg quote on WhatsApp and a call back at 6pm.",
        "transcript": [{"speaker": "bot", "text": "Namaste, aapne WhatsApp par Amba Poli ka rate poocha tha, wahin se aage badhein?"},
                       {"speaker": "seller", "text": "Haan, abhi busy hoon, shaam 6 baje call karo aur rate WhatsApp pe bhej do"}]}).json()
say(f"on-end → `{json.dumps(e)}`\n")

say("## 4. Channel A again — the next WhatsApp turn already knows the call\n")
v2 = c.post("/hooks/on-start", json={"glid": GLID}).json()
say(f"**opening for the next contact** (file v{v2['file_version']}):\n\n> {v2['opening_line']}\n")
ctx = c.get(f"/context/{GLID}").text
say("**Prompt the WhatsApp bot would receive now (context file):**\n")
say("```markdown\n" + ctx + "```\n")
assert "Call Later" in ctx and "called later" in ctx and "500 kg" in ctx
assert "baad mein call" in v2["opening_line"], "the call outcome should now drive the next opening"
say("## Check\n")
m = c.get("/metrics").json()
say(f"- event → refreshed file latency in this run: p50 {m['refresh_latency_ms']['p50']} ms, p95 {m['refresh_latency_ms']['p95']} ms\n- file size: {len(ctx.split())} words")
(ROOT / "examples" / "resume_session.md").write_text("\n".join(log) + "\n", encoding="utf-8")
