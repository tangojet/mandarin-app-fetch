"""Unified cookie management — single source of truth for all services.

All file-based cookies are stored in Playwright JSON format under
~/.mandarin-app-fetch/{service}-cookies.json with a sibling .meta.json
tracking provenance.

Accepts both raw header strings ("k1=v1; k2=v2") and Playwright JSON
arrays, normalizing internally.

Docker CDP cookies (XHS via Docker, Douyin via Docker, Goofish) are
out of scope — those are live Chrome sessions managed via noVNC login.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("cookie_manager")

COOKIE_DIR = Path.home() / ".mandarin-app-fetch"

# Service name → default cookie domain
SERVICES: dict[str, str] = {
    "xhs": ".xiaohongshu.com",
    "douyin": ".douyin.com",
    "bilibili": ".bilibili.com",
    "weibo": ".weibo.cn",
    "xueqiu": ".xueqiu.com",
    "toutiao": ".toutiao.com",
    "doubao": ".doubao.com",
    "yuanbao": ".tencent.com",
}

# Services that use Docker CDP (not file-based cookies)
CDP_SERVICES = {"goofish"}


def _cookie_path(service: str) -> Path:
    return COOKIE_DIR / f"{service}-cookies.json"


def _meta_path(service: str) -> Path:
    return COOKIE_DIR / f"{service}-cookies.meta.json"


# ---------------------------------------------------------------------------
# Parsing / conversion
# ---------------------------------------------------------------------------

def parse_raw_header(raw: str, domain: str) -> list[dict]:
    """Split a raw Cookie header string into Playwright cookie objects.

    Example input: "k1=v1; k2=v2"
    """
    cookies = []
    for part in raw.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": domain,
            "path": "/",
        })
    return cookies


def cookies_to_header(cookies: list[dict]) -> str:
    """Convert a Playwright cookie list to a raw Cookie header string."""
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def _is_playwright_format(data: Any) -> bool:
    """Check if data looks like a Playwright cookie array."""
    return (
        isinstance(data, list)
        and len(data) > 0
        and isinstance(data[0], dict)
        and "name" in data[0]
        and "value" in data[0]
    )


def detect_and_normalize(data: Any, service: str) -> list[dict]:
    """Accept either a raw header string or Playwright JSON array, normalize.

    Returns a list of Playwright-format cookie dicts.
    """
    domain = SERVICES.get(service, f".{service}.com")

    if isinstance(data, str):
        return parse_raw_header(data, domain)

    if _is_playwright_format(data):
        # Ensure each cookie has at least domain and path
        for c in data:
            c.setdefault("domain", domain)
            c.setdefault("path", "/")
        return data

    raise ValueError(
        f"Unrecognized cookie format for {service}: expected raw header string "
        f"or Playwright JSON array of {{name, value, ...}} objects"
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_cookies(service: str, cookies: list[dict], source: str = "api") -> Path:
    """Write cookies to disk in Playwright JSON format + metadata."""
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cookie_path(service)
    path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))

    meta = {
        "service": service,
        "count": len(cookies),
        "source": source,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _meta_path(service).write_text(json.dumps(meta, indent=2))

    logger.info("saved %d %s cookies to %s (source=%s)", len(cookies), service, path, source)
    return path


def load_cookies(service: str) -> Optional[list[dict]]:
    """Read Playwright-format cookies from disk. Returns None if missing."""
    path = _cookie_path(service)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return data
        logger.warning("unexpected cookie format in %s", path)
        return None
    except Exception as e:
        logger.warning("failed to load cookies from %s: %s", path, e)
        return None


def load_cookies_as_header(service: str) -> Optional[str]:
    """Read cookies from disk and return as a raw header string."""
    cookies = load_cookies(service)
    if not cookies:
        return None
    return cookies_to_header(cookies)


def _load_meta(service: str) -> Optional[dict]:
    """Read metadata for a service's cookies."""
    path = _meta_path(service)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def get_all_status() -> dict:
    """Return cookie status for all known services."""
    status = {}

    for service in SERVICES:
        cookies = load_cookies(service)
        meta = _load_meta(service)
        if cookies is not None:
            entry: dict[str, Any] = {
                "has_cookies": True,
                "count": len(cookies),
            }
            if meta:
                entry["updated_at"] = meta.get("updated_at")
                entry["source"] = meta.get("source")
            status[service] = entry
        else:
            status[service] = {"has_cookies": False}

    for service in CDP_SERVICES:
        status[service] = {
            "type": "docker_cdp",
            "note": "managed via Docker Chrome session",
        }

    return status


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------

def seed_from_env() -> None:
    """Persist DOUBAO_COOKIE_1 env var to file if no file exists yet.

    Called once at startup so the env var bootstraps the first cookie file,
    but subsequent updates go through the API.
    """
    path = _cookie_path("doubao")
    if path.exists():
        logger.info("doubao cookie file already exists, skipping env seed")
        return

    cookie_str = os.environ.get("DOUBAO_COOKIE_1")
    if not cookie_str:
        return

    cookies = parse_raw_header(cookie_str, ".doubao.com")
    if cookies:
        save_cookies("doubao", cookies, source="env")
        logger.info("seeded doubao cookies from DOUBAO_COOKIE_1 env var")


def migrate_yuanbao_txt() -> None:
    """One-time migration of yuanbao-cookies.txt → yuanbao-cookies.json.

    If the old plain-text file exists and no JSON file exists yet, parse the
    raw header string and save in Playwright format.
    """
    json_path = _cookie_path("yuanbao")
    if json_path.exists():
        return

    # Check both default path and env-configured path
    txt_path = Path(os.environ.get(
        "YUANBAO_COOKIE_FILE",
        str(COOKIE_DIR / "yuanbao-cookies.txt"),
    ))
    if not txt_path.exists():
        return

    try:
        raw = txt_path.read_text().strip()
        if not raw:
            return
        cookies = parse_raw_header(raw, ".tencent.com")
        if cookies:
            save_cookies("yuanbao", cookies, source="migrated")
            logger.info("migrated yuanbao cookies from %s", txt_path)
    except Exception as e:
        logger.warning("failed to migrate yuanbao cookies: %s", e)
