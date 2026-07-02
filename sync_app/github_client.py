"""GitHub REST API client for fetching repos, file trees, and downloading files."""

import logging
import time
from typing import Optional

import requests

from sync_app.models import FileInfo

logger = logging.getLogger("github_sync")


class GitHubClient:
    BASE_URL = "https://api.github.com"

    def __init__(self, token: Optional[str] = None):
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/vnd.github+json"
        self.session.headers["X-GitHub-Api-Version"] = "2022-11-28"
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self._rate_limit_remaining = 5000
        self._rate_limit_reset = 0

    def _check_rate_limit(self, response: requests.Response):
        """Update rate limit info from response headers."""
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset_ts = response.headers.get("X-RateLimit-Reset")
        if remaining is not None:
            self._rate_limit_remaining = int(remaining)
        if reset_ts is not None:
            self._rate_limit_reset = int(reset_ts)

    def _wait_for_rate_limit(self):
        """Sleep until rate limit resets if exhausted."""
        if self._rate_limit_remaining <= 1:
            now = int(time.time())
            wait = max(self._rate_limit_reset - now + 1, 1)
            logger.warning("GitHub rate limit exhausted. Waiting %d seconds.", wait)
            time.sleep(wait)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Make a request with rate limit handling and retry on 429."""
        max_retries = 3
        for attempt in range(max_retries):
            self._wait_for_rate_limit()
            try:
                response = self.session.request(method, url, timeout=30, **kwargs)
            except requests.RequestException as e:
                logger.error("GitHub request failed (attempt %d/%d): %s", attempt + 1, max_retries, e)
                if attempt == max_retries - 1:
                    raise
                time.sleep(2 ** attempt)
                continue

            self._check_rate_limit(response)

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                logger.warning("GitHub 429 rate limited. Retrying after %d seconds.", retry_after)
                time.sleep(retry_after)
                continue

            if response.status_code == 403 and "rate limit" in response.text.lower():
                reset_ts = int(response.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait = max(reset_ts - int(time.time()) + 1, 1)
                logger.warning("Secondary rate limit hit. Waiting %d seconds.", wait)
                time.sleep(wait)
                continue

            return response

        raise RuntimeError("GitHub request failed after max retries")

    def get_repos(self, username: str, include_private: bool = False) -> list[dict]:
        """Fetch all non-fork repositories for a user. Returns list of repo dicts."""
        repos = []
        private_count = 0
        page = 1
        while True:
            url = f"{self.BASE_URL}/users/{username}/repos"
            params = {"per_page": 100, "page": page, "type": "owner", "sort": "updated"}
            response = self._request("GET", url, params=params)

            if response.status_code != 200:
                logger.error("Failed to fetch repos for %s: %s", username, response.text)
                break

            data = response.json()
            if not data:
                break

            for repo in data:
                if repo.get("fork"):
                    continue
                is_private = repo.get("private", False)
                if is_private and not include_private:
                    private_count += 1
                    continue
                repos.append({
                    "name": repo["name"],
                    "full_name": repo["full_name"],
                    "default_branch": repo["default_branch"],
                    "updated_at": repo["updated_at"],
                    "size": repo["size"],
                    "private": is_private,
                    "owner": repo["owner"]["login"],
                })

            if len(data) < 100:
                break
            page += 1

        if private_count > 0:
            logger.info("Skipped %d private repo(s) for user %s (sync_private_repos=false)", private_count, username)
        logger.info("Fetched %d repositories for user %s", len(repos), username)
        return repos

    def get_default_branch(self, owner: str, repo: str) -> str:
        """Get the default branch name for a repository."""
        url = f"{self.BASE_URL}/repos/{owner}/{repo}"
        response = self._request("GET", url)
        if response.status_code == 200:
            return response.json().get("default_branch", "main")
        logger.warning("Could not get default branch for %s/%s, defaulting to 'main'", owner, repo)
        return "main"

    def get_file_tree(self, owner: str, repo: str, branch: str) -> list[FileInfo]:
        """Get recursive file tree for a repository branch. Returns only files (blobs)."""
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/git/trees/{branch}"
        params = {"recursive": "1"}
        response = self._request("GET", url, params=params)

        if response.status_code != 200:
            logger.error("Failed to get tree for %s/%s@%s: %s", owner, repo, branch, response.text)
            return []

        data = response.json()
        if data.get("truncated"):
            logger.warning("Tree for %s/%s is truncated! Large repo may be incomplete.", owner, repo)

        files = []
        for item in data.get("tree", []):
            if item["type"] == "blob":
                size = item.get("size", 0)
                if size is None:
                    size = 0
                files.append(FileInfo(
                    path=item["path"],
                    size=size,
                    sha=item.get("sha", ""),
                    is_dir=False,
                ))

        logger.info("Repo %s/%s: %d files in tree", owner, repo, len(files))
        return files

    def download_file(self, owner: str, repo: str, branch: str, file_path: str, dest_path: str) -> bool:
        """Download a single file from GitHub to a local path. Returns True on success."""
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
        try:
            self._wait_for_rate_limit()
            response = self.session.get(raw_url, timeout=60, stream=True)
            if response.status_code == 200:
                import os
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                with open(dest_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                return True
            else:
                logger.error("Failed to download %s: HTTP %d", raw_url, response.status_code)
                return False
        except requests.RequestException as e:
            logger.error("Download error for %s: %s", raw_url, e)
            return False
