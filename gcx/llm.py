"""Sarvam client wrapper (chat, STT, TTS) with caching and graceful degradation.

Everything that *needs* the network is optional: if SARVAM_API_KEY is missing or a call fails, callers get None
and fall back to deterministic text, so the context files are always buildable offline.
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

from .config import Config
from .db import DB
from .util import LANG_NAMES, TTS_CODE, sha

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


class Sarvam:
    def __init__(self, cfg: Config, db: DB | None = None):
        self.cfg, self.db = cfg, db
        self._client = None
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self.cfg.llm_enabled

    @property
    def client(self):
        if self._client is None:
            from sarvamai import SarvamAI  # imported lazily so offline builds don't need the SDK to work

            self._client = SarvamAI(api_subscription_key=self.cfg.api_key, timeout=float(self.cfg.llm("timeout_s")))
        return self._client

    # ------------------------------------------------------------------ chat
    def chat(self, messages: list[dict], *, model: str | None = None, max_tokens: int = 400,
             temperature: float = 0.2) -> str | None:
        if not self.enabled:
            return None
        try:
            r = self.client.chat.completions(model=model or self.cfg.llm("model"), messages=messages,
                                             temperature=temperature, max_tokens=max_tokens)
            txt = (r.choices[0].message.content or "").strip()
            return _THINK.sub("", txt).strip() or None
        except Exception as e:  # network, quota, bad key ... never break a build or a call
            self.last_error = f"{type(e).__name__}: {e}"
            return None

    def digest(self, kind: str, items: list[str], lang: str = "en", max_words: int = 25) -> str | None:
        """Compress several free-text items into one short line. Cached by content hash."""
        items = [i for i in items if i]
        if not self.enabled or len(items) < 2:
            return None
        h = sha("digest", kind, lang, max_words, *items)
        if self.db and (hit := self.db.cache_get(h)) is not None:
            return hit or None
        sys = ("You compress CRM history for a voice assistant. Reply with ONE line of at most "
               f"{max_words} words in {LANG_NAMES.get(lang, 'English')}. Use only facts present in the input; "
               "no names of people, no phone numbers, no guesses, no preamble.")
        out = self.chat([{"role": "system", "content": sys},
                         {"role": "user", "content": f"Topic: {kind}\n" + "\n".join(f"- {i}" for i in items[:12])}],
                        max_tokens=120, temperature=0.1)
        if out and self.db:
            self.db.cache_put(h, out, self.cfg.llm("model"))
        return out

    # ------------------------------------------------------------ voice I/O
    def stt(self, audio_path: str | Path, mode: str = "transcribe") -> str | None:
        if not self.enabled:
            return None
        try:
            with open(audio_path, "rb") as f:
                r = self.client.speech_to_text.transcribe(file=f, model=self.cfg.llm("stt_model"), mode=mode)
            return r.transcript
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return None

    def tts(self, text: str, lang: str, out_path: str | Path) -> bool:
        if not self.enabled:
            return False
        try:
            r = self.client.text_to_speech.convert(text=text[:2500], language_code=TTS_CODE.get(lang, "hi-IN"),
                                                   speaker=self.cfg.llm("tts_speaker"), model=self.cfg.llm("tts_model"))
            Path(out_path).write_bytes(base64.b64decode(r.audios[0]))
            return True
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return False
