"""Small helpers: redaction, clipping, timestamps, language detection."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections import Counter
from zoneinfo import ZoneInfo

PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL = re.compile(r"https?://\S+")
WS = re.compile(r"\s+")


def redact(text: str | None) -> str:
    """Strip phone numbers, e-mails and URLs. Message bodies in the export still contain them."""
    if not text:
        return ""
    t = str(text).replace("\\/", "/")  # JSON-escaped slashes (http:\/\/...) would dodge the URL pattern
    if re.match(r"\s*[\[{]\s*\"", t):  # a raw JSON payload (image/template message), not something a person said
        return "[structured message]"
    t = PHONE.sub("[phone]", t)
    t = EMAIL.sub("[email]", t)
    t = URL.sub("[link]", t)
    return WS.sub(" ", t).strip()


def clip(text: str | None, n: int) -> str:
    t = WS.sub(" ", str(text or "")).strip()
    return t if len(t) <= n else t[: max(0, n - 1)].rstrip() + "…"


def sha(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y%m%d%H%M%S",
            "%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y %H:%M")


def parse_ts(value) -> dt.datetime | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in {"nan", "nat", "none", "null"}:
        return None
    s = re.sub(r"(\.\d{1,6})\d*", r"\1", s)  # trim nanoseconds
    s = re.sub(r"(Z|[+-]\d\d:\d\d)$", "", s)  # drop tz suffix; data is IST throughout
    for f in _FORMATS:
        try:
            return dt.datetime.strptime(s, f)
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def ts_str(d: dt.datetime | None) -> str | None:
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else None


def to_int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def truthy(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "t", "y", "yes", "1.0"}


def ago(days: float, lang: str = "en") -> str:
    d = int(days)
    if lang == "en":
        return "today" if d <= 0 else "yesterday" if d == 1 else f"{d} days ago"
    if lang == "hi":
        return "आज" if d <= 0 else "कल" if d == 1 else f"{d} दिन पहले"
    return "aaj" if d <= 0 else "kal" if d == 1 else f"{d} din pehle"


# ----------------------------------------------------------------- language
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_HINGLISH = set("""hai hain nahi nahin kya aap aapka aapke aapki mujhe mere meri chahiye chahie bhai ji haan han
kitna kitne rate bhejo bhej do dena dijiye karo kijiye kar raha rahe rahi tha thi abhi baad mein me se ko ka ki ke
kaun kab kahan kaise wala wali bolo bol bataiye batao theek thik accha achha samajh dekh dekho karna hoga paisa
ruk ruko call krna kr hoon hu hun toh to bas sir madam""".split())


def detect_lang(text: str | None) -> str | None:
    """'hi' (Devanagari), 'hinglish' (romanised Hindi) or 'en'. None when there is no signal."""
    if not text:
        return None
    t = str(text)
    dev = len(_DEVANAGARI.findall(t))
    if dev >= 3:
        return "hi"
    words = re.findall(r"[a-zA-Z]+", t.lower())
    if len(words) < 2:
        return None
    hits = sum(1 for w in words if w in _HINGLISH)
    if hits >= 2 or (hits >= 1 and hits / len(words) >= 0.25):
        return "hinglish"
    return "en" if len(words) >= 3 else None


def vote_lang(items: list[tuple[str | None, float]], default: str = "hinglish") -> str:
    """items = [(lang, weight)]. Newer evidence gets a bigger weight from the caller."""
    c: Counter = Counter()
    for lang, w in items:
        if lang:
            c[lang] += w
    return c.most_common(1)[0][0] if c else default


LANG_NAMES = {"en": "English", "hinglish": "Hinglish (romanised Hindi mixed with English)", "hi": "Hindi (Devanagari)"}
TTS_CODE = {"en": "en-IN", "hinglish": "hi-IN", "hi": "hi-IN"}


def now_ist() -> dt.datetime:
    """Naive IST wall-clock time: the dataset's timestamps are all IST."""
    return dt.datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None, microsecond=0)
