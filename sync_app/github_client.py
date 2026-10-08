"""GitHub REST API client for fetching repos, file trees, and downloading files.

Contract for callers (see docs/adr/0001-fail-closed-mirror-delete.md):
**an empty result may only ever come from a successful HTTP 200 response.**
Every other outcome is reported by raising :class:`GitHubAPIError`, because a
mirror-delete driven by an empty list removes real data from OpenList.
"""

import logging
import time
from typing import Optional

import requests

from sync_app.models import FileInfo, FileTree, RepoInfo

logger = logging.getLogger("github_sync")


class GitHubAPIError(Exception):
    """A GitHub API call did not return usable data.

    Callers must treat this as *unknown*, never as "nothing there".
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None, url: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class GitHubAuthError(GitHubAPIError):
    """HTTP 401 - the configured token was rejected by GitHub."""


def _describe(response: requests.Response) -> str:
    """Best-effort extraction of GitHub's error message from a response."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:200] or "no response body"
    if isinstance(body, dict):
        return str(body.get("message") or body)[:200]
    return str(body)[:200]


class GitHubClient:
    BASE_URL = "https://api.github.com"

    def __init__(self, token: Optional[str] = None):
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/vnd.github+json"
        self.session.headers["X-GitHub-Api-Version"] = "2022-11-28"

        # A token pasted from a web page frequently carries surrounding
        # whitespace; "Bearer  ghp_x " is answered with 401 Bad credentials,
        # which is indistinguishable from an expired token.
        token = token.strip() if isinstance(token, str) else token
        self.authenticated = bool(token)
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

        # Set when GitHub has rejected the configured token during this process.
        self.token_rejected = False

        self._rate_limit_remaining = 5000
        self._rate_limit_reset = 0

    def _check_rate_limit(self, response: requests.Response):
        """Update rate limit info from response headers."""
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset_ts = response.headers.get("X-RateLimit-Reset")
        if remaining is not None:
            try:
                self._rate_limit_remaining = int(remaining)
            except ValueError:
                pass
        if reset_ts is not None:
            try:
                self._rate_limit_reset = int(reset_ts)
            except ValueError:
                pass

    def _wait_for_rate_limit(self):
        """Sleep until rate limit resets if exhausted."""
        if self._rate_limit_remaining <= 1:
            now = int(time.time())
            wait = max(self._rate_limit_reset - now + 1, 1)
            logger.warning("GitHub rate limit exhausted. Waiting %d seconds.", wait)
            time.sleep(wait)

    def _request(self, method: str, url: str, *, anonymous: bool = False, **kwargs) -> requests.Response:
        """Make a request with rate limit handling and retry on 429.

        Raises :class:`GitHubAPIError` when no usable response could be obtained.
        """
        max_retries = 3
        extra_headers = dict(kwargs.pop("headers", None) or {})
        if anonymous:
            # requests drops a session-level header when the per-request value is None.
            extra_headers["Authorization"] = None
        last_status: Optional[int] = None

        for attempt in range(max_retries):
            self._wait_for_rate_limit()
            try:
                response = self.session.request(
                    method, url, timeout=30, headers=extra_headers or None, **kwargs
                )
            except requests.RequestException as e:
                logger.error("GitHub request failed (attempt %d/%d): %s", attempt + 1, max_retries, e)
                if attempt == max_retries - 1:
                    raise GitHubAPIError(
                        f"GitHub request failed after {max_retries} attempts: {e}", url=url
                    ) from e
                time.sleep(2 ** attempt)
                continue

            self._check_rate_limit(response)
            last_status = response.status_code

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

        raise GitHubAPIError(
            f"GitHub request failed after {max_retries} attempts (last status: HTTP {last_status})",
            status_code=last_status,
            url=url,
        )

    # ------------------------------------------------------------------
    # Repositories
    # ------------------------------------------------------------------

    def fetch_repos(self, username: str, *, allow_unauthenticated_fallback: bool = False) -> list[RepoInfo]:
        """Fetch *every* repository owned by ``username``.

        Forks and private repositories are included and flagged, so the caller
        can distinguish "exists but is not synced" from "does not exist".

        Raises :class:`GitHubAPIError` on any incomplete or failed listing; it
        never returns a partial page set, because a partial list would make the
        missing repositories look deleted.

        With ``allow_unauthenticated_fallback`` a rejected token is retried
        without credentials (public data only). This is opt-in because the
        unauthenticated listing cannot show private repositories.
        """
        repos: list[RepoInfo] = []
        page = 1
        while True:
            url = f"{self.BASE_URL}/users/{username}/repos"
            params = {"per_page": 100, "page": page, "type": "owner", "sort": "updated"}
            response = self._request("GET", url, params=params)

            if response.status_code == 401 and self.authenticated and allow_unauthenticated_fallback:
                logger.warning(
                    "GitHub rejected the configured token (401 Bad credentials) while listing '%s'. "
                    "Retrying without authentication: only public repositories are visible.",
                    username,
                )
                self.token_rejected = True
                response = self._request("GET", url, params=params, anonymous=True)

            if response.status_code != 200:
                message = (
                    f"GitHub returned HTTP {response.status_code} while listing repositories for "
                    f"'{username}': {_describe(response)}"
                )
                if response.status_code == 401:
                    raise GitHubAuthError(
                        f"{message} - the configured github.token is invalid, expired or revoked",
                        status_code=401,
                        url=url,
                    )
                raise GitHubAPIError(message, status_code=response.status_code, url=url)

            data = response.json()
            if not isinstance(data, list):
                raise GitHubAPIError(
                    f"Unexpected repository payload for '{username}' (expected a list, got "
                    f"{type(data).__name__})",
                    status_code=response.status_code,
                    url=url,
                )

            repos.extend(RepoInfo.from_api(item) for item in data if isinstance(item, dict))

            if len(data) < 100:
                break
            page += 1

        if not repos:
            logger.warning(
                "GitHub reported 0 repositories for user %s (successful, complete listing).", username
            )
        else:
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

    # ------------------------------------------------------------------
    # File trees
    # ------------------------------------------------------------------

    def get_file_tree(self, owner: str, repo: str, branch: str) -> FileTree:
        """Get the recursive file tree for a repository branch.

        Raises :class:`GitHubAPIError` when the tree could not be read. The
        returned :class:`FileTree` is marked ``truncated`` when GitHub capped
        the response, in which case it must not drive deletions.
        """
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/git/trees/{branch}"
        params = {"recursive": "1"}
        response = self._request("GET", url, params=params)

        if response.status_code != 200:
            raise GitHubAPIError(
                f"Could not read the file tree of {owner}/{repo}@{branch}: HTTP "
                f"{response.status_code} {_describe(response)}",
                status_code=response.status_code,
                url=url,
            )

        data = response.json()
        if not isinstance(data, dict):
            raise GitHubAPIError(
                f"Unexpected file tree payload for {owner}/{repo}@{branch} "
                f"(expected an object, got {type(data).__name__})",
                status_code=response.status_code,
                url=url,
            )

        truncated = bool(data.get("truncated"))
        files = []
        for item in data.get("tree", []):
            if item.get("type") != "blob":
                continue
            size = item.get("size", 0)
            if size is None:
                size = 0
            files.append(FileInfo(
                path=item["path"],
                size=size,
                sha=item.get("sha", ""),
                is_dir=False,
            ))

        if truncated:
            logger.error(
                "File tree for %s/%s@%s is truncated by GitHub (%d entries returned). "
                "The listing is incomplete; mirror-delete is disabled for this repository.",
                owner, repo, branch, len(files),
            )
        else:
            logger.info("Repo %s/%s: %d files in tree", owner, repo, len(files))
        return FileTree(files=files, truncated=truncated)

    # ------------------------------------------------------------------
    # Downloads
    # ------------------------------------------------------------------

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
            logger.error("Failed to download %s: HTTP %d", raw_url, response.status_code)
            return False
        except requests.RequestException as e:
            logger.error("Download error for %s: %s", raw_url, e)
            return False
        except OSError as e:
            logger.error("Could not write %s: %s", dest_path, e)
            return False
