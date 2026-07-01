"""OpenList REST API client for file operations via JWT-authenticated session."""

import json
import logging
import os
import time
from typing import Optional

import requests

from sync_app.models import FileInfo

logger = logging.getLogger("github_sync")


class OpenListClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self._token: Optional[str] = None
        self._token_expiry: float = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _api_url(self, path: str) -> str:
        return f"{self.base_url}/api{path}"

    def _ensure_auth(self):
        """Check token validity and refresh if needed."""
        if self._token and time.time() < self._token_expiry - 60:
            return
        self.login()

    def _request(self, method: str, path: str, auth_required: bool = True, **kwargs) -> dict:
        """Make an API request and return parsed JSON data field, or raise on error."""
        if auth_required:
            self._ensure_auth()

        headers = kwargs.pop("headers", {})
        if self._token:
            headers["Authorization"] = self._token

        url = self._api_url(path)
        try:
            response = self.session.request(method, url, headers=headers, timeout=60, **kwargs)
        except requests.RequestException as e:
            raise ConnectionError(f"OpenList request failed: {e}")

        # Handle 401 – token may have been invalidated server-side
        if response.status_code == 401 and auth_required:
            logger.info("Token expired, re-logging in...")
            self.login()
            headers["Authorization"] = self._token
            response = self.session.request(method, url, headers=headers, timeout=60, **kwargs)

        try:
            body = response.json()
        except json.JSONDecodeError:
            raise RuntimeError(f"OpenList returned non-JSON response (HTTP {response.status_code}): {response.text[:500]}")

        if isinstance(body, dict):
            code = body.get("code", response.status_code)
            message = body.get("message", "")
            if code not in (200, 0):
                raise RuntimeError(f"OpenList API error (code={code}): {message}")
            return body.get("data", body)
        return body

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def login(self):
        """Authenticate and store JWT token."""
        url = self._api_url("/auth/login")
        try:
            response = self.session.post(
                url,
                json={"username": self.username, "password": self.password},
                timeout=30,
            )
            body = response.json()
        except requests.RequestException as e:
            raise ConnectionError(f"Login request failed: {e}")
        except json.JSONDecodeError:
            raise RuntimeError(f"Login response is not valid JSON: {response.text[:200]}")

        code = body.get("code", response.status_code)
        if code not in (200, 0):
            raise RuntimeError(f"Login failed (code={code}): {body.get('message', 'Unknown error')}")

        data = body.get("data", body)
        self._token = data.get("token")
        if not self._token:
            raise RuntimeError("Login response missing token field")

        # Assume token is valid for ~2 hours (standard JWT practice)
        self._token_expiry = time.time() + 7200
        logger.info("OpenList login successful")

    # ------------------------------------------------------------------
    # File system operations
    # ------------------------------------------------------------------

    def list_directory(self, path: str) -> list[FileInfo]:
        """List files and directories at the given path. Handles pagination."""
        items: list[FileInfo] = []
        page = 1
        while True:
            try:
                data = self._request("POST", "/fs/list", json={
                    "path": path,
                    "page": page,
                    "per_page": 100,
                    "refresh": False,
                })
            except RuntimeError as e:
                logger.error("Failed to list directory '%s': %s", path, e)
                return items

            if data is None:
                break
            if isinstance(data, dict):
                content = data.get("content") or []
                for item in content:
                    items.append(FileInfo(
                        path=item.get("name", ""),
                        size=item.get("size", 0) or 0,
                        is_dir=item.get("is_dir", False),
                    ))
                total = data.get("total", 0)
                if len(items) >= total or len(content) == 0:
                    break
            elif isinstance(data, list):
                for item in data:
                    items.append(FileInfo(
                        path=item.get("name", ""),
                        size=item.get("size", 0) or 0,
                        is_dir=item.get("is_dir", False),
                    ))
                if len(data) < 100:
                    break
            else:
                break
            page += 1

        return items

    def get_file_info(self, path: str) -> Optional[FileInfo]:
        """Get metadata for a single file or directory."""
        try:
            data = self._request("POST", "/fs/get", json={"path": path})
        except RuntimeError as e:
            logger.error("Failed to get info for '%s': %s", path, e)
            return None

        if isinstance(data, dict):
            return FileInfo(
                path=data.get("name", ""),
                size=data.get("size", 0) or 0,
                is_dir=data.get("is_dir", False),
            )
        return None

    def create_directory(self, path: str) -> bool:
        """Create a directory. Returns True on success."""
        try:
            self._request("POST", "/fs/mkdir", json={"path": path})
            logger.debug("Created directory: %s", path)
            return True
        except RuntimeError as e:
            error_str = str(e)
            if "already exists" in error_str.lower() or "exist" in error_str.lower():
                logger.debug("Directory already exists: %s", path)
                return True
            logger.error("Failed to create directory '%s': %s", path, e)
            return False

    def ensure_directory_path(self, path: str) -> bool:
        """Create a directory and all parent directories as needed."""
        parts = path.strip("/").split("/")
        current = ""
        for part in parts:
            current = f"{current}/{part}"
            if not self.create_directory(current):
                return False
        return True

    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """Upload a file to OpenList using stream upload. Returns True on success."""
        file_size = os.path.getsize(local_path)

        try:
            self._ensure_auth()
            url = self._api_url("/fs/put")
            headers = {
                "File-Path": remote_path,
                "Content-Type": "application/octet-stream",
                "Content-Length": str(file_size),
                "Overwrite": "true",
            }
            if self._token:
                headers["Authorization"] = self._token

            with open(local_path, "rb") as f:
                response = self.session.put(url, headers=headers, data=f, timeout=120)

            if response.status_code == 401:
                self.login()
                headers["Authorization"] = self._token
                with open(local_path, "rb") as f:
                    response = self.session.put(url, headers=headers, data=f, timeout=120)

            if response.status_code in (200, 201, 204):
                return True

            # Fallback: try form upload
            logger.debug("Stream upload returned %d, trying form upload...", response.status_code)
            return self._upload_form(local_path, remote_path)

        except (requests.RequestException, OSError) as e:
            logger.error("Upload failed for %s: %s", remote_path, e)
            return False

    def _upload_form(self, local_path: str, remote_path: str) -> bool:
        """Fallback: upload file using multipart form data."""
        try:
            self._ensure_auth()
            url = self._api_url("/fs/form")
            file_name = os.path.basename(local_path)

            with open(local_path, "rb") as f:
                files = {"file": (file_name, f, "application/octet-stream")}
                headers = {
                    "File-Path": remote_path,
                    "Overwrite": "true",
                }
                if self._token:
                    headers["Authorization"] = self._token
                response = self.session.put(
                    url,
                    files=files,
                    headers=headers,
                    timeout=120,
                )

            if response.status_code == 401:
                self.login()
                headers["Authorization"] = self._token
                headers["File-Path"] = remote_path
                headers["Overwrite"] = "true"
                with open(local_path, "rb") as f:
                    files = {"file": (file_name, f, "application/octet-stream")}
                    response = self.session.put(
                        url,
                        files=files,
                        headers=headers,
                        timeout=120,
                    )

            return response.status_code in (200, 201, 204)
        except (requests.RequestException, OSError) as e:
            logger.error("Form upload failed for %s: %s", remote_path, e)
            return False

    def remove_files(self, directory: str, names: list[str]) -> bool:
        """Remove one or more files/directories from a directory. Returns True on success."""
        if not names:
            return True
        try:
            self._request("POST", "/fs/remove", json={
                "dir": directory,
                "names": names,
            })
            logger.info("Removed %d file(s) from %s", len(names), directory)
            return True
        except RuntimeError as e:
            logger.error("Failed to remove files from %s: %s", directory, e)
            return False
