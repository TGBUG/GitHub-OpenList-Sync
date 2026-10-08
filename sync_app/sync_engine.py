"""Core sync orchestrator: compares directories and dispatches file transfers.

Safety model (see docs/adr/0001-fail-closed-mirror-delete.md):

    mirror-delete acts only on *positive evidence of absence* - a complete,
    successful GitHub listing that does not contain the item.

Failed, partial (mid-pagination), truncated, or locally-filtered views are
"unknown", not "gone", and therefore never delete anything.
"""

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from sync_app.config import Config
from sync_app.failure_manager import FailureManager
from sync_app.github_client import GitHubAPIError, GitHubClient
from sync_app.models import FileInfo, RepoInfo, SyncState, SyncTask, TaskStatus
from sync_app.openlist_client import OpenListClient

logger = logging.getLogger("github_sync")

MANIFEST_FILE = os.path.join("data", "sync_manifest.json")


class SyncManifest:
    """Local manifest recording the last-synced state of every file per repo.

    Format::

        {"owner/repo": {"branch": "main", "private": false,
                        "files": {"path": {"sha": "abc123"}, ...}}}

    Repo keys are matched case-insensitively: GitHub logins are
    case-insensitive, so ``TGBUG/repo`` and ``tgbug/repo`` must resolve to the
    same entry.
    """

    def __init__(self, path: str = MANIFEST_FILE):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self):
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, KeyError):
                self._data = {}

    def _save(self):
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    # -- repo-level keys use "owner/repo" as the canonical form ---------------

    def _repo_key(self, owner: str, repo: str) -> str:
        return f"{owner}/{repo}"

    def _resolve_key(self, owner: str, repo: str) -> str:
        """Return the existing key for owner/repo (any casing), else a new one."""
        key = self._repo_key(owner, repo)
        if key in self._data:
            return key
        folded = key.casefold()
        for existing in self._data:
            if existing.casefold() == folded:
                return existing
        return key

    # -- query ----------------------------------------------------------------

    def get_entry(self, owner: str, repo: str) -> dict | None:
        with self._lock:
            return self._data.get(self._resolve_key(owner, repo))

    def get_files(self, owner: str, repo: str) -> dict[str, dict]:
        """Return {path: {sha, ...}} for a repo, or empty dict."""
        entry = self.get_entry(owner, repo)
        if entry:
            return entry.get("files", {})
        return {}

    # -- update ---------------------------------------------------------------

    def set_file(self, owner: str, repo: str, file_path: str, sha: str):
        with self._lock:
            key = self._resolve_key(owner, repo)
            if key not in self._data:
                self._data[key] = {"branch": "", "files": {}}
            self._data[key].setdefault("files", {})[file_path] = {"sha": sha}
            self._save()

    def remove_file(self, owner: str, repo: str, file_path: str):
        with self._lock:
            key = self._resolve_key(owner, repo)
            if key in self._data:
                self._data[key].get("files", {}).pop(file_path, None)
                if not self._data[key].get("files"):
                    del self._data[key]
                self._save()

    def set_branch(self, owner: str, repo: str, branch: str, private: Optional[bool] = None):
        """Record the branch (and, when known, the visibility) of a repo."""
        with self._lock:
            key = self._resolve_key(owner, repo)
            entry = self._data.get(key)
            if entry is None:
                entry = {"branch": branch, "files": {}}
                self._data[key] = entry
            entry["branch"] = branch
            if private is not None:
                entry["private"] = bool(private)
            self._save()

    def remove_repo(self, owner: str, repo: str):
        with self._lock:
            self._data.pop(self._resolve_key(owner, repo), None)
            self._save()

    def list_repos(self, owner: str) -> list[str]:
        """Return repo names tracked in the manifest for a given owner."""
        target = owner.casefold()
        with self._lock:
            names = []
            for key in self._data:
                head, sep, tail = key.partition("/")
                if sep and tail and head.casefold() == target:
                    names.append(tail)
            return names


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
            return {"repos_synced": 0, "files_uploaded": 0, "files_deleted": 0,
                    "files_failed": 0, "errors": [], "warnings": []}

        include_private = self.config.sync_private_repos
        if include_private and not self.config.github_token:
            logger.warning(
                "sync_private_repos=true but no GitHub token configured. "
                "Private repos require authentication. Skipping private repos."
            )
            include_private = False

        total_repos = 0
        total_deleted = 0
        errors: list[str] = []
        warnings: list[str] = []

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
                warnings.extend(user_summary.get("warnings") or [])
                if user_summary.get("error"):
                    errors.append(f"{username}: {user_summary['error']}")

            if getattr(self.github, "token_rejected", False):
                errors.append(
                    "github.token was rejected by GitHub (401 Bad credentials); this cycle only saw "
                    "public data. Update the token in config.yaml."
                )

            self.state.last_sync_time = time.time()

            with self._state_lock:
                total_uploaded = self.state.completed_files
                total_failed = self.state.failed_files
                self.state.last_error = " | ".join(errors) if errors else None
                self.state.last_warnings = list(warnings)

            elapsed = time.time() - start_time
            logger.info(
                "Sync complete in %.1fs: %d repos across %d user(s), %d uploaded, %d deleted, %d failed",
                elapsed, total_repos, len(usernames), total_uploaded, total_deleted, total_failed,
            )
            for warning in warnings:
                logger.warning("Sync warning: %s", warning)
            if errors:
                logger.error("Sync finished with %d error(s):", len(errors))
                for error in errors:
                    logger.error("  - %s", error)
                logger.error("No data was deleted because of these errors.")

            return {
                "repos_synced": total_repos,
                "files_uploaded": total_uploaded,
                "files_deleted": total_deleted,
                "files_failed": total_failed,
                "errors": errors,
                "warnings": warnings,
            }

        except Exception as e:
            logger.exception("Sync aborted with unexpected error: %s", e)
            self.state.last_sync_time = time.time()
            errors.append(f"unexpected error: {e}")
            with self._state_lock:
                self.state.last_error = " | ".join(errors)
                self.state.last_warnings = list(warnings)
                return {
                    "repos_synced": total_repos,
                    "files_uploaded": self.state.completed_files,
                    "files_deleted": total_deleted,
                    "files_failed": self.state.failed_files,
                    "errors": errors,
                    "warnings": warnings,
                }

        finally:
            self.state.is_running = False
            self._update_current_file(None)
            self._update_current_repo(None)
            self._update_current_user(None)

    # ------------------------------------------------------------------
    # Per-user sync
    # ------------------------------------------------------------------

    @staticmethod
    def _result(repos_synced=0, files_uploaded=0, files_deleted=0, files_failed=0,
                error=None, warnings=None) -> dict:
        return {
            "repos_synced": repos_synced,
            "files_uploaded": files_uploaded,
            "files_deleted": files_deleted,
            "files_failed": files_failed,
            "error": error,
            "warnings": warnings or [],
        }

    def _sync_user(self, username: str, include_private: bool) -> dict:
        """Sync all repos for a single GitHub user."""
        logger.info("Fetching repository list for user: %s", username)
        try:
            all_repos = self.github.fetch_repos(
                username,
                allow_unauthenticated_fallback=self.config.allow_unauthenticated_fallback,
            )
        except GitHubAPIError as e:
            logger.error(
                "Could not list the repositories of '%s': %s", username, e
            )
            logger.error(
                "Skipping user '%s' for this cycle. Nothing will be deleted from OpenList for "
                "this account: an unreadable repository list is not evidence of deletion.",
                username,
            )
            return self._result(error=f"repository list unavailable ({e})")

        # A repo is only "gone" if a successful, complete listing omits it.
        # Forks, private repos and filtered repos are still part of this set,
        # so local configuration can never delete remote data.
        present_repo_names = {r.name.casefold() for r in all_repos}

        syncable_repos: list[RepoInfo] = []
        skipped_visibility: list[str] = []
        skipped_filter: list[str] = []
        for repo in all_repos:
            if repo.fork:
                skipped_visibility.append(f"{repo.full_name} (fork)")
                continue
            if repo.private and not include_private:
                skipped_visibility.append(f"{repo.full_name} (private)")
                continue
            if not self.config.is_repo_allowed(repo.full_name):
                skipped_filter.append(repo.full_name)
                continue
            syncable_repos.append(repo)

        if skipped_visibility:
            logger.info(
                "Not syncing %d repo(s) for user %s: %s",
                len(skipped_visibility), username, skipped_visibility,
            )
        if skipped_filter:
            logger.info(
                "Filter (%s): skipped %d repo(s) for user %s: %s",
                self.config.filter_mode, len(skipped_filter), username, skipped_filter,
            )

        warnings: list[str] = []
        total_deleted = 0

        # Clean up repos that exist in the manifest but are no longer on GitHub
        if self.config.mirror_delete:
            deleted, cleanup_warnings = self._cleanup_stale_repos(
                username, present_repo_names, include_private=include_private
            )
            total_deleted += deleted
            warnings.extend(cleanup_warnings)

        if not syncable_repos:
            logger.warning(
                "No syncable repositories for user '%s' (none returned, or all were forks, "
                "private, or filtered out).", username,
            )
            with self._state_lock:
                self.state.deleted_files += total_deleted
            return self._result(files_deleted=total_deleted, warnings=warnings)

        upload_tasks: list[SyncTask] = []
        for repo in syncable_repos:
            if self.state.stop_requested:
                logger.info("Stop requested, aborting repo scan.")
                break

            self._update_current_repo(repo.name)
            repo_tasks, delete_count, repo_warnings = self._compare_repo(repo, username)
            upload_tasks.extend(repo_tasks)
            total_deleted += delete_count
            warnings.extend(repo_warnings)

        with self._state_lock:
            self.state.deleted_files += total_deleted

        total = len(upload_tasks)
        with self._state_lock:
            self.state.total_files += total

        if total == 0:
            logger.info("No files need to be uploaded for user %s. All repos are in sync.", username)
            return self._result(
                repos_synced=len(syncable_repos), files_deleted=total_deleted, warnings=warnings,
            )

        status = self.failures.check_failure_rate()
        if status != "ok":
            logger.warning("Sync blocked by failure control: %s", status)
            return self._result(
                repos_synced=len(syncable_repos), files_deleted=total_deleted, warnings=warnings,
            )

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

        return self._result(
            repos_synced=len(syncable_repos),
            files_uploaded=user_uploaded,
            files_deleted=total_deleted,
            files_failed=user_failed,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Repo comparison  (uses local manifest for upload decisions,
    #                    GitHub listings for mirror-delete evidence)
    # ------------------------------------------------------------------

    def _compare_repo(self, repo: RepoInfo, username: str) -> tuple[list[SyncTask], int, list[str]]:
        """Compare a single repo against the local sync manifest.

        Returns (upload_tasks, delete_count, warnings).
        """
        owner = repo.owner or username
        repo_name = repo.name
        branch = repo.default_branch
        remote_base = f"{self.config.openlist_target_directory}/{username}/{repo_name}"
        warnings: list[str] = []

        logger.info("Comparing repo: %s/%s", username, repo_name)

        try:
            tree = self.github.get_file_tree(owner, repo_name, branch)
        except GitHubAPIError as e:
            logger.error(
                "Skipping %s/%s: could not read its file list (%s). "
                "No files will be deleted from this repo in this cycle.",
                username, repo_name, e,
            )
            return [], 0, [f"{username}/{repo_name}: file list unavailable ({e})"]

        gh_map: dict[str, FileInfo] = {f.path: f for f in tree.files}

        # Compare against local manifest (not OpenList)
        manifest_files = self.manifest.get_files(owner, repo_name)

        upload_tasks: list[SyncTask] = []
        delete_count = 0

        self.openlist.ensure_directory_path(remote_base)
        self.manifest.set_branch(owner, repo_name, branch, private=repo.private)

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

        # Mirror delete: remove manifest entries that no longer exist on GitHub.
        # Only a complete tree is valid evidence of absence.
        if self.config.mirror_delete:
            if tree.truncated:
                logger.error(
                    "Mirror-delete skipped for %s/%s: GitHub truncated the file tree, so %d "
                    "tracked file(s) cannot be confirmed as deleted.",
                    username, repo_name, len(manifest_files),
                )
            else:
                stale_paths = [path for path in manifest_files if path not in gh_map]
                if stale_paths:
                    allowed, reason = self._delete_guard(
                        len(stale_paths), len(manifest_files), f"{username}/{repo_name}"
                    )
                    if not allowed:
                        warnings.append(reason)
                    else:
                        logger.info("Deleting %d stale file(s) from %s/%s (mirror mode)",
                                    len(stale_paths), username, repo_name)
                        deleted, failed = self._batch_delete(remote_base, stale_paths)
                        for path in deleted:
                            self.manifest.remove_file(owner, repo_name, path)
                        delete_count = len(deleted)
                        if failed:
                            failed_list = ", ".join(sorted(failed)[:5])
                            warnings.append(
                                f"{username}/{repo_name}: OpenList refused to delete {len(failed)} "
                                f"file(s) ({failed_list}); they stay in the manifest and will be "
                                f"retried next cycle."
                            )

        return upload_tasks, delete_count, warnings

    def _cleanup_stale_repos(
        self, username: str, present_repo_names: set[str], include_private: bool
    ) -> tuple[int, list[str]]:
        """Remove manifest entries and OpenList data for repos no longer on GitHub.

        ``present_repo_names`` must be the complete, case-folded repository name
        set from a successful listing. Returns (deleted_count, warnings).
        """
        warnings: list[str] = []
        tracked = self.manifest.list_repos(username)
        if not tracked:
            return 0, warnings

        invisible_reason = self._private_invisible_reason(include_private)
        candidates: list[str] = []
        for tracked_repo in tracked:
            if tracked_repo.casefold() in present_repo_names:
                continue
            entry = self.manifest.get_entry(username, tracked_repo) or {}
            if entry.get("private") and invisible_reason:
                message = (
                    f"{username}/{tracked_repo} is missing from the repository listing, but it was "
                    f"private and private repos are invisible right now ({invisible_reason}); "
                    f"keeping it."
                )
                logger.warning(message)
                warnings.append(message)
                continue
            candidates.append(tracked_repo)

        if not candidates:
            return 0, warnings

        allowed, reason = self._delete_guard(len(candidates), len(tracked), f"user {username}")
        if not allowed:
            return 0, warnings + [reason]

        remote_dir = f"{self.config.openlist_target_directory}/{username}"
        count = 0
        for tracked_repo in candidates:
            logger.info(
                "Repo '%s/%s' is not in GitHub's repository list; removing it from OpenList.",
                username, tracked_repo,
            )
            if self.openlist.remove_files(remote_dir, [tracked_repo]):
                self.manifest.remove_repo(username, tracked_repo)
                count += 1
            else:
                message = (
                    f"{username}/{tracked_repo}: OpenList refused to delete it; it stays in the "
                    f"manifest and will be retried next cycle."
                )
                logger.error(message)
                warnings.append(message)
        if count:
            logger.info("Cleaned up %d deleted repo(s) for user %s", count, username)
        return count, warnings

    # ------------------------------------------------------------------
    # Deletion safety net
    # ------------------------------------------------------------------

    def _private_invisible_reason(self, include_private: bool) -> Optional[str]:
        """Return why private repos cannot be listed, or None if they can."""
        if not include_private:
            return "github.sync_private_repos is disabled"
        if not getattr(self.github, "authenticated", False):
            return "no GitHub token is configured"
        if getattr(self.github, "token_rejected", False):
            return "the configured token was rejected and the listing is anonymous"
        return None

    def _delete_guard(self, count: int, total: int, scope: str) -> tuple[bool, Optional[str]]:
        """Refuse a deletion burst that looks like a broken listing rather than real churn.

        Blocks when ``count >= delete_guard_min_count`` *and*
        ``count > delete_guard_ratio * total``. Set ``sync.delete_guard_ratio: 1.0``
        to disable.
        """
        if count <= 0:
            return True, None

        min_count = self.config.delete_guard_min_count
        ratio = self.config.delete_guard_ratio
        if count >= min_count and count > ratio * total:
            reason = (
                f"Delete guard tripped for {scope}: {count} of {total} known item(s) would be "
                f"removed (guard: >= {min_count} items and > {ratio:.0%}). Nothing was deleted. "
                f"Confirm on GitHub that this really happened, then re-run, or set "
                f"sync.delete_guard_ratio to 1.0 to disable the guard."
            )
            logger.error(reason)
            return False, reason
        return True, None

    @staticmethod
    def _manifest_matches(gh_info: FileInfo, mf_entry: dict) -> bool:
        """Return True if the GitHub file matches the manifest entry."""
        mf_sha = mf_entry.get("sha")
        return gh_info.sha == mf_sha

    def _batch_delete(self, remote_base: str, paths: list[str]) -> tuple[set[str], set[str]]:
        """Delete files in batches grouped by parent directory.

        Returns (deleted_paths, failed_paths) so callers only drop manifest
        entries for files that OpenList actually removed.
        """
        groups: dict[str, list[tuple[str, str]]] = {}
        for path in paths:
            parent = os.path.dirname(path) or "/"
            name = os.path.basename(path)
            full_parent = f"{remote_base}/{parent}".rstrip("/")
            groups.setdefault(full_parent, []).append((name, path))

        deleted: set[str] = set()
        failed: set[str] = set()
        for directory, items in groups.items():
            if self.state.stop_requested:
                failed.update(path for _name, path in items)
                continue
            names = [name for name, _path in items]
            if self.openlist.remove_files(directory, names):
                deleted.update(path for _name, path in items)
            else:
                failed.update(path for _name, path in items)
        return deleted, failed

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
