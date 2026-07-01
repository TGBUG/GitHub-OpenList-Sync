"""Core sync orchestrator: compares directories and dispatches file transfers."""

import logging
import os
import tempfile
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
        self._state_lock = threading.Lock()
        self._temp_dir = os.path.abspath("temp")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run_sync(self) -> dict:
        """Run a full sync cycle. Returns summary dict."""
        self.state.is_running = True
        self.state.reset()
        start_time = time.time()

        upload_tasks: list[SyncTask] = []

        try:
            # 1. Fetch all GitHub repos
            logger.info("Fetching repository list for user: %s", self.config.github_username)
            repos = self.github.get_repos(self.config.github_username)
            if not repos:
                logger.warning("No repositories found for user '%s'", self.config.github_username)
                self.state.last_sync_time = time.time()
                return {"repos_synced": 0, "files_uploaded": 0, "files_deleted": 0, "files_failed": 0}

            # 2. Compare each repo and build task lists
            for repo in repos:
                if self.state.stop_requested:
                    logger.info("Stop requested, aborting repo scan.")
                    break

                self._update_current_repo(repo["name"])
                repo_tasks, delete_count = self._compare_repo(repo)
                upload_tasks.extend(repo_tasks)
                with self._state_lock:
                    self.state.deleted_files += delete_count

            total = len(upload_tasks)
            with self._state_lock:
                self.state.total_files = total

            if total == 0:
                logger.info("No files need to be uploaded. All repos are in sync.")
                self.state.last_sync_time = time.time()
                return {
                    "repos_synced": len(repos),
                    "files_uploaded": 0,
                    "files_deleted": self.state.deleted_files,
                    "files_failed": 0,
                }

            # 3. Check failure rate before starting uploads
            status = self.failures.check_failure_rate()
            if status != "ok":
                logger.warning("Sync blocked by failure control: %s", status)
                self.state.last_sync_time = time.time()
                return self._build_summary(len(repos))

            # 4. Dispatch upload tasks to thread pool
            logger.info("Dispatching %d upload tasks with %d workers", total, self.config.max_threads)
            os.makedirs(self._temp_dir, exist_ok=True)

            with ThreadPoolExecutor(max_workers=self.config.max_threads) as executor:
                futures = {executor.submit(self._process_task, task): task for task in upload_tasks}
                for future in as_completed(futures):
                    if self.state.stop_requested:
                        executor.shutdown(wait=False, cancel_futures=True)
                        logger.info("Sync stopped by user request.")
                        break
                    try:
                        future.result()
                    except Exception:
                        pass  # Already handled inside _process_task

            # 5. Finalize
            self.state.last_sync_time = time.time()
            summary = self._build_summary(len(repos))
            elapsed = time.time() - start_time
            logger.info(
                "Sync complete in %.1fs: %d repos, %d uploaded, %d deleted, %d failed",
                elapsed, summary["repos_synced"], summary["files_uploaded"],
                summary["files_deleted"], summary["files_failed"],
            )
            return summary

        except Exception as e:
            logger.exception("Sync aborted with unexpected error: %s", e)
            self.state.last_sync_time = time.time()
            return self._build_summary(0)

        finally:
            self.state.is_running = False
            self._update_current_file(None)
            self._update_current_repo(None)

    # ------------------------------------------------------------------
    # Repo comparison
    # ------------------------------------------------------------------

    def _compare_repo(self, repo: dict) -> tuple[list[SyncTask], int]:
        """Compare a single repo: returns (upload_tasks, delete_count)."""
        owner, repo_name = repo["full_name"].split("/")
        branch = repo["default_branch"]
        remote_base = f"{self.config.openlist_target_directory}/{repo_name}"

        # Fetch file trees
        logger.info("Comparing repo: %s", repo_name)
        github_files = self.github.get_file_tree(owner, repo_name, branch)
        openlist_files = self.openlist.list_directory(remote_base)

        # Build index maps keyed by relative path
        gh_map: dict[str, FileInfo] = {f.path: f for f in github_files}
        ol_map: dict[str, FileInfo] = {f.path: f for f in openlist_files}

        upload_tasks: list[SyncTask] = []
        delete_count = 0

        # Ensure repo directory exists
        self.openlist.ensure_directory_path(remote_base)

        # Files to upload (new or changed)
        for path, gh_info in gh_map.items():
            if self.state.stop_requested:
                break
            ol_info = ol_map.get(path)
            if ol_info is None:
                # New file
                if self.failures.should_retry(path, repo_name):
                    upload_tasks.append(self._make_task(path, repo_name, owner, repo_name, branch, gh_info))
            elif gh_info.size != ol_info.size:
                # File changed (different size)
                if self.failures.should_retry(path, repo_name):
                    upload_tasks.append(self._make_task(path, repo_name, owner, repo_name, branch, gh_info))

        # Files to delete (mirror mode: exists on OpenList but not on GitHub)
        if self.config.mirror_delete:
            to_delete = [path for path in ol_map if path not in gh_map]
            if to_delete:
                logger.info("Deleting %d file(s) from %s (mirror mode)", len(to_delete), repo_name)
                self._batch_delete(remote_base, to_delete)
                delete_count = len(to_delete)

        return upload_tasks, delete_count

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
            retry_count=self.failures.get_retry_count(file_path, repo_name),
        )

    # ------------------------------------------------------------------
    # Individual task processing (runs in thread pool)
    # ------------------------------------------------------------------

    def _process_task(self, task: SyncTask):
        """Process a single file sync task: download → upload → cleanup."""
        if self.state.stop_requested:
            task.status = TaskStatus.SKIPPED
            return

        task.status = TaskStatus.IN_PROGRESS
        self._update_current_file(task.file_path)

        retries_left = self.config.max_retries - task.retry_count
        for attempt in range(retries_left):
            if self.state.stop_requested:
                task.status = TaskStatus.SKIPPED
                return

            # Check failure rate before each attempt
            status = self.failures.check_failure_rate()
            if status != "ok":
                wait = self.failures.get_cooldown_remaining()
                logger.warning("Blocked by failure control (%s). Skipping %s", status, task.file_path)
                task.status = TaskStatus.SKIPPED
                return

            try:
                # Download
                local_path = self._download_file(task)
                if not local_path:
                    raise RuntimeError(f"Download failed for {task.file_path}")

                # Upload
                remote_path = f"{self.config.openlist_target_directory}/{task.repo_name}/{task.file_path}"
                self.openlist.ensure_directory_path(os.path.dirname(remote_path))
                success = self.openlist.upload_file(local_path, remote_path)
                if not success:
                    raise RuntimeError(f"Upload failed for {task.file_path}")

                # Cleanup
                self._cleanup_temp(local_path)

                # Success
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
                    # All retries exhausted
                    task.status = TaskStatus.FAILED
                    task.error_message = str(e)
                    self.failures.record_failure(task.file_path, task.repo_name, str(e))
                    with self._state_lock:
                        self.state.failed_files += 1

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
            # Clean up empty parent dirs up to temp root
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

    def _build_summary(self, repos_synced: int) -> dict:
        with self._state_lock:
            return {
                "repos_synced": repos_synced,
                "files_uploaded": self.state.completed_files,
                "files_deleted": self.state.deleted_files,
                "files_failed": self.state.failed_files,
            }
