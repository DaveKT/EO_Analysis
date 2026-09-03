"""Environment-driven settings.

The OpenRouter key is read from `eo_openrouterkey` and from nothing else. There
is deliberately no fallback to other variable names: this project had a stale
key sitting in the environment under a different name, and a fallback chain
would silently prefer it. There is also no my_secrets.py -- v1 kept an API key
in plaintext in the working tree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

API_KEY_VAR = "eo_openrouterkey"

load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


def _path(env_var: str, default: str) -> Path:
    raw = os.getenv(env_var, default)
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    raw_dir: Path
    model: str
    concurrency: int
    openrouter_api_key: str | None
    openrouter_base_url: str

    def require_api_key(self) -> str:
        """Return the OpenRouter key, or explain how to set it.

        Only the LLM stages call this; fetch/validate/export/status must keep
        working without a key.
        """
        if not self.openrouter_api_key:
            raise ConfigError(
                f"{API_KEY_VAR} is not set. Add it to .env (see .env.example) "
                "or export it in your shell."
            )
        return self.openrouter_api_key


def load_settings() -> Settings:
    return Settings(
        db_path=_path("EO_DB_PATH", "data/eo.db"),
        raw_dir=_path("EO_RAW_DIR", "data/raw"),
        model=os.getenv("EO_MODEL", "openai/gpt-oss-120b"),
        concurrency=int(os.getenv("EO_CONCURRENCY", "8")),
        openrouter_api_key=os.getenv(API_KEY_VAR) or None,
        openrouter_base_url=os.getenv(
            "EO_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        ),
    )
