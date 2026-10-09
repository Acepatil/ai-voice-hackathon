"""Facts -> compact markdown with YAML front matter, trimmed to a word/char budget."""
from __future__ import annotations

import datetime as dt
import re

from .config import Config
from .util import clip

REQUIRED = {"seller": ["Open with", "Open items", "Last", "History"], "buyer": ["Open with", "Open items", "Last"]}
FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def _yn(b) -> str:
    return "✔" if b else "✘"


def _identity_lines(role: str, f: dict) -> tuple[str, list[str]]:
    i = f["identity"]
    if role == "seller":
        title = f"# {i.get('name') or 'Seller ' + f['glid']}" + (f" — {', '.join(x for x in (i.get('city'), i.get('state')) if x)}" if i.get("city") else "")
        bits = [x for x in (i.get("business_type"), i.get("nature"), i.get("turnover") and f"turnover {i['turnover']}", i.get("plan")) if x]
        k = i["kyc"]
        lines = []
        if bits:
            lines.append(" · ".join(bits))
        if i.get("categories"):
            lines.append("Categories: " + ", ".join(i["categories"]))
        lines.append(f"KYC: mobile {_yn(k['mobile'])} email {_yn(k['email'])} GST {_yn(k['gst'])} PAN {_yn(k['pan'])}"
                     + (f" · products {i['products']}" if i.get("products") else "")
                     + (f" · catalog score {i['catalog_score']}" if i.get("catalog_score") else ""))
        return title, lines
    nm = i.get("name") or i.get("company") or f"Buyer {f['glid']}"
    title = f"# {nm}" + (f" ({i['company']})" if i.get("name") and i.get("company") else "") + (f" — {i['city']}" if i.get("city") else "")
    bits = [x for x in (i.get("designation"), i.get("plan"), i.get("vintage") and f"since {i['vintage']}") if x and x not in ("--", "-")]
    lines = [" · ".join(bits)] if bits else []
    if f.get("interests"):
        lines.append("Interested in: " + ", ".join(f["interests"]))
    return title, lines


def render(f: dict, cfg: Config, *, version: int = 1, as_of: dt.datetime | None = None) -> str:
    role = f["role"]
    R, L = cfg.recent_days, cfg.lookback_days
    title, id_lines = _identity_lines(role, f)

    # (priority, header, lines, keep_min) — lower priority number = kept longest
    blocks: list[list] = []
    opening = f["opening"] or "(no opening: do not initiate contact)"
    blocks.append([0, "Open with", [f"“{opening}”"], 1])
    if f["flags"]:
        blocks.append([1, "Flags", [f"- {x}" for x in f["flags"]], 1])
    if f["open_items"]:
        blocks.append([2, "Open items", [f"- {o['text']}" for o in f["open_items"]], 1])
    if f["recent"]:
        blocks.append([3, f"Last {R} days", [f"- {e['ts'][5:10]} [{e['channel']}] {clip(e['text'], 150)}" for e in f["recent"]], 0])
    if f["window"] or f.get("digest"):
        ls = ([f"- {f['window']}"] if f["window"] else []) + ([f"- Gist: {f['digest']}"] if f.get("digest") else [])
        blocks.append([4, f"Days {R + 1}–{L}", ls, 0])
    if f["history"]:
        blocks.append([5, "History", [f"- {h}" for h in f["history"]], 0])

    def words() -> int:
        txt = "\n".join([title, *id_lines] + [x for b in blocks for x in [b[1], *b[2]]])
        return len(txt.split())

    def chars() -> int:
        return len("\n".join([title, *id_lines] + [x for b in blocks for x in [b[1], *b[2]]]))

    while (words() > cfg.max_words or chars() > cfg.max_chars):
        cand = [b for b in blocks if len(b[2]) > b[3]]
        if not cand:
            break
        worst = max(cand, key=lambda b: b[0])  # trim the least important block first, from its tail
        worst[2].pop()
        if not worst[2]:
            blocks.remove(worst)

    data_through = f["data_through"] or ""
    asof = (as_of or dt.datetime.now()).strftime("%Y-%m-%dT%H:%M:%S+05:30")
    srcs = f.get("sources", [])
    fm = ["---", f"glid: {f['glid']}", f"role: {role}", f"as_of: {asof}", f"data_through: {data_through or 'n/a'}",
          f"language: {f['language']}", f"language_basis: {'observed' if f.get('lang_observed') else 'default'}", f"do_not_call: {str(f['do_not_call']).lower()}",
          f"window: {{recent_days: {R}, lookback_days: {L}}}", f"version: {version}",
          f"sources: [{', '.join(srcs)}]", "---"]
    out = fm + [title, *id_lines]
    for _, h, ls, _k in blocks:
        out += ["", "**Open with:** " + ls[0]] if h == "Open with" else ["", f"## {h}", *ls]
    return "\n".join(out).strip() + "\n"


def body_for_hash(md: str) -> str:
    """Hash input: everything except as_of/version, so unchanged content never bumps the version."""
    return re.sub(r"^(as_of|version): .*$", "", md, flags=re.M)


def parse_front_matter(md: str) -> dict:
    m = FM_RE.match(md)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def validate(md: str, role: str) -> list[str]:
    """Structure check used by the eval: parseable front matter + required sections present."""
    errs = []
    fm = parse_front_matter(md)
    for k in ("glid", "role", "as_of", "data_through", "language", "version"):
        if k not in fm:
            errs.append(f"front matter missing {k}")
    body = FM_RE.sub("", md)
    if not body.lstrip().startswith("# "):
        errs.append("no title")
    for sec in REQUIRED[role]:
        if sec == "Open with" and "**Open with:**" not in body:
            errs.append("missing opening")
        elif sec not in ("Open with", "Last", "Open items", "History") and f"## {sec}" not in body:
            errs.append(f"missing section {sec}")
    return errs
