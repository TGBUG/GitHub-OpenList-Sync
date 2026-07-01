"""Failure tracking, retry logic, and rate/cooldown management."""

import json
import logging
import os
import threading
import time
from typing import Optional

from sync_app.models import FailureRecord

logger = logging.getLogger("github_sync")


class FailureManager:
    def __init__(
        self,
        data_file: str = "data/failures.json",
        max_retries: int = 3,
        max_failures_per_minute: int = 10,
        cooldown_minutes: int = 5,
    ):
        self._data_file = data_file
        self.max_retries = max_retries
        self.max_failures_per_minute = max_failures_per_minute
        self.cooldown_minutes = cooldown_minutes

        self._lock = threading.Lock()
        self._records: list[FailureRecord] = []
        self._failure_timestamps: list[float] = []  # For rate calculation
        self._consecutive_failures: int = 0
        self._cooldown_until: float = 0
        self._paused: bool = False

        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self):
        os.makedirs(os.path.dirname(self._data_file), exist_ok=True)
        if os.path.exists(self._data_file):
            try:
                with open(self._data_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self._records = [FailureRecord.from_dict(r) for r in raw]
                logger.info("Loaded %d failure record(s)", len(self._records))
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Failed to load failures.json: %s. Starting fresh.", e)
                self._records = []

    def _save(self):
        os.makedirs(os.path.dirname(self._data_file), exist_ok=True)
        with open(self._data_file, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in self._records], f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Failure recording
    # ------------------------------------------------------------------

    def record_failure(self, file_path: str, repo_name: str, error_message: str):
        """Record a file sync failure."""
        now = time.time()
        with self._lock:
            # Check if there's an existing record for this file
            existing = self._find_record(file_path, repo_name)
            if existing:
                existing.timestamp = now
                existing.retry_count += 1
                existing.error_message = error_message
            else:
                record = FailureRecord(
                    timestamp=now,
                    file_path=file_path,
                    repo_name=repo_name,
                    error_message=error_message,
                    retry_count=1,
                )
                self._records.append(record)

            self._failure_timestamps.append(now)
            self._consecutive_failures += 1
            self._save()

    def record_success(self):
        """Reset consecutive failure counter on a successful operation."""
        with self._lock:
            self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # Retry logic
    # ------------------------------------------------------------------

    def should_retry(self, file_path: str, repo_name: str) -> bool:
        """Check whether a file should be retried based on past failure records."""
        with self._lock:
            record = self._find_record(file_path, repo_name)
            if record is None:
                return True  # No previous failure, can try
            if record.retry_count < self.max_retries:
                return True
            return False

    def get_retry_count(self, file_path: str, repo_name: str) -> int:
        """Get the number of previous retries for a file."""
        with self._lock:
            record = self._find_record(file_path, repo_name)
            return record.retry_count if record else 0

    def clear_failure(self, file_path: str, repo_name: str):
        """Remove a failure record after successful retry."""
        with self._lock:
            self._records = [
                r for r in self._records
                if not (r.file_path == file_path and r.repo_name == repo_name)
            ]
            self._save()

    def clear_all_failures(self):
        """Reset all failure records."""
        with self._lock:
            self._records.clear()
            self._failure_timestamps.clear()
            self._consecutive_failures = 0
            self._cooldown_until = 0
            self._paused = False
            self._save()

    # ------------------------------------------------------------------
    # Failure rate & cooldown
    # ------------------------------------------------------------------

    def check_failure_rate(self) -> str:
        """
        Check if failure rate exceeds threshold.
        Returns 'ok', 'paused', or 'cooldown'.
        """
        with self._lock:
            now = time.time()

            # Cooldown check
            if self._cooldown_until > now:
                remaining = int(self._cooldown_until - now)
                return "cooldown"
            elif self._cooldown_until > 0 and self._cooldown_until <= now:
                logger.info("Cooldown period ended, resuming sync.")
                self._cooldown_until = 0
                self._paused = False
                self._consecutive_failures = 0

            if self._paused:
                return "paused"

            # Clean old timestamps (keep only last 60s)
            cutoff = now - 60
            self._failure_timestamps = [t for t in self._failure_timestamps if t > cutoff]

            # Check rate
            rate = len(self._failure_timestamps)
            if rate > self.max_failures_per_minute:
                self._paused = True
                logger.warning(
                    "Failure rate %d/min exceeds threshold %d. Pausing sync.",
                    rate, self.max_failures_per_minute,
                )
                return "paused"

            # Check consecutive failures for cooldown
            if self._consecutive_failures >= 10:
                self._cooldown_until = now + (self.cooldown_minutes * 60)
                self._paused = True
                logger.warning(
                    "Consecutive failures reached %d. Entering cooldown for %d minutes.",
                    self._consecutive_failures, self.cooldown_minutes,
                )
                return "cooldown"

            return "ok"

    def get_cooldown_remaining(self) -> int:
        """Return seconds remaining in cooldown, or 0."""
        with self._lock:
            remaining = int(self._cooldown_until - time.time())
            return max(remaining, 0)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_all_failures(self) -> list[dict]:
        """Return all failure records as dicts."""
        with self._lock:
            return [r.to_dict() for r in self._records]

    def _find_record(self, file_path: str, repo_name: str) -> Optional[FailureRecord]:
        for r in self._records:
            if r.file_path == file_path and r.repo_name == repo_name:
                return r
        return None
