"""User settings: ~/.moni_pod/config.json. Defaults confirmed by the user on 2026-10-06 (PLAN §7)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from . import config


@dataclass(frozen=True)
class Settings:
    ttl_default_hours: float = 2.0
    ttl_max_hours: float = 8.0
    session_budget_usd: float = 2.0
    stale_stopped_hours: float = 24.0  # highlight stopped pods older than this (disk keeps billing)

    def to_dict(self) -> dict:
        return asdict(self)


def load() -> Settings:
    path = config.home_dir() / "config.json"
    if not path.exists():
        return Settings()
    data = json.loads(path.read_text(encoding="utf-8"))
    known = Settings.__dataclass_fields__
    return Settings(**{k: type(getattr(Settings, k))(v) for k, v in data.items() if k in known})
