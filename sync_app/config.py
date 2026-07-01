"""Configuration loader and validator."""

import os
from typing import Any, Optional

import yaml


class Config:
    def __init__(self, config_path: str = "config.yaml"):
        self._config_path = config_path
        self._data: dict[str, Any] = {}
        self.load()

    def load(self):
        if not os.path.exists(self._config_path):
            raise FileNotFoundError(f"Config file not found: {self._config_path}")
        with open(self._config_path, "r", encoding="utf-8") as f:
            self._data = yaml.safe_load(f) or {}
        self._validate()

    def _validate(self):
        required_sections = ["github", "openlist", "sync", "web"]
        for section in required_sections:
            if section not in self._data:
                raise ValueError(f"Missing required config section: [{section}]")

        sync = self._data["sync"]
        required_sync = [
            "interval_hours", "max_threads", "max_retries",
            "retry_interval_seconds", "max_failures_per_minute", "cooldown_minutes",
        ]
        for key in required_sync:
            if key not in sync:
                raise ValueError(f"Missing required sync config: sync.{key}")

        for key in ("max_threads", "max_retries"):
            if sync[key] < 1:
                raise ValueError(f"sync.{key} must be >= 1")
        for key in ("retry_interval_seconds", "max_failures_per_minute", "cooldown_minutes"):
            if sync[key] < 0:
                raise ValueError(f"sync.{key} must be >= 0")

    def save(self):
        with open(self._config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._data, f, default_flow_style=False, allow_unicode=True)

    # -- GitHub --
    @property
    def github_username(self) -> str:
        return self._data["github"].get("username", "")

    @property
    def github_token(self) -> Optional[str]:
        token = self._data["github"].get("token", "")
        return token if token else None

    # -- OpenList --
    @property
    def openlist_base_url(self) -> str:
        return self._data["openlist"].get("base_url", "").rstrip("/")

    @property
    def openlist_username(self) -> str:
        return self._data["openlist"].get("username", "")

    @property
    def openlist_password(self) -> str:
        return self._data["openlist"].get("password", "")

    @property
    def openlist_target_directory(self) -> str:
        return self._data["openlist"].get("target_directory", "/github-sync")

    # -- Sync --
    @property
    def sync_interval_hours(self) -> int:
        return self._data["sync"]["interval_hours"]

    @property
    def max_threads(self) -> int:
        return self._data["sync"]["max_threads"]

    @property
    def max_retries(self) -> int:
        return self._data["sync"]["max_retries"]

    @property
    def retry_interval_seconds(self) -> int:
        return self._data["sync"]["retry_interval_seconds"]

    @property
    def max_failures_per_minute(self) -> int:
        return self._data["sync"]["max_failures_per_minute"]

    @property
    def cooldown_minutes(self) -> int:
        return self._data["sync"]["cooldown_minutes"]

    @property
    def mirror_delete(self) -> bool:
        return self._data["sync"].get("mirror_delete", True)

    # -- Web --
    @property
    def web_host(self) -> str:
        return self._data["web"].get("host", "0.0.0.0")

    @property
    def web_port(self) -> int:
        return self._data["web"].get("port", 8080)
