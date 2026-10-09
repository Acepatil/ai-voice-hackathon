"""Text/WhatsApp/voice channel brain: loads the context file, talks via Sarvam, writes the turn back as an event.

The write-back is what makes the memory cross channels: a WhatsApp turn is an event; the file is re-rendered;
the next voice call's on-start hook reads the refreshed file.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .service import ContextService
from .util import LANG_NAMES, clip, detect_lang

_SESSIONS: dict[str, list[dict]] = defaultdict(list)

SYSTEM = """You are IndiaMART's AI assistant, talking to a {role} on {channel}.
Reply in {lang}. {style}

CONTEXT FILE (what IndiaMART already knows about this person — trust it, it is current as of its as_of time):
<<<
{ctx}
>>>

Rules
- Use the file. Never ask for something the file already states (company, city, categories, last enquiry, last call outcome); confirm it instead ("I see you deal in X — right?").
- Start from the "Open with" line unless the person has already said something specific; then follow their lead.
- Do not invent facts, prices, leads, or promises that are not in the file. If unsure, say you will check or arrange a callback from an IndiaMART executive.
- If the file says do_not_call or the person asks to stop, stop politely and confirm; if it flags that they asked about bots, say you are an AI assistant.
- Never read out phone numbers, e-mails or internal IDs."""

STYLE = {
    "voice": "This is a phone call: at most 2 short sentences, plain spoken words, one question at a time, no lists or markdown.",
    "whatsapp": "This is WhatsApp: at most 3 short lines, no markdown tables, one clear next step.",
    "chat": "This is web/app chat: concise, at most 4 lines, one clear next step.",
}


def build_messages(ctx_md: str, role: str, channel: str, lang: str, history: list[dict], user_text: str) -> list[dict]:
    sys = SYSTEM.format(role=role, channel=channel, lang=LANG_NAMES.get(lang, "English"),
                        style=STYLE.get(channel, STYLE["chat"]), ctx=ctx_md.strip())
    return [{"role": "system", "content": sys}, *history[-8:], {"role": "user", "content": user_text}]


def reply(svc: ContextService, glid: str, text: str, *, role: str = "auto", channel: str = "whatsapp",
          session: str | None = None, persist: bool = True) -> dict:
    ctx = svc.get(glid, role)
    if not ctx:
        return {"reply": None, "error": f"unknown GLID {glid}"}
    role = ctx["role"]
    lang = ctx.get("language") or "hinglish"
    seen = detect_lang(text)
    if seen:  # the person's current language beats the file's long-run vote
        lang = seen
    hist = _SESSIONS[session or f"{glid}:{channel}"]
    msgs = build_messages(ctx["markdown"], role, channel, lang, hist, text)
    out = svc.llm.chat(msgs, model=svc.cfg.llm("chat_model"), max_tokens=220, temperature=0.3)
    if persist:
        svc.ingest_event(glid, role, "whatsapp" if channel == "whatsapp" else channel, "wa_chat_turn",
                         f"{channel} [live] user: “{clip(text, 100)}”" + (f" → bot: “{clip(out, 120)}”" if out else ""),
                         {"intent": "live_chat", "user": clip(text, 200), "bot": clip(out or "", 240), "text": clip(text, 200),
                          "session": session or ""})
    if out:
        hist += [{"role": "user", "content": text}, {"role": "assistant", "content": out}]
    return {"reply": out, "language": lang, "role": role, "context_version": ctx["version"],
            "llm_error": None if out else (svc.llm.last_error or "SARVAM_API_KEY not set"),
            "prompt": None if out else msgs}


def voice_turn(svc: ContextService, glid: str, audio_in: str, audio_out: str, role: str = "auto") -> dict:
    """File-based voice loop for local testing: Saaras STT -> reply() -> Bulbul TTS."""
    heard = svc.llm.stt(audio_in)
    if not heard:
        return {"error": svc.llm.last_error or "STT unavailable (no key?)"}
    r = reply(svc, glid, heard, role=role, channel="voice")
    r["heard"] = heard
    if r.get("reply"):
        r["audio_saved"] = svc.llm.tts(re.sub(r"[*_#]", "", r["reply"]), r["language"], audio_out)
        r["audio_path"] = audio_out if r["audio_saved"] else None
    return r


def repl(svc: ContextService, glid: str, role: str = "auto", channel: str = "whatsapp") -> None:  # pragma: no cover
    ctx = svc.get(glid, role)
    if not ctx:
        print("unknown GLID")
        return
    print(f"[{ctx['role']} {glid}] v{ctx['version']} · opening: {ctx.get('opening') or ''}\n(ctrl-c to quit)\n")
    while True:
        try:
            t = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not t:
            continue
        r = reply(svc, glid, t, role=role, channel=channel, session=f"cli:{glid}")
        print("bot>", r["reply"] or f"(no model reply: {r['llm_error']})")
