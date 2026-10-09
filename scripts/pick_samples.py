"""Pick a varied, reproducible set of sample files into samples/ (one per situation the files must handle).
Run after `python -m gcx build --cohort`.   python scripts/pick_samples.py"""
import json, pathlib, random, shutil, sqlite3, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
out, sd = ROOT / "out", ROOT / "samples"
c = sqlite3.connect(ROOT / "var" / "gcx.sqlite")
S = {p.stem: p.read_text(encoding="utf-8") for p in sorted((out / "seller").glob("*.md"))}
B = {p.stem: p.read_text(encoding="utf-8") for p in sorted((out / "buyer").glob("*.md"))}

def pick(d, pred, n=1, seed=3):
    xs = [k for k, v in sorted(d.items()) if pred(v)]
    random.Random(seed).shuffle(xs)
    return xs[:n]

partial = [g for (g, data) in c.execute("select glid,data from profiles where role='buyer'") if '"partial": true' in data]
rich = max((g for g in B if "snapshot" in (c.execute("select data from profiles where glid=? and role='buyer'", (g,)).fetchone() or [""])[0]),
           key=lambda g: len(B[g]), default=None)
sets = {
    "seller: do-not-call flag": pick(S, lambda v: "do_not_call: true" in v),
    "seller: asked to be called later": pick(S, lambda v: "asked to be called later" in v and "## Last 7 days" in v),
    "seller: meeting fixed": pick(S, lambda v: "meeting was fixed" in v),
    "seller: busy, many unreplied enquiries": pick(S, lambda v: "## Last 7 days" in v and len(v.split()) > 270),
    "seller: profile only (sparse)": pick(S, lambda v: "sources: [profile]" in v),
    "seller: Hindi (Devanagari) speaker": pick(S, lambda v: "language: hi\n" in v),
    "seller: English speaker": pick(S, lambda v: "language: en\n" in v and "language_basis: observed" in v),
    "seller: asked if talking to a bot": pick(S, lambda v: "asked whether they are talking to a bot" in v),
    "seller: WhatsApp step left unfinished": pick(S, lambda v: "left unfinished" in v),
    "buyer: getContext, rich activity": [rich] if rich else [],
    "buyer: getContext, truncated row salvaged": [g for g in partial if g in B][:1],
    "buyer: enquiry got a seller reply": pick(B, lambda v: "got a seller reply" in v),
    "buyer: sellers bought the requirement": pick(B, lambda v: "bought your requirement" in v),
    "buyer: WhatsApp requirement flow (a seller acting as buyer)": pick(B, lambda v: "via WhatsApp" in v and "Open items" in v),
}
shutil.rmtree(sd, ignore_errors=True)
(sd / "seller").mkdir(parents=True); (sd / "buyer").mkdir(parents=True)
idx = ["# Sample files\n", "Real GLIDs from the dataset, one per situation the files must handle. Regenerate any with `python -m gcx build --glid <GLID>`.\n",
       "| Situation | Role | GLID | Words |", "|---|---|---|---|"]
for k, v in sets.items():
    for g in v:
        role = k.split(":")[0]
        src = S if role == "seller" else B
        shutil.copy(out / role / f"{g}.md", sd / role / f"{g}.md")
        idx.append(f"| {k.split(': ', 1)[1]} | {role} | `{g}` | {len(src[g].split())} |")
(sd / "INDEX.md").write_text("\n".join(idx) + "\n", encoding="utf-8")
print("\n".join(idx))
