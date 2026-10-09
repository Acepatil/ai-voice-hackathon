"""Build deploy/demo.sqlite: a small copy of the event store with only the demo GLIDs (no bulk personal data).

Usage:  python scripts/make_demo_db.py [--extra 273498704,52906062] [--out deploy/demo.sqlite]
GLIDs come from examples/demo_campaign_contacts.csv + the files in samples/ (+ --extra).
Needs var/gcx.sqlite (run `python -m gcx ingest` first).
"""
import argparse, csv, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from gcx.db import DB  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--src", default=str(ROOT / "var" / "gcx.sqlite"))
ap.add_argument("--out", default=str(ROOT / "deploy" / "demo.sqlite"))
ap.add_argument("--extra", default="")
a = ap.parse_args()

ids = set(x.strip() for x in a.extra.split(",") if x.strip())
with open(ROOT / "examples" / "demo_campaign_contacts.csv", encoding="utf-8") as f:
    ids |= {r["glid"].strip() for r in csv.DictReader(f)}
for p in (ROOT / "samples").rglob("*.md"):
    if p.stem.isdigit():
        ids.add(p.stem)
if not Path(a.src).exists():
    sys.exit(f"{a.src} not found - run: python -m gcx ingest")

out = Path(a.out)
out.parent.mkdir(parents=True, exist_ok=True)
for suffix in ("", "-wal", "-shm"):
    Path(str(out) + suffix).unlink(missing_ok=True)
DB(out).conn.close()                       # creates the schema
con = sqlite3.connect(out)
con.execute("ATTACH DATABASE ? AS src", (a.src,))
q = ",".join("?" * len(ids))
ids = sorted(ids)
con.execute(f"INSERT OR IGNORE INTO events(glid,role,ts,channel,etype,summary,data,key,ingested_at) "
            f"SELECT glid,role,ts,channel,etype,summary,data,key,ingested_at FROM src.events WHERE glid IN ({q})", ids)
con.execute(f"INSERT OR IGNORE INTO profiles(glid,role,data,updated_at) "
            f"SELECT glid,role,data,updated_at FROM src.profiles WHERE glid IN ({q})", ids)
con.commit()
n_ev = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
n_g = con.execute("SELECT COUNT(DISTINCT glid) FROM events").fetchone()[0]
con.execute("DETACH DATABASE src"); con.execute("VACUUM"); con.close()
print(f"wrote {out}  ({out.stat().st_size/1024:.0f} KB): {n_ev} events, {n_g} GLIDs of {len(ids)} requested")
