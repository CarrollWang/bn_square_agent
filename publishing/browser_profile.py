from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re


def browser_profile_root() -> Path:
    configured = os.getenv("BINANCE_PROFILE_ROOT", "./data/browser_profiles").strip()
    return Path(configured or "./data/browser_profiles").expanduser().resolve()


def browser_profile_path(account_key: str) -> Path:
    key = account_key.strip()
    if not key:
        raise ValueError("账号标识不能为空")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-._")[:40] or "account"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return browser_profile_root() / f"{slug}-{digest}"


def browser_profile_dir(account_key: str) -> Path:
    profile_dir = browser_profile_path(account_key)
    profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return profile_dir
