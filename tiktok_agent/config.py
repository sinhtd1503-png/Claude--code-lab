"""Load settings from config.yaml + .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    raw: dict
    anthropic_api_key: str | None
    apify_token: str | None
    pixabay_api_key: str | None
    telegram_bot_token: str | None
    telegram_chat_id: int | None
    tiktok_client_key: str | None
    tiktok_client_secret: str | None
    tiktok_redirect_uri: str | None
    data_dir: Path

    def __getitem__(self, key):
        return self.raw[key]


def load_settings(config_path: str | Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env")
    path = Path(config_path) if config_path else ROOT / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    data_dir = ROOT / raw.get("data_dir", "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    return Settings(
        raw=raw,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        apify_token=os.getenv("APIFY_TOKEN"),
        pixabay_api_key=os.getenv("PIXABAY_API_KEY"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=int(chat_id) if chat_id else None,
        tiktok_client_key=os.getenv("TIKTOK_CLIENT_KEY"),
        tiktok_client_secret=os.getenv("TIKTOK_CLIENT_SECRET"),
        tiktok_redirect_uri=os.getenv("TIKTOK_REDIRECT_URI"),
        data_dir=data_dir,
    )
