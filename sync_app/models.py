"""Data models for sync state, tasks, and failure records."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class FileInfo:
    path: str
    size: int
    sha: str = ""
    is_dir: bool = False


@dataclass
class SyncTask:
    file_path: str
    repo_name: str
    github_download_url: str
    file_size: int
    sha: str = ""
    status: TaskStatus = TaskStatus.PENDING
    error_message: Optional[str] = None
    retry_count: int = 0


@dataclass
class FailureRecord:
    timestamp: float
    file_path: str
    repo_name: str
    error_message: str
    retry_count: int

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "file_path": self.file_path,
            "repo_name": self.repo_name,
            "error_message": self.error_message,
            "retry_count": self.retry_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FailureRecord":
        return cls(
            timestamp=d["timestamp"],
            file_path=d["file_path"],
            repo_name=d["repo_name"],
            error_message=d["error_message"],
            retry_count=d["retry_count"],
        )


@dataclass
class SyncState:
    """Shared state between sync engine and web dashboard."""
    is_running: bool = False
    current_file: Optional[str] = None
    total_files: int = 0
    completed_files: int = 0
    failed_files: int = 0
    deleted_files: int = 0
    last_sync_time: Optional[float] = None
    stop_requested: bool = False
    current_repo: Optional[str] = None
    current_user: Optional[str] = None

    @property
    def progress_pct(self) -> float:
        if self.total_files == 0:
            return 0.0
        return round((self.completed_files / self.total_files) * 100, 1)

    def reset(self):
        self.current_file = None
        self.total_files = 0
        self.completed_files = 0
        self.failed_files = 0
        self.deleted_files = 0
        self.stop_requested = False
        self.current_repo = None
        self.current_user = None

    def to_dict(self) -> dict:
        return {
            "is_running": self.is_running,
            "current_file": self.current_file,
            "current_repo": self.current_repo,
            "current_user": self.current_user,
            "total_files": self.total_files,
            "completed_files": self.completed_files,
            "failed_files": self.failed_files,
            "deleted_files": self.deleted_files,
            "last_sync_time": self.last_sync_time,
            "progress_pct": self.progress_pct,
            "stop_requested": self.stop_requested,
        }
