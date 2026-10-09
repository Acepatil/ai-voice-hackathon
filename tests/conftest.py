import os, shutil, sys
from pathlib import Path
import pytest, yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    """A ContextService on an empty temp store with a fixed clock — tests never touch the real data."""
    import datetime as dt
    monkeypatch.setenv("GCX_DB", str(tmp_path / "t.sqlite"))
    monkeypatch.setenv("GCX_OUT", str(tmp_path / "out"))
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    monkeypatch.delenv("GCX_HOOK_TOKEN", raising=False)
    from gcx.config import load
    from gcx.service import ContextService
    cfg = load()
    cfg.raw["as_of"] = "now"
    s = ContextService(cfg)
    t = [dt.datetime(2026, 10, 2, 12, 0, 0)]
    s.clock = lambda: t[0]
    s.tick = lambda minutes=1: t.__setitem__(0, t[0] + dt.timedelta(minutes=minutes))
    return s
