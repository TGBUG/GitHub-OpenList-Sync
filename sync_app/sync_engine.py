"""Core sync orchestrator: compares directories and dispatches file transfers."""

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from sync_app.config import Config
from sync_app.failure_manager import FailureManager
from sync_app.github_client import GitHubClient
from sync_app.models import FileInfo, SyncState, SyncTask, TaskStatus
from sync_app.openlist_client import OpenListClient

logger = logging.getLogger("github_sync")

MANIFEST_FILE = os.path.join("data", "sync_manifest.json")


class SyncManifest:
    """Local manifest recording the last-synced state of every file per repo.

    Format::

        {"owner/repo": {"branch": "main", "files": {"path": {"sha": "abc123"}, ...}}}
    """

    def __init__(self, path: str = MANIFEST_FILE):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, KeyError):
                self._data = {}

    def _save(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    # -- repo-level keys use "owner/repo" as the canonical form ---------------

    def _repo_key(self, owner: str, repo: str) -> str:
        return f"{owner}/{repo}"

    # -- query ----------------------------------------------------------------

    def get_entry(self, owner: str, repo: str) -> dict | None:
        with self._lock:
            return self._data.get(self._repo_key(owner, repo))

    def get_files(self, owner: str, repo: str) -> dict[str, dict]:
        """Return {path: {sha, ...}} for a repo, or empty dict."""
        entry = self.get_entry(owner, repo)
        if entry:
            return entry.get("files", {})
        return {}

    # -- update ---------------------------------------------------------------

    def set_file(self, owner: str, repo: str, file_path: str, sha: str):
        with self._lock:
            key = self._repo_key(owner, repo)
            if key not in self._data:
                self._data[key] = {"branch": "", "files": {}}
            self._data[key]["files"][file_path] = {"sha": sha}
            self._save()

    def remove_file(self, owner: str, repo: str, file_path: str):
        with self._lock:
            key = self._repo_key(owner, repo)
            if key in self._data:
                self._data[key]["files"].pop(file_path, None)
                if not self._data[key]["files"]:
                    del self._data[key]
                self._save()

    def set_branch(self, owner: str, repo: str, branch: str):
        with self._lock:
            key = self._repo_key(owner, repo)
            if key not in self._data:
                self._data[key] = {"branch": branch, "files": {}}
            else:
                self._data[key]["branch"] = branch
            self._save()

    def remove_repo(self, owner: str, repo: str):
        with self._lock:
            self._data.pop(self._repo_key(owner, repo), None)
            self._save()


class SyncEngine:
    def __init__(
        self,
        config: Config,
        github_client: GitHubClient,
        openlist_client: OpenListClient,
        failure_manager: FailureManager,
        sync_state: SyncState,
    ):
        self.config = config
        self.github = github_client
        self.openlist = openlist_client
        self.failures = failure_manager
        self.state = sync_state
        self.manifest = SyncManifest()
        self._state_lock = threading.Lock()
        self._temp_dir = os.path.abspath("temp")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run_sync(self) -> dict:
        """Run a full sync cycle for all configured users. Returns summary dict."""
        self.state.is_running = True
        self.state.reset()
        start_time = time.time()

        usernames = self.config.github_usernames
        if not usernames:
            logger.warning("No GitHub usernames configured.")
            self.state.last_sync_time = time.time()
            self.state.is_running = False
            return {"repos_synced": 0, "files_uploaded": 0, "files_deleted": 0, "files_failed": 0}

        include_private = self.config.sync_private_repos
        if include_private and not self.config.github_token:
            logger.warning(
                "sync_private_repos=true but no GitHub token configured. "
                "Private repos require authentication. Skipping private repos."
            )
            include_private = False

        total_repos = 0
        total_deleted = 0

        try:
            for username in usernames:
                if self.state.stop_requested:
                    logger.info("Stop requested, aborting sync.")
                    break

                self._update_current_user(username)
                logger.info("=== Syncing user: %s ===", username)

                user_summary = self._sync_user(username, include_private)
                total_repos += user_summary["repos_synced"]
                total_deleted += user_summary["files_deleted"]

            self.state.last_sync_time = time.time()

            with self._state_lock:
                total_uploaded = self.state.completed_files
                total_failed = self.state.failed_files

            elapsed = time.time() - start_time
            logger.info(
                "Sync complete in %.1fs: %d repos across %d user(s), %d uploaded, %d deleted, %d failed",
                elapsed, total_repos, len(usernames), total_uploaded, total_deleted, total_failed,
            )
            return {
                "repos_synced": total_repos,
                "files_uploaded": total_uploaded,
                "files_deleted": total_deleted,
                "files_failed": total_failed,
            }

        except Exception as e:
            logger.exception("Sync aborted with unexpected error: %s", e)
            self.state.last_sync_time = time.time()
            with self._state_lock:
                return {
                    "repos_synced": total_repos,
                    "files_uploaded": self.state.completed_files,
                    "files_deleted": total_deleted,
                    "files_failed": self.state.failed_files,
                }

        finally:
            self.state.is_running = False
            self._update_current_file(None)
            self._update_current_repo(None)
            self._update_current_user(None)

    # ------------------------------------------------------------------
    # Per-user sync
    # ------------------------------------------------------------------

    def _sync_user(self, username: str, include_private: bool) -> dict:
        """Sync all repos for a single GitHub user."""
        logger.info("Fetching repository list for user: %s", username)
        repos = self.github.get_repos(username, include_private=include_private)
        if not repos:
            logger.warning("No repositories found for user '%s'", username)
            return {"repos_synced": 0, "files_uploaded": 0, "files_deleted": 0, "files_failed": 0}

        filtered_repos = []
        skipped_repos = []
        for repo in repos:
            full_name = repo["full_name"]
            if self.config.is_repo_allowed(full_name):
                filtered_repos.append(repo)
            else:
                skipped_repos.append(full_name)

        if skipped_repos:
            logger.info("Filter (%s): skipped %d repo(s) for user %s: %s",
                        self.config.filter_mode, len(skipped_repos), username, skipped_repos)

        if not filtered_repos:
            logger.warning("All repos filtered out for user '%s'", username)
            return {"repos_synced": 0, "files_uploaded": 0, "files_deleted": 0, "files_failed": 0}

        upload_tasks: list[SyncTask] = []
        total_deleted = 0

        for repo in filtered_repos:
            if self.state.stop_requested:
                logger.info("Stop requested, aborting repo scan.")
                break

            self._update_current_repo(repo["name"])
            repo_tasks, delete_count = self._compare_repo(repo, username)
            upload_tasks.extend(repo_tasks)
            total_deleted += delete_count

        with self._state_lock:
            self.state.deleted_files += total_deleted

        total = len(upload_tasks)
        with self._state_lock:
            self.state.total_files += total

        if total == 0:
            logger.info("No files need to be uploaded for user %s. All repos are in sync.", username)
            return {"repos_synced": len(filtered_repos), "files_uploaded": 0,
                    "files_deleted": total_deleted, "files_failed": 0}

        status = self.failures.check_failure_rate()
        if status != "ok":
            logger.warning("Sync blocked by failure control: %s", status)
            return {"repos_synced": len(filtered_repos), "files_uploaded": 0,
                    "files_deleted": total_deleted, "files_failed": 0}

        logger.info("Dispatching %d upload tasks with %d workers", total, self.config.max_threads)
        os.makedirs(self._temp_dir, exist_ok=True)

        # Track per-user starting counters
        with self._state_lock:
            uploaded_before = self.state.completed_files
            failed_before = self.state.failed_files

        with ThreadPoolExecutor(max_workers=self.config.max_threads) as executor:
            futures = {executor.submit(self._process_task, task, username): task for task in upload_tasks}
            for future in as_completed(futures):
                if self.state.stop_requested:
                    executor.shutdown(wait=False, cancel_futures=True)
                    logger.info("Sync stopped by user request.")
                    break
                try:
                    future.result()
                except Exception:
                    pass

        with self._state_lock:
            user_uploaded = self.state.completed_files - uploaded_before
            user_failed = self.state.failed_files - failed_before

        return {"repos_synced": len(filtered_repos), "files_uploaded": user_uploaded,
                "files_deleted": total_deleted, "files_failed": user_failed}

    # ------------------------------------------------------------------
    # Repo comparison  (uses local manifest for upload decisions,
    #                    OpenList listing only for mirror-delete detection)
    # ------------------------------------------------------------------

    def _compare_repo(self, repo: dict, username: str) -> tuple[list[SyncTask], int]:
        """Compare a single repo against the local sync manifest.

        Returns (upload_tasks, delete_count).
        """
        owner = repo.get("owner", username)
        repo_name = repo["name"]
        branch = repo["default_branch"]
        remote_base = f"{self.config.openlist_target_directory}/{username}/{repo_name}"

        logger.info("Comparing repo: %s/%s", username, repo_name)

        github_files = self.github.get_file_tree(owner, repo_name, branch)
        gh_map: dict[str, FileInfo] = {f.path: f for f in github_files}

        # Compare against local manifest (not OpenList)
        manifest_files = self.manifest.get_files(owner, repo_name)

        upload_tasks: list[SyncTask] = []
        delete_count = 0

        self.openlist.ensure_directory_path(remote_base)
        self.manifest.set_branch(owner, repo_name, branch)

        for path, gh_info in gh_map.items():
            if self.state.stop_requested:
                break
            mf_entry = manifest_files.get(path)
            if mf_entry is None:
                # New file (not in manifest)
                if self.failures.should_retry(path, repo_name):
                    upload_tasks.append(self._make_task(path, repo_name, owner, repo_name, branch, gh_info))
            elif not self._manifest_matches(gh_info, mf_entry):
                # File changed since last sync
                if self.failures.should_retry(path, repo_name):
                    upload_tasks.append(self._make_task(path, repo_name, owner, repo_name, branch, gh_info))

        # Mirror delete: remove manifest entries that no longer exist on GitHub
        if self.config.mirror_delete:
            stale_paths = [path for path in manifest_files if path not in gh_map]
            if stale_paths:
                logger.info("Deleting %d stale file(s) from %s/%s (mirror mode)",
                            len(stale_paths), username, repo_name)
                self._batch_delete(remote_base, stale_paths)
                for path in stale_paths:
                    self.manifest.remove_file(owner, repo_name, path)
                delete_count = len(stale_paths)

        return upload_tasks, delete_count

    @staticmethod
    def _manifest_matches(gh_info: FileInfo, mf_entry: dict) -> bool:
        """Return True if the GitHub file matches the manifest entry.

        Uses SHA when available (precise); falls back to size for old manifest entries.
        """
        mf_sha = mf_entry.get("sha")
        return gh_info.sha == mf_sha

    def _batch_delete(self, remote_base: str, paths: list[str]):
        """Delete files in batches grouped by parent directory."""
        groups: dict[str, list[str]] = {}
        for path in paths:
            parent = os.path.dirname(path) or "/"
            name = os.path.basename(path)
            full_parent = f"{remote_base}/{parent}".rstrip("/")
            if full_parent not in groups:
                groups[full_parent] = []
            groups[full_parent].append(name)

        for directory, names in groups.items():
            if self.state.stop_requested:
                break
            self.openlist.remove_files(directory, names)

    def _make_task(
        self, file_path: str, repo_name: str, owner: str, repo: str, branch: str, gh_info: FileInfo,
    ) -> SyncTask:
        return SyncTask(
            file_path=file_path,
            repo_name=repo_name,
            github_download_url=f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}",
            file_size=gh_info.size,
            sha=gh_info.sha,
            retry_count=self.failures.get_retry_count(file_path, repo_name),
        )

    # ------------------------------------------------------------------
    # Individual task processing (runs in thread pool)
    # ------------------------------------------------------------------

    def _process_task(self, task: SyncTask, username: str):
        """Process a single file sync task: download -> upload -> update manifest."""
        if self.state.stop_requested:
            task.status = TaskStatus.SKIPPED
            return

        task.status = TaskStatus.IN_PROGRESS
        self._update_current_file(task.file_path)
        self._update_current_repo(task.repo_name)

        retries_left = self.config.max_retries - task.retry_count
        for attempt in range(retries_left):
            if self.state.stop_requested:
                task.status = TaskStatus.SKIPPED
                return

            status = self.failures.check_failure_rate()
            if status != "ok":
                logger.warning("Blocked by failure control (%s). Skipping %s", status, task.file_path)
                task.status = TaskStatus.SKIPPED
                return

            try:
                local_path = self._download_file(task)
                if not local_path:
                    raise RuntimeError(f"Download failed for {task.file_path}")

                remote_path = f"{self.config.openlist_target_directory}/{username}/{task.repo_name}/{task.file_path}"
                self.openlist.ensure_directory_path(os.path.dirname(remote_path))
                success = self.openlist.upload_file(local_path, remote_path)
                if not success:
                    raise RuntimeError(f"Upload failed for {task.file_path}")

                self._cleanup_temp(local_path)

                # Update manifest so future comparisons skip this file
                self._update_manifest_from_task(task)

                task.status = TaskStatus.COMPLETED
                self.failures.clear_failure(task.file_path, task.repo_name)
                self.failures.record_success()
                with self._state_lock:
                    self.state.completed_files += 1
                return

            except Exception as e:
                error_msg = f"[Attempt {attempt + 1}/{retries_left} after {task.retry_count} prior] {e}"
                logger.warning("Task failed: %s", error_msg)
                task.retry_count += 1

                if attempt < retries_left - 1:
                    wait = self.config.retry_interval_seconds
                    logger.info("Retrying %s in %ds...", task.file_path, wait)
                    time.sleep(wait)
                else:
                    task.status = TaskStatus.FAILED
                    task.error_message = str(e)
                    self.failures.record_failure(task.file_path, task.repo_name, str(e))
                    with self._state_lock:
                        self.state.failed_files += 1

    def _update_manifest_from_task(self, task: SyncTask):
        """Extract owner/repo from the task download URL and update the manifest."""
        parts = task.github_download_url.replace("https://raw.githubusercontent.com/", "").split("/", 2)
        if len(parts) >= 3:
            owner = parts[0]
            repo = parts[1]
            self.manifest.set_file(owner, repo, task.file_path, task.sha)

    def _download_file(self, task: SyncTask) -> Optional[str]:
        """Download a file to a temp location. Returns the local path or None."""
        parts = task.github_download_url.replace("https://raw.githubusercontent.com/", "").split("/", 2)
        if len(parts) >= 3:
            owner, repo, rest = parts[0], parts[1], parts[2]
            branch_and_path = rest.split("/", 1)
            branch = branch_and_path[0]
        else:
            owner, repo, branch = "", "", "main"

        local_path = os.path.join(self._temp_dir, task.repo_name, task.file_path)
        success = self.github.download_file(owner, repo, branch, task.file_path, local_path)
        return local_path if success else None

    @staticmethod
    def _cleanup_temp(path: str):
        """Remove a temp file and its parent directories if empty."""
        try:
            if os.path.exists(path):
                os.remove(path)
            parent = os.path.dirname(path)
            while parent and os.path.exists(parent) and parent != os.path.abspath("temp"):
                try:
                    os.rmdir(parent)
                    parent = os.path.dirname(parent)
                except OSError:
                    break
        except OSError:
            pass

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _update_current_file(self, path: Optional[str]):
        with self._state_lock:
            self.state.current_file = path

    def _update_current_repo(self, repo: Optional[str]):
        with self._state_lock:
            self.state.current_repo = repo

    def _update_current_user(self, user: Optional[str]):
        with self._state_lock:
            self.state.current_user = user
