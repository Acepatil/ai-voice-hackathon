from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Config:
    raw: dict
    root: Path = ROOT

    # ---- paths
    def path(self, key: str) -> Path:
        env = {"seller_dir": "GCX_SELLER_DIR", "buyer_csv": "GCX_BUYER_CSV", "db": "GCX_DB", "out": "GCX_OUT"}.get(key)
        v = os.environ.get(env, "") if env else ""
        v = v or self.raw["paths"][key]
        p = Path(v)
        return p if p.is_absolute() else (self.root / p).resolve()

    # ---- window / budget
    @property
    def recent_days(self) -> int:
        return int(self.raw["window"]["recent_days"])

    @property
    def lookback_days(self) -> int:
        return int(self.raw["window"]["lookback_days"])

    @property
    def max_words(self) -> int:
        return int(self.raw["budget"]["max_words"])

    @property
    def max_chars(self) -> int:
        return int(self.raw["budget"]["max_chars"])

    # ---- llm
    @property
    def api_key(self) -> str:
        return os.environ.get("SARVAM_API_KEY", "").strip()

    @property
    def llm_enabled(self) -> bool:
        v = str(self.raw["llm"].get("enabled", "auto")).lower()
        if v in {"false", "0", "no", "off"}:
            return False
        return bool(self.api_key)

    def llm(self, key: str):
        return self.raw["llm"][key]

    @property
    def holdout_pct(self) -> int:
        return int(self.raw.get("ab", {}).get("holdout_pct", 0))

    @property
    def hook_token(self) -> str:
        return os.environ.get("GCX_HOOK_TOKEN", "").strip()

    def as_of_mode(self):
        """'now' | 'data_end' | datetime (pinned)."""
        v = str(self.raw.get("as_of", "data_end"))
        if v in ("now", "data_end"):
            return v
        from .util import parse_ts
        return parse_ts(v) or "data_end"


def load(path: str | os.PathLike | None = None) -> Config:
    _load_dotenv(ROOT / ".env")
    p = Path(path or os.environ.get("GCX_CONFIG") or ROOT / "config.yaml")
    with open(p, encoding="utf-8") as f:
        return Config(yaml.safe_load(f), root=p.parent if p.is_absolute() else ROOT)
