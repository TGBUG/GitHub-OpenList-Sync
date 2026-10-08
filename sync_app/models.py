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
class FileTree:
    """The result of a repository file-tree lookup.

    ``truncated`` is True when GitHub itself capped the response
    (``"truncated": true``). A truncated tree is *incomplete*, so it must never
    be used as evidence that a file was deleted upstream.
    """

    files: list[FileInfo] = field(default_factory=list)
    truncated: bool = False

    @property
    def complete(self) -> bool:
        return not self.truncated


@dataclass
class RepoInfo:
    """A repository as reported by the GitHub API.

    ``fetch_repos`` returns every repository owned by the account, including
    forks and private ones, so that the sync engine can tell "this repository
    exists but I am not syncing it" apart from "this repository is gone".
    """

    name: str
    full_name: str
    owner: str
    default_branch: str = "main"
    private: bool = False
    fork: bool = False
    updated_at: str = ""
    size: int = 0

    @classmethod
    def from_api(cls, repo: dict) -> "RepoInfo":
        owner = (repo.get("owner") or {}).get("login") or ""
        return cls(
            name=repo.get("name", ""),
            full_name=repo.get("full_name") or f"{owner}/{repo.get('name', '')}",
            owner=owner,
            default_branch=repo.get("default_branch") or "main",
            private=bool(repo.get("private", False)),
            fork=bool(repo.get("fork", False)),
            updated_at=repo.get("updated_at", ""),
            size=repo.get("size", 0) or 0,
        )


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
    last_error: Optional[str] = None
    last_warnings: list[str] = field(default_factory=list)

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
        self.last_error = None
        self.last_warnings = []

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
            "last_error": self.last_error,
            "last_warnings": self.last_warnings,
        }
