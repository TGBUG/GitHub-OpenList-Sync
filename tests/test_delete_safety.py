"""Delete-safety regression tests.

These tests encode the invariant that a *failed* or *incomplete* GitHub fetch
must never be interpreted as "the repo/file is gone" and thus never trigger a
mirror-delete on OpenList.

They run with the standard library only: pytest is not required, and a tiny
``yaml`` stub is installed when PyYAML is missing so that ``sync_app.config``
can be imported on a bare interpreter.

    python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest

# --- make `sync_app.config` importable without PyYAML -----------------------
try:  # pragma: no cover - depends on the environment
    import yaml  # noqa: F401
except ImportError:  # pragma: no cover
    _yaml = types.ModuleType("yaml")
    _yaml.safe_load = lambda *a, **k: {}
    _yaml.safe_dump = lambda *a, **k: None
    sys.modules["yaml"] = _yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sync_app.failure_manager import FailureManager  # noqa: E402
from sync_app.github_client import GitHubAPIError, GitHubAuthError, GitHubClient  # noqa: E402
from sync_app.models import SyncState  # noqa: E402
from sync_app.sync_engine import SyncEngine, SyncManifest  # noqa: E402


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=None, headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text if text is not None else json.dumps(json_data or {})
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class FakeGitHubSession:
    """A requests.Session stand-in that serves canned GitHub API responses.

    It mirrors requests' header semantics: per-request headers are merged over
    the session headers, and a value of ``None`` removes the session header
    (that is how the client issues an anonymous request).
    """

    def __init__(self, router):
        self._router = router
        self.headers = {}
        self.calls = []

    def _merge_headers(self, extra):
        merged = dict(self.headers)
        for key, value in (extra or {}).items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        return merged

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("params")))
        headers = self._merge_headers(kwargs.get("headers"))
        return self._router(method, url, kwargs.get("params") or {}, headers)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, None))
        headers = self._merge_headers(kwargs.get("headers"))
        return self._router("GET", url, {}, headers)


class RecordingOpenList:
    """OpenList stand-in that records (and can fail) removals and uploads."""

    def __init__(self):
        self.removed = []
        self.uploaded = []
        self.fail_remove = False
        self.fail_upload = False
        self.created = []

    def ensure_directory_path(self, path):
        self.created.append(path)
        return True

    def remove_files(self, directory, names):
        self.removed.append((directory, tuple(names)))
        return not self.fail_remove

    def upload_file(self, local_path, remote_path):
        self.uploaded.append(remote_path)
        return not self.fail_upload

    @property
    def removed_names(self):
        out = []
        for _directory, names in self.removed:
            out.extend(names)
        return out


class FakeConfig:
    """Minimal Config stand-in (avoids needing a YAML file)."""

    def __init__(
        self,
        *,
        mirror_delete=True,
        sync_private_repos=False,
        filter_mode="blacklist",
        repo_filter_list=None,
        delete_guard_min_count=5,
        delete_guard_ratio=0.5,
        allow_unauthenticated_fallback=False,
    ):
        self.github_token = None
        self.sync_private_repos = sync_private_repos
        self.filter_mode = filter_mode
        self.repo_filter_list = repo_filter_list or []
        self.openlist_target_directory = "/github-sync"
        self.max_threads = 2
        self.max_retries = 1
        self.retry_interval_seconds = 0
        self.mirror_delete = mirror_delete
        self.delete_guard_min_count = delete_guard_min_count
        self.delete_guard_ratio = delete_guard_ratio
        self.allow_unauthenticated_fallback = allow_unauthenticated_fallback

    def is_repo_allowed(self, full_name):
        if not self.repo_filter_list:
            return True
        if self.filter_mode == "whitelist":
            return full_name in self.repo_filter_list
        return full_name not in self.repo_filter_list


def repo(name, owner="alice", default_branch="main", fork=False, private=False):
    return {
        "name": name,
        "full_name": f"{owner}/{name}",
        "default_branch": default_branch,
        "updated_at": "2026-09-28T00:00:00Z",
        "size": 1,
        "private": private,
        "fork": fork,
        "owner": {"login": owner},
    }


def tree(*paths, truncated=False):
    return {
        "tree": [
            {"path": p, "type": "blob", "size": 1, "sha": f"sha-{p}"} for p in paths
        ],
        "truncated": truncated,
    }


def router_for(repos, tree_payload, tree_status=200):
    """Route repository listings and file-tree lookups to canned payloads.

    The tree URL contains ``/repos/`` too, so it must be matched first.
    """
    def router(method, url, params, headers=None):
        if "/git/trees/" in url:
            return FakeResponse(tree_status, tree_payload)
        return FakeResponse(200, repos)
    return router


class EngineTestCase(unittest.TestCase):
    """Builds an engine wired to fake GitHub/OpenList backends."""

    username = "alice"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gols-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def make_engine(self, router, manifest=None, **config_kwargs):
        github = GitHubClient(token=None)
        github.session = FakeGitHubSession(router)

        openlist = RecordingOpenList()
        config = FakeConfig(**config_kwargs)
        failures = FailureManager(data_file=os.path.join(self.tmp, "failures.json"))
        state = SyncState()

        engine = SyncEngine(
            config=config,
            github_client=github,
            openlist_client=openlist,
            failure_manager=failures,
            sync_state=state,
        )
        engine.manifest = SyncManifest(os.path.join(self.tmp, "manifest.json"))
        if manifest:
            engine.manifest._data = json.loads(json.dumps(manifest))
        return engine, openlist, state

    @staticmethod
    def manifest_for(*repo_files):
        """manifest_for(("repoA", ["a.txt"]), ...) -> manifest dict."""
        data = {}
        for name, files in repo_files:
            data[f"alice/{name}"] = {
                "branch": "main",
                "files": {f: {"sha": f"sha-{f}"} for f in files},
            }
        return data


# ---------------------------------------------------------------------------
# R1 - a failed repo-list fetch must not be read as "all repos deleted"
# ---------------------------------------------------------------------------


class TestRepoListFailure(EngineTestCase):
    def test_401_does_not_delete_any_repo(self):
        """The incident: an expired/invalid token returned 401 -> full wipe."""
        manifest = self.manifest_for(("repoA", ["a.txt"]), ("repoB", ["b.txt"]))

        def router(method, url, params, headers=None):
            return FakeResponse(401, {"message": "Bad credentials", "status": "401"})

        engine, openlist, _state = self.make_engine(router, manifest)
        summary = engine._sync_user(self.username, include_private=False)

        self.assertEqual(openlist.removed, [], "401 must never delete anything from OpenList")
        self.assertIsNotNone(summary["error"], "a failed fetch must be reported to the caller")
        self.assertEqual(summary["files_deleted"], 0)
        self.assertEqual(sorted(engine.manifest._data), ["alice/repoA", "alice/repoB"])

    def test_404_user_does_not_delete_any_repo(self):
        manifest = self.manifest_for(("repoA", ["a.txt"]))
        engine, openlist, _state = self.make_engine(
            lambda m, u, p, h=None: FakeResponse(404, {"message": "Not Found"}), manifest
        )
        summary = engine._sync_user(self.username, include_private=False)

        self.assertEqual(openlist.removed, [])
        self.assertIsNotNone(summary["error"])

    def test_server_error_does_not_delete_any_repo(self):
        manifest = self.manifest_for(("repoA", ["a.txt"]))
        engine, openlist, _state = self.make_engine(
            lambda m, u, p, h=None: FakeResponse(500, {"message": "Server Error"}), manifest
        )
        engine._sync_user(self.username, include_private=False)
        self.assertEqual(openlist.removed, [])

    def test_mid_pagination_failure_does_not_delete_missing_pages(self):
        """Page 2 failing must not make page-3 repos look deleted."""
        page_one = [repo(f"repo{i:03d}") for i in range(100)]
        manifest = self.manifest_for(("repo150", ["a.txt"]), ("repo250", ["b.txt"]))

        def router(method, url, params, headers=None):
            if "/git/trees/" in url:
                return FakeResponse(200, tree())
            if int(params.get("page", 1)) == 1:
                return FakeResponse(200, page_one)
            return FakeResponse(500, {"message": "Server Error"})

        engine, openlist, _state = self.make_engine(router, manifest)
        summary = engine._sync_user(self.username, include_private=False)

        self.assertEqual(openlist.removed, [], "a partial page set must not drive deletions")
        self.assertIsNotNone(summary["error"])

    def test_genuinely_empty_account_still_cleans_up(self):
        """A real 200 with an empty list is still allowed to mirror-delete."""
        manifest = self.manifest_for(("repoA", ["a.txt"]))
        engine, openlist, _state = self.make_engine(router_for([], tree()), manifest)
        summary = engine._sync_user(self.username, include_private=False)

        self.assertEqual(openlist.removed, [("/github-sync/alice", ("repoA",))])
        self.assertIsNone(summary["error"])
        self.assertEqual(summary["files_deleted"], 1)

    def test_missing_repo_is_cleaned_up_when_list_succeeds(self):
        manifest = self.manifest_for(("repoA", ["a.txt"]), ("repoB", ["b.txt"]))
        engine, openlist, _state = self.make_engine(
            router_for([repo("repoA")], tree("a.txt")), manifest
        )
        engine._sync_user(self.username, include_private=False)
        self.assertEqual(openlist.removed_names, ["repoB"])


# ---------------------------------------------------------------------------
# R3/R4 - an incomplete file tree must not delete files
# ---------------------------------------------------------------------------


class TestFileTreeFailure(EngineTestCase):
    def test_tree_error_does_not_delete_files(self):
        manifest = self.manifest_for(("repoA", ["a.txt", "b.txt"]))
        engine, openlist, _state = self.make_engine(
            router_for([repo("repoA")], {"message": "Server Error"}, tree_status=500), manifest
        )
        summary = engine._sync_user(self.username, include_private=False)

        self.assertEqual(openlist.removed, [], "a failed tree fetch must not delete files")
        self.assertEqual(list(engine.manifest.get_files("alice", "repoA")), ["a.txt", "b.txt"])
        self.assertTrue(summary["warnings"], "the skipped repo must be reported")

    def test_truncated_tree_does_not_delete_files(self):
        manifest = self.manifest_for(("repoA", ["a.txt", "b.txt"]))
        engine, openlist, _state = self.make_engine(
            router_for([repo("repoA")], tree("a.txt", truncated=True)), manifest
        )
        engine._sync_user(self.username, include_private=False)

        self.assertEqual(openlist.removed, [], "a truncated tree must not delete unseen files")

    def test_legitimate_file_removal_still_works(self):
        manifest = self.manifest_for(("repoA", ["a.txt", "b.txt"]))
        engine, openlist, _state = self.make_engine(
            router_for([repo("repoA")], tree("a.txt")), manifest
        )
        engine._sync_user(self.username, include_private=False)

        self.assertEqual(openlist.removed_names, ["b.txt"])

    def test_tree_error_does_not_wipe_a_large_repo(self):
        """Six tracked files, tree fetch fails -> nothing may be deleted."""
        files = [f"f{i}.txt" for i in range(6)]
        manifest = self.manifest_for(("repoA", files))
        engine, openlist, _state = self.make_engine(
            router_for([repo("repoA")], {"message": "Bad credentials"}, tree_status=401), manifest
        )
        engine._sync_user(self.username, include_private=False)
        self.assertEqual(openlist.removed, [])


# ---------------------------------------------------------------------------
# R5 - local filter / privacy config must not delete existing repos
# ---------------------------------------------------------------------------


class TestLocalConfigMustNotDelete(EngineTestCase):
    def test_blacklisted_repo_is_not_deleted(self):
        manifest = self.manifest_for(("repoA", ["a.txt"]), ("repoB", ["b.txt"]))
        engine, openlist, _state = self.make_engine(
            router_for([repo("repoA"), repo("repoB")], tree("a.txt")),
            manifest, repo_filter_list=["alice/repoB"], filter_mode="blacklist",
        )
        engine._sync_user(self.username, include_private=False)

        self.assertEqual(openlist.removed, [], "filtered-out repos still exist on GitHub")
        self.assertIn("alice/repoB", engine.manifest._data)

    def test_whitelist_does_not_delete_unlisted_repo(self):
        manifest = self.manifest_for(("repoA", ["a.txt"]), ("repoB", ["b.txt"]))
        engine, openlist, _state = self.make_engine(
            router_for([repo("repoA"), repo("repoB")], tree("a.txt")),
            manifest, repo_filter_list=["alice/repoA"], filter_mode="whitelist",
        )
        engine._sync_user(self.username, include_private=False)
        self.assertEqual(openlist.removed, [])

    def test_disabling_private_sync_does_not_delete_private_repos(self):
        manifest = self.manifest_for(("repoA", ["a.txt"]), ("secret", ["s.txt"]))
        engine, openlist, _state = self.make_engine(
            router_for([repo("repoA"), repo("secret", private=True)], tree("a.txt")),
            manifest, sync_private_repos=False,
        )
        engine._sync_user(self.username, include_private=False)

        self.assertEqual(openlist.removed, [], "private repos must not be treated as deleted")
        self.assertIn("alice/secret", engine.manifest._data)

    def test_private_repo_absent_from_anonymous_listing_is_not_deleted(self):
        """A repo recorded as private must survive a listing that cannot see it."""
        manifest = {
            "alice/secret": {"branch": "main", "private": True,
                             "files": {"s.txt": {"sha": "sha-s.txt"}}},
        }
        engine, openlist, _state = self.make_engine(router_for([], tree()), manifest)
        summary = engine._sync_user(self.username, include_private=False)

        self.assertEqual(openlist.removed, [])
        self.assertIn("alice/secret", engine.manifest._data)
        self.assertTrue(summary["warnings"])

    def test_forked_repo_is_not_deleted(self):
        manifest = self.manifest_for(("repoA", ["a.txt"]), ("forked", ["f.txt"]))
        engine, openlist, _state = self.make_engine(
            router_for([repo("repoA"), repo("forked", fork=True)], tree("a.txt")), manifest
        )
        engine._sync_user(self.username, include_private=False)
        self.assertEqual(openlist.removed, [])


# ---------------------------------------------------------------------------
# R6 - bulk-delete guard
# ---------------------------------------------------------------------------


class TestBulkDeleteGuard(EngineTestCase):
    def test_mass_repo_deletion_is_blocked(self):
        manifest = self.manifest_for(*[(f"repo{i}", ["a.txt"]) for i in range(20)])
        engine, openlist, state = self.make_engine(router_for([], tree()), manifest)
        summary = engine._sync_user(self.username, include_private=False)

        self.assertEqual(openlist.removed, [], "a full-account wipe must be held back")
        self.assertTrue(any("Delete guard" in w for w in summary["warnings"]))
        self.assertIn("alice/repo0", engine.manifest._data)
        self.assertEqual(summary["files_deleted"], 0)
        self.assertEqual(state.deleted_files, 0)

    def test_small_deletion_below_guard_still_happens(self):
        manifest = self.manifest_for(("repoA", ["a.txt"]), ("repoB", ["b.txt"]), ("repoC", ["c.txt"]))
        engine, openlist, _state = self.make_engine(
            router_for([repo("repoA")], tree("a.txt")), manifest
        )
        engine._sync_user(self.username, include_private=False)
        self.assertEqual(set(openlist.removed_names), {"repoB", "repoC"})

    def test_guard_can_be_disabled_by_ratio_one(self):
        manifest = self.manifest_for(*[(f"repo{i}", ["a.txt"]) for i in range(20)])
        engine, openlist, _state = self.make_engine(
            router_for([], tree()), manifest, delete_guard_ratio=1.0
        )
        engine._sync_user(self.username, include_private=False)
        self.assertEqual(len(openlist.removed_names), 20)

    def test_large_file_deletion_burst_is_blocked(self):
        files = [f"f{i}.txt" for i in range(10)]
        manifest = self.manifest_for(("repoA", files))
        engine, openlist, _state = self.make_engine(
            router_for([repo("repoA")], tree("f0.txt")), manifest
        )
        summary = engine._sync_user(self.username, include_private=False)

        self.assertEqual(openlist.removed, [], "9 of 10 files vanishing is a red flag")
        self.assertTrue(any("Delete guard" in w for w in summary["warnings"]))


# ---------------------------------------------------------------------------
# R7 - manifest must survive a failed OpenList delete
# ---------------------------------------------------------------------------


class TestFailedRemoteDelete(EngineTestCase):
    def test_manifest_kept_when_openlist_delete_fails(self):
        manifest = self.manifest_for(("repoA", ["a.txt"]), ("repoB", ["b.txt"]))
        engine, openlist, _state = self.make_engine(
            router_for([repo("repoA")], tree("a.txt")), manifest
        )
        openlist.fail_remove = True
        summary = engine._sync_user(self.username, include_private=False)

        self.assertIn("alice/repoB", engine.manifest._data,
                      "keep tracking the repo so the next cycle retries the delete")
        self.assertEqual(summary["files_deleted"], 0)
        self.assertTrue(summary["warnings"])

    def test_manifest_removed_when_openlist_delete_succeeds(self):
        manifest = self.manifest_for(("repoA", ["a.txt"]), ("repoB", ["b.txt"]))
        engine, openlist, _state = self.make_engine(
            router_for([repo("repoA")], tree("a.txt")), manifest
        )
        engine._sync_user(self.username, include_private=False)
        self.assertNotIn("alice/repoB", engine.manifest._data)


# ---------------------------------------------------------------------------
# R8 - deleted_files must be counted once
# ---------------------------------------------------------------------------


class TestDeleteAccounting(EngineTestCase):
    def test_deleted_count_not_double_counted(self):
        manifest = self.manifest_for(("repoA", ["a.txt"]), ("repoB", ["b.txt"]))
        engine, openlist, state = self.make_engine(router_for([], tree()), manifest)
        summary = engine._sync_user(self.username, include_private=False)

        self.assertEqual(summary["files_deleted"], 2)
        self.assertEqual(state.deleted_files, 2, "state.deleted_files must match the summary")

    def test_deleted_count_via_stale_scan(self):
        manifest = self.manifest_for(("repoA", ["a.txt"]), ("repoB", ["b.txt"]), ("repoC", ["c.txt"]))
        engine, openlist, state = self.make_engine(
            router_for([repo("repoA")], tree("a.txt")), manifest
        )
        summary = engine._sync_user(self.username, include_private=False)
        self.assertEqual(summary["files_deleted"], 2)
        self.assertEqual(state.deleted_files, 2)


# ---------------------------------------------------------------------------
# Cycle-level reporting
# ---------------------------------------------------------------------------


class TestCycleReporting(EngineTestCase):
    def test_run_sync_surfaces_errors(self):
        manifest = self.manifest_for(("repoA", ["a.txt"]))
        engine, openlist, state = self.make_engine(
            lambda m, u, p, h=None: FakeResponse(401, {"message": "Bad credentials"}), manifest
        )
        engine.config.github_usernames = ["alice"]
        summary = engine.run_sync()

        self.assertEqual(openlist.removed, [])
        self.assertTrue(summary["errors"])
        self.assertIsNotNone(state.last_error)
        self.assertIn("401", state.last_error)

    def test_run_sync_clean_cycle_has_no_errors(self):
        manifest = self.manifest_for(("repoA", ["a.txt"]))
        engine, openlist, state = self.make_engine(
            router_for([repo("repoA")], tree("a.txt")), manifest
        )
        engine.config.github_usernames = ["alice"]
        summary = engine.run_sync()

        self.assertEqual(summary["errors"], [])
        self.assertIsNone(state.last_error)


# ---------------------------------------------------------------------------
# Happy path: the fix must not break normal uploading
# ---------------------------------------------------------------------------


class TestUploadPath(EngineTestCase):
    def _stub_download(self, engine):
        def fake_download(owner, repo, branch, file_path, dest_path):
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as fh:
                fh.write(b"content")
            return True

        engine.github.download_file = fake_download

    def test_new_file_is_downloaded_uploaded_and_recorded(self):
        manifest = self.manifest_for(("repoA", ["old.txt"]))
        engine, openlist, _state = self.make_engine(
            router_for([repo("repoA")], tree("old.txt", "new.txt")), manifest
        )
        self._stub_download(engine)
        summary = engine._sync_user(self.username, include_private=False)

        self.assertEqual(summary["files_uploaded"], 1)
        self.assertEqual(openlist.uploaded, ["/github-sync/alice/repoA/new.txt"])
        self.assertIn("new.txt", engine.manifest.get_files("alice", "repoA"))
        self.assertEqual(summary["files_deleted"], 0)
        self.assertEqual(openlist.removed, [])

    def test_unchanged_files_are_not_re_uploaded(self):
        manifest = self.manifest_for(("repoA", ["a.txt"]))
        engine, openlist, _state = self.make_engine(
            router_for([repo("repoA")], tree("a.txt")), manifest
        )
        self._stub_download(engine)
        summary = engine._sync_user(self.username, include_private=False)

        self.assertEqual(summary["files_uploaded"], 0)
        self.assertEqual(openlist.uploaded, [])

    def test_empty_repo_upload_is_skipped(self):
        """A 200 with an empty tree is "no files", not "all files deleted"."""
        manifest = self.manifest_for(("repoA", ["a.txt"]))
        engine, openlist, _state = self.make_engine(
            router_for([repo("repoA")], tree()), manifest
        )
        summary = engine._sync_user(self.username, include_private=False)

        self.assertEqual(openlist.removed_names, ["a.txt"])
        self.assertEqual(summary["files_deleted"], 1)


# ---------------------------------------------------------------------------
# github_client unit-level contracts
# ---------------------------------------------------------------------------


class TestGitHubClientContract(unittest.TestCase):
    def _client_with(self, router, token=None):
        client = GitHubClient(token=token)
        session_headers = dict(client.session.headers)
        client.session = FakeGitHubSession(router)
        client.session.headers.update(session_headers)
        return client

    def test_fetch_repos_raises_auth_error_on_401(self):
        client = self._client_with(lambda m, u, p, h=None: FakeResponse(401, {"message": "Bad credentials"}))
        with self.assertRaises(GitHubAuthError):
            client.fetch_repos("alice")

    def test_fetch_repos_raises_on_500(self):
        client = self._client_with(lambda m, u, p, h=None: FakeResponse(500, {"message": "boom"}))
        with self.assertRaises(GitHubAPIError):
            client.fetch_repos("alice")

    def test_fetch_repos_returns_all_owned_repos_including_skipped(self):
        payload = [repo("a"), repo("b", private=True), repo("c", fork=True)]
        client = self._client_with(lambda m, u, p, h=None: FakeResponse(200, payload))
        repos = client.fetch_repos("alice")
        self.assertEqual({r.name for r in repos}, {"a", "b", "c"})
        self.assertTrue([r for r in repos if r.name == "b"][0].private)
        self.assertTrue([r for r in repos if r.name == "c"][0].fork)

    def test_fetch_repos_paginates(self):
        pages = {1: [repo(f"r{i}") for i in range(100)], 2: [repo("last")]}

        def router(method, url, params, headers=None):
            return FakeResponse(200, pages[int(params["page"])])

        repos = self._client_with(router).fetch_repos("alice")
        self.assertEqual(len(repos), 101)

    def test_fetch_repos_raises_on_mid_pagination_error(self):
        def router(method, url, params, headers=None):
            if int(params["page"]) == 1:
                return FakeResponse(200, [repo(f"r{i}") for i in range(100)])
            # Avoid the "rate limit" wording: that path sleeps until reset.
            return FakeResponse(403, {"message": "Repository access blocked"})

        with self.assertRaises(GitHubAPIError):
            self._client_with(router).fetch_repos("alice")

    def test_invalid_token_falls_back_to_unauthenticated_for_public_data(self):
        def router(method, url, params, headers=None):
            if headers.get("Authorization"):
                return FakeResponse(401, {"message": "Bad credentials"})
            return FakeResponse(200, [repo("a")])

        client = self._client_with(router, token="expired-token")
        repos = client.fetch_repos("alice", allow_unauthenticated_fallback=True)

        self.assertEqual([r.name for r in repos], ["a"])
        self.assertTrue(client.token_rejected)

    def test_unauthorized_is_not_retried_without_opt_in(self):
        client = self._client_with(
            lambda m, u, p, h=None: FakeResponse(401, {"message": "Bad credentials"}),
            token="expired-token",
        )
        with self.assertRaises(GitHubAuthError):
            client.fetch_repos("alice", allow_unauthenticated_fallback=False)

    def test_get_file_tree_raises_on_error(self):
        client = self._client_with(lambda m, u, p, h=None: FakeResponse(500, {"message": "boom"}))
        with self.assertRaises(GitHubAPIError):
            client.get_file_tree("alice", "a", "main")

    def test_get_file_tree_marks_truncated(self):
        client = self._client_with(
            lambda m, u, p, h=None: FakeResponse(200, {"tree": [], "truncated": True})
        )
        result = client.get_file_tree("alice", "a", "main")
        self.assertFalse(result.complete)
        self.assertTrue(result.truncated)

    def test_get_file_tree_complete_when_not_truncated(self):
        client = self._client_with(
            lambda m, u, p, h=None: FakeResponse(200, {"tree": [
                {"path": "a.txt", "type": "blob", "size": 3, "sha": "s"},
                {"path": "sub", "type": "tree", "sha": "t"},
            ]})
        )
        result = client.get_file_tree("alice", "a", "main")
        self.assertTrue(result.complete)
        self.assertEqual([f.path for f in result.files], ["a.txt"])

    def test_token_is_stripped(self):
        client = GitHubClient(token="  ghp_token\n")
        self.assertEqual(client.session.headers["Authorization"], "Bearer ghp_token")
        self.assertTrue(client.authenticated)

    def test_no_token_is_not_authenticated(self):
        self.assertFalse(GitHubClient(token="   ").authenticated)
        self.assertFalse(GitHubClient(token=None).authenticated)


# ---------------------------------------------------------------------------
# Manifest robustness
# ---------------------------------------------------------------------------


class TestManifest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gols-manifest-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.manifest = SyncManifest(os.path.join(self.tmp, "sync_manifest.json"))

    def test_repo_keys_are_case_insensitive(self):
        self.manifest.set_file("TGBUG", "Repo", "a.txt", "sha1")
        self.assertTrue(self.manifest.get_files("tgbug", "repo"))
        self.assertTrue(self.manifest.get_files("TGBUG", "Repo"))
        self.assertEqual(self.manifest.list_repos("tgbug"), ["Repo"])
        self.manifest.remove_repo("tgbug", "repo")
        self.assertEqual(self.manifest.list_repos("TGBUG"), [])

    def test_private_flag_is_recorded(self):
        self.manifest.set_branch("alice", "secret", "main", private=True)
        self.assertTrue(self.manifest.get_entry("alice", "secret")["private"])
        self.manifest.set_branch("alice", "secret", "main")
        self.assertTrue(self.manifest.get_entry("alice", "secret")["private"],
                        "an omitted flag must not erase a known visibility")


if __name__ == "__main__":
    unittest.main(verbosity=2)
