"""Settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    grocy_url: str
    grocy_api_key: str
    anthropic_api_key: str
    claude_model: str
    claude_model_cheap: str
    claude_fallbacks: bool
    usda_fdc_api_key: str
    ntfy_topic_url: str
    tz: str
    companion_token: str
    db_path: str
    people: int
    alert_hour: int
    alert_due_days: int
    off_user_agent: str

    @property
    def grocy_api(self) -> str:
        return self.grocy_url.rstrip("/") + "/api"


@lru_cache
def get_settings() -> Settings:
    env = os.environ.get
    return Settings(
        grocy_url=env("GROCY_URL", "http://localhost:8080"),
        grocy_api_key=env("GROCY_API_KEY", ""),
        anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
        claude_model=env("CLAUDE_MODEL", "claude-sonnet-5-5"),
        claude_model_cheap=env("CLAUDE_MODEL_CHEAP", "claude-haiku-5-5"),
        claude_fallbacks=_bool(env("CLAUDE_FALLBACKS"), True),
        usda_fdc_api_key=env("USDA_FDC_API_KEY", "") or "DEMO_KEY",
        ntfy_topic_url=env("NTFY_TOPIC_URL", ""),
        tz=env("TZ", "Europe/Vilnius"),
        companion_token=env("COMPANION_TOKEN", ""),
        db_path=env("COMPANION_DB_PATH", "/data/companion.db"),
        people=int(env("HOUSEHOLD_PEOPLE", "2")),
        alert_hour=int(env("ALERT_HOUR", "8")),
        alert_due_days=int(env("ALERT_DUE_DAYS", "3")),
        off_user_agent=env("OFF_USER_AGENT", "GrocyPlus-Companion/0.1 (self-hosted household app)"),
    )
