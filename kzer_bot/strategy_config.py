"""Validated, hashable configuration for the SPY 0DTE strategy."""
from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "config" / "spy_0dte.toml"


@dataclass(frozen=True)
class StrategyConfig:
    raw: dict
    path: Path
    hash: str

    def get(self, section: str, key: str):
        return self.raw[section][key]


def load_strategy_config(path: Path = DEFAULT_PATH) -> StrategyConfig:
    raw_bytes = path.read_bytes()
    raw = tomllib.loads(raw_bytes.decode("utf-8"))
    required = {"contract", "signal", "schedule", "exit", "promotion", "events", "execution"}
    missing = required - raw.keys()
    if missing:
        raise ValueError(f"Missing strategy config sections: {sorted(missing)}")
    lo = float(raw["contract"]["premium_min"])
    hi = float(raw["contract"]["premium_max"])
    mid = float(raw["contract"]["premium_mid"])
    if not (0 < lo <= mid <= hi):
        raise ValueError("Contract premium bounds must satisfy 0 < min <= mid <= max")
    if not (0 < float(raw["contract"]["max_spread_pct"]) <= 1):
        raise ValueError("max_spread_pct must be in (0, 1]")
    mode = str(raw["execution"]["execution_mode"]).strip().lower()
    if mode not in {"auto", "review", "shadow"}:
        raise ValueError("execution_mode must be one of: auto, review, shadow")
    early_minute = int(raw["schedule"]["early_put_entry_minute"])
    if not 30 <= early_minute < int(raw["schedule"]["entry_minute_start"]):
        raise ValueError("early_put_entry_minute must be between 30 and the standard entry minute")
    digest = hashlib.sha256(raw_bytes).hexdigest()[:12]
    return StrategyConfig(raw=raw, path=path, hash=digest)


def config_summary(config: StrategyConfig) -> str:
    return json.dumps({
        "strategy_version": config.raw.get("strategy_version"),
        "config_hash": config.hash,
        "execution_mode": config.raw.get("execution", {}).get("execution_mode"),
        "path": str(config.path),
    }, sort_keys=True)
