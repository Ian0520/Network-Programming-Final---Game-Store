from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional


HW3_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = HW3_ROOT / "config.json"
CONFIG_PATH_ENV = "NP_HW3_CONFIG"


def config_path() -> Path:
    p = (os.environ.get(CONFIG_PATH_ENV) or "").strip()
    if p:
        return Path(p)
    return DEFAULT_CONFIG_PATH


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    path = config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def resolve_path(p: str) -> Path:
    pp = Path(p)
    if pp.is_absolute():
        return pp
    return (HW3_ROOT / pp).resolve()


def section(name: str) -> dict[str, Any]:
    cfg = load_config()
    sec = cfg.get(name)
    return sec if isinstance(sec, dict) else {}


def get_str(sec: dict[str, Any], key: str) -> Optional[str]:
    v = sec.get(key)
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    return None


def get_int(sec: dict[str, Any], key: str) -> Optional[int]:
    v = sec.get(key)
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None

