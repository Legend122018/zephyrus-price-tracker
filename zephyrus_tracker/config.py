"""Configuration loading: TOML file + environment overrides."""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("config.toml")

DEFAULTS: dict[str, Any] = {
    "bestbuy": {
        "api_key": "",
        "requests_per_second": 4.0,
        "timeout_seconds": 30,
        "max_retries": 4,
    },
    "search": {
        # Each term becomes its own Products API query; results are merged.
        "terms": ["zephyrus"],
        "manufacturer": "asus",
        # A product must match one of these in its name to be tracked.
        "include_keywords": ["zephyrus"],
        # ...and none of these (filters out sleeves, chargers, docks, warranties).
        "exclude_keywords": [
            "case", "sleeve", "backpack", "bag", "charger", "adapter", "dock",
            "warranty", "protection", "mouse", "keyboard", "headset", "cable",
        ],
        # Optional allowlist of model families, e.g. ["G14", "G16"]. Empty = all.
        "models": [],
    },
    "location": {
        "postal_code": "02108",   # downtown Boston
        "radius_miles": 25,
        "store_ids": [],          # optional allowlist of store IDs
        "city_allowlist": [],     # optional allowlist of city names
    },
    "thresholds": {
        # Discounts are always measured against the regular LIST price, so a
        # sale and an open-box markdown stack. Best Buy open-box Excellent is
        # routinely ~10% off on its own, which is why the open-box bar sits
        # well above the new-unit bar -- otherwise every listing would alert.
        "heavy_discount_pct": 20.0,      # % off list before a NEW unit is notable
        "open_box_discount_pct": 25.0,   # higher bar: open-box starts discounted
        "price_drop_pct": 5.0,           # drop vs last seen price
        "price_drop_dollars": 100.0,     # ...or this many dollars
        "min_drop_dollars": 40.0,        # ignore noise below this
        "alert_on_all_time_low": True,
        "alert_on_new_product": True,
        "max_price": 0.0,                # 0 = no cap
    },
    "alerts": {
        "cooldown_hours": 24,            # don't repeat the same alert this often
        "include_unavailable": False,    # alert on price even when out of stock
    },
    "storage": {
        "database": "zephyrus.db",
    },
    "notify": {
        "console": {"enabled": True},
        "file": {"enabled": True, "path": "alerts.jsonl"},
        "email": {
            "enabled": False,
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "use_ssl": False,
            "username": "",
            "password": "",
            "from_addr": "",
            "to_addrs": [],
        },
        "ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": "", "token": ""},
        "webhook": {"enabled": False, "url": ""},
    },
}

#: Environment variables that override config values, ``ENV -> (section, key)``.
ENV_OVERRIDES = {
    "BESTBUY_API_KEY": ("bestbuy", "api_key"),
    "ZEPHYRUS_DB": ("storage", "database"),
    "ZEPHYRUS_POSTAL_CODE": ("location", "postal_code"),
    "ZEPHYRUS_NTFY_TOPIC": ("notify.ntfy", "topic"),
    "ZEPHYRUS_SMTP_PASSWORD": ("notify.email", "password"),
    "ZEPHYRUS_WEBHOOK_URL": ("notify.webhook", "url"),
}


class ConfigError(RuntimeError):
    pass


class Config:
    """Dotted-path access over a merged config dict."""

    def __init__(self, data: dict[str, Any], path: Path | None = None) -> None:
        self.data = data
        self.path = path

    # -------------------------------------------------------------- loading

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        data = copy.deepcopy(DEFAULTS)

        if cfg_path.exists():
            with open(cfg_path, "rb") as fh:
                try:
                    user = tomllib.load(fh)
                except tomllib.TOMLDecodeError as exc:
                    raise ConfigError(f"{cfg_path} is not valid TOML: {exc}") from exc
            _deep_merge(data, user)
        elif path is not None:
            raise ConfigError(f"Config file not found: {cfg_path}")

        cfg = cls(data, cfg_path if cfg_path.exists() else None)
        cfg._apply_env()
        return cfg

    def _apply_env(self) -> None:
        for env_name, (section, key) in ENV_OVERRIDES.items():
            value = os.environ.get(env_name)
            if value:
                self.set(f"{section}.{key}", value)
                # An env-provided notifier secret implies you want it switched on.
                if section.startswith("notify.") and key in ("topic", "url"):
                    self.set(f"{section}.enabled", True)

    # --------------------------------------------------------------- access

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def section(self, name: str) -> dict[str, Any]:
        value = self.get(name, {})
        return value if isinstance(value, dict) else {}

    # ------------------------------------------------------------ validation

    def require_api_key(self) -> str:
        key = str(self.get("bestbuy.api_key") or "").strip()
        if not key:
            raise ConfigError(
                "No Best Buy API key configured.\n"
                "  1. Sign up (free, instant) at https://developer.bestbuy.com/\n"
                "  2. export BESTBUY_API_KEY=your_key   (or set bestbuy.api_key in config.toml)"
            )
        return key

    def validate(self) -> list[str]:
        """Return a list of human-readable problems; empty means good to go."""
        problems: list[str] = []
        if not str(self.get("bestbuy.api_key") or "").strip():
            problems.append("bestbuy.api_key is empty (or set BESTBUY_API_KEY)")

        postal = str(self.get("location.postal_code") or "")
        if not (postal.isdigit() and len(postal) == 5):
            problems.append(f"location.postal_code must be a 5-digit ZIP, got {postal!r}")

        if not self.get("search.terms"):
            problems.append("search.terms is empty -- nothing would be tracked")

        email = self.section("notify.email")
        if email.get("enabled"):
            if not email.get("to_addrs"):
                problems.append("notify.email.enabled is true but to_addrs is empty")
            if not email.get("username") or not email.get("password"):
                problems.append("notify.email needs username and password (Gmail: an app password)")

        ntfy = self.section("notify.ntfy")
        if ntfy.get("enabled") and not ntfy.get("topic"):
            problems.append("notify.ntfy.enabled is true but topic is empty")

        webhook = self.section("notify.webhook")
        if webhook.get("enabled") and not webhook.get("url"):
            problems.append("notify.webhook.enabled is true but url is empty")

        if not any(self.section(f"notify.{n}").get("enabled") for n in ("console", "file", "email", "ntfy", "webhook")):
            problems.append("every notifier is disabled -- alerts would go nowhere")

        return problems


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge ``overlay`` into ``base`` in place."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base
