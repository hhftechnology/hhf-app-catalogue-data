import copy
import unittest
from unittest import mock
from urllib.error import HTTPError
from datetime import timedelta
from pathlib import Path

from build_snapshot import FILENAMES, InvalidSnapshot, decode, encode, normalize_baseline, package, validate
from github_client import GitHub, GitHubError, development_signals
from publisher import run
from tests.fixture_source import BASELINE_INPUT, BUILD_TIME, ExampleGitHub, MemoryStore
from upload_r2 import BASELINE, POINTER, StoreError, read_previous


class _Response:
    """Minimal stand-in for an http.client response."""

    def __init__(self, payload):
        self.payload = payload
        self.headers = {}

    def read(self, _limit=None):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FlakyOpener:
    """Raises the supplied HTTP status codes, then answers with a fixed payload."""

    def __init__(self, failures, payload=b"{}"):
        self.failures = list(failures)
        self.payload = payload
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        if self.failures:
            raise HTTPError(request.full_url, self.failures.pop(0), "upstream", {}, None)
        return _Response(self.payload)


class PublisherTest(unittest.TestCase):
    def setUp(self):
        self.api = ExampleGitHub()
        self.store = MemoryStore()

    def publish(self, hours=0, target=None):
        return run(self.store, self.api, target, now=BUILD_TIME + timedelta(hours=hours), minimum=2)

    def test_fixture_bytes_and_manifest_write_order(self):
        self.assertEqual(self.publish(), "published 2026.11.15.1 (2 apps)")
        self.assertEqual(self.store.writes[-1], POINTER)
        self.assertEqual(self.store.objects["v1/manifests/2026.11.15.1.json"], self.store.objects[POINTER])
        root = Path(__file__).parent / "fixtures"
        checked_keys = []
        for path in sorted((root / "v1").rglob("*.json")):
            key = path.relative_to(root).as_posix()
            self.assertEqual(path.read_bytes(), self.store.objects[key])
            checked_keys.append(key)
        self.assertEqual(len(checked_keys), 7)

    def test_unchanged_retries_fetch_fresh_data_and_preserve_pointer(self):
        self.publish()
        old = self.store.objects[POINTER]
        self.assertEqual(self.publish(hours=1), "unchanged")
        self.assertEqual(self.store.objects[POINTER], old)
        self.api.stars = 15
        self.assertEqual(self.publish(hours=2), "published 2026.11.15.2 (2 apps)")
        self.assertEqual(read_previous(self.store)[1]["metrics"]["apps"][0]["stars"], 15)

    def test_high_priority_target_recheck_changes_ai_without_app_changes(self):
        self.publish()
        old = decode(self.store.objects[POINTER])
        self.publish(hours=1, target="sample-app")
        current = decode(self.store.objects[POINTER])
        changed = {role for role in current["files"] if current["files"][role]["sha256"] != old["files"][role]["sha256"]}
        self.assertEqual(changed, {"ai_signals"})
        self.assertEqual(len(self.api.ai_calls), 2)
        with self.assertRaisesRegex(InvalidSnapshot, "absent"):
            self.publish(hours=2, target="not-in-baseline")

    def test_rate_limit_resumes_completed_graphql_progress(self):
        scan = self.api.ai
        def rate_limit(*args):
            raise GitHubError("simulated rate limit")
        self.api.ai = rate_limit
        with self.assertRaises(GitHubError):
            self.publish()
        self.assertNotIn(POINTER, self.store.objects)
        self.api.ai = scan
        self.publish(hours=4)
        self.assertEqual(self.api.metric_calls, ["example/sample-app"])
        self.assertEqual(self.api.ai_calls, ["example/sample-app"])

    def test_malformed_live_or_candidate_snapshot_never_replaces_pointer(self):
        self.publish()
        pointer = self.store.objects[POINTER]
        self.api.stars = -1
        with self.assertRaisesRegex(InvalidSnapshot, "stars metric"):
            self.publish(hours=1)
        self.assertEqual(self.store.objects[POINTER], pointer)
        manifest = decode(pointer)
        self.store.objects[manifest["files"]["history"]["key"]] = b"{}"
        with self.assertRaisesRegex(InvalidSnapshot, "missing or corrupt"):
            self.publish(hours=2)
        self.assertEqual(self.store.objects[POINTER], pointer)

    def test_incomplete_upload_leaves_previous_pointer_and_retries_with_new_version(self):
        self.publish()
        pointer = self.store.objects[POINTER]
        self.api.stars = 13
        self.store.fail_key = "v1/snapshot/2026.11.15.2/metrics.json"
        with self.assertRaises(StoreError):
            self.publish(hours=1)
        self.assertEqual(self.store.objects[POINTER], pointer)
        self.store.fail_key = None
        self.assertEqual(self.publish(hours=2), "published 2026.11.15.3 (2 apps)")
        self.assertEqual(read_previous(self.store)[1]["metrics"]["apps"][0]["stars"], 13)

    def test_pointer_compare_and_swap_preserves_operator_rollback(self):
        self.publish()
        rollback = self.store.objects[POINTER]
        self.api.stars = 12
        self.publish(hours=1)
        put = self.store.put
        def rollback_before_commit(key, body, **condition):
            if key == POINTER:
                self.store.objects[POINTER] = rollback
            put(key, body, **condition)
        self.store.put = rollback_before_commit
        self.api.stars = 13
        with self.assertRaisesRegex(StoreError, "pointer changed"):
            self.publish(hours=2)
        self.assertEqual(self.store.objects[POINTER], rollback)

    def test_missing_repositories_keep_values_without_creating_star_samples(self):
        self.publish()
        self.api.unavailable = True
        self.publish(hours=24)
        _, data, _ = read_previous(self.store)
        self.assertEqual(data["metrics"]["apps"][0]["stars"], 11)
        self.assertTrue(data["metrics"]["apps"][0]["stale"])
        self.assertNotIn("sample-app", data["history"]["days"]["2026-11-16"])

    def test_daily_star_history_uses_exact_date_and_prunes_old_samples(self):
        self.publish()
        self.api.stars = 15
        self.publish(hours=24 * 7)
        row = read_previous(self.store)[1]["metrics"]["apps"][0]
        self.assertEqual((row["stars_prev_7d"], row["stars_delta_7d"]), (11, 4))
        self.publish(hours=24 * 90)
        files = read_previous(self.store)[1]
        self.assertNotIn("2026-11-15", files["history"]["days"])
        self.assertIsNone(files["metrics"]["apps"][0]["stars_delta_7d"])

    def test_unapproved_growth_stages_private_review_diff(self):
        baseline = {**BASELINE_INPUT, "apps": {"entries": []}, "metrics": {"apps": []}}
        for index in range(550):
            baseline["apps"]["entries"].append({"reference": f"example-{index}", "name": "Synthetic fixture", "id": index + 1})
            baseline["metrics"]["apps"].append({"id": index + 1, "commit": "2026-11-01"})
        initial = {**baseline, "apps": {"entries": baseline["apps"]["entries"][:524]}}
        self.store = MemoryStore(initial)
        self.publish()
        pointer = self.store.objects[POINTER]
        self.store.objects[BASELINE] = encode(baseline)
        with self.assertRaisesRegex(InvalidSnapshot, "review required for 26"):
            self.publish(hours=1)
        self.assertEqual(self.store.objects[POINTER], pointer)
        review = self.store.objects["v1/review/2026.11.15.2.json"]
        self.assertEqual(len(decode(review)["references"]), 26)
        baseline["approved_additions"] = decode(review)
        self.store.objects[BASELINE] = encode(baseline)
        self.assertEqual(self.publish(hours=2), "published 2026.11.15.3 (550 apps)")

    def test_invalid_baseline_is_rejected_before_api_or_storage_writes(self):
        duplicate = copy.deepcopy(BASELINE_INPUT)
        duplicate["apps"]["entries"].append(duplicate["apps"]["entries"][0])
        self.store = MemoryStore(duplicate)
        with self.assertRaisesRegex(InvalidSnapshot, "duplicate app"):
            self.publish()
        self.assertEqual(self.store.writes, [])
        self.assertEqual(self.api.metric_calls, [])

    def test_stored_snapshot_without_source_reference_still_validates(self):
        root = Path(__file__).parent / "fixtures" / "v1" / "snapshot" / "2026.11.15.1"
        files = {role: decode((root / filename).read_bytes()) for role, filename in FILENAMES.items()}
        validate(files, 2)
        legacy = copy.deepcopy(files)
        for row in legacy["apps"]["entries"]:
            row.pop("source_reference")
        with self.assertRaises(InvalidSnapshot):
            validate(legacy, 2)
        validate(legacy, 2, allow_legacy_app_fields=True)
        legacy["apps"]["entries"][0]["unexpected"] = "x"
        with self.assertRaises(InvalidSnapshot):
            validate(legacy, 2, allow_legacy_app_fields=True)

    def test_blank_source_reference_normalizes_to_null(self):
        baseline = copy.deepcopy(BASELINE_INPUT)
        baseline["apps"]["entries"][0]["source_reference"] = ""
        baseline["apps"]["entries"][1]["source_reference"] = "   "
        apps, _, _ = normalize_baseline(baseline, minimum=2)
        self.assertEqual([app["source_reference"] for app in apps], [None, None])
        baseline["apps"]["entries"][0]["source_reference"] = "not a reference"
        with self.assertRaises(InvalidSnapshot):
            normalize_baseline(baseline, minimum=2)

    def test_bad_schema_cannot_be_packaged(self):
        self.publish()
        data = read_previous(self.store)[1]
        data["metrics"]["apps"][0]["reference"] = "different-app"
        with self.assertRaisesRegex(InvalidSnapshot, "identities mismatch"):
            package(data, "2026.11.15.2", "2026-11-15T13:00:00Z")


class GitHubTest(unittest.TestCase):
    def test_ai_evidence_uses_development_artifacts(self):
        repo = "example/sample-app"
        self.assertEqual(development_signals(repo, [], [], "An AI chatbot with Codex integration", []), [])
        self.assertEqual(development_signals(repo, ["CLAUDE.md"], [], "", [])[0]["weight"], 0.6)
        commits = [{"sha": "a" * 40, "commit": {"message": "Change behavior\n\nCo-authored-by: Copilot <example@users.noreply.github.com>"}}]
        signals = development_signals(repo, [], [], "This repository is largely AI-assisted", commits)
        self.assertEqual([(item["type"], item["weight"]) for item in signals], [("commit_trailer", 1.0), ("readme_declaration", 0.3)])

    def test_transient_github_5xx_is_retried(self):
        client = GitHub("token")
        client.opener = _FlakyOpener([502, 503])
        with mock.patch("github_client.time.sleep") as sleep:
            self.assertEqual(client.request("/rate_limit"), {})
        self.assertEqual(client.opener.calls, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_persistent_github_5xx_fails_with_checkpoint_message(self):
        client = GitHub("token")
        client.opener = _FlakyOpener([502] * 5)
        with mock.patch("github_client.time.sleep"):
            with self.assertRaises(GitHubError) as caught:
                client.request("/rate_limit")
        self.assertIn("resume from its checkpoint", str(caught.exception))
        self.assertEqual(client.opener.calls, 5)

    def test_graphql_accepts_only_alias_level_not_found_and_public_repos(self):
        api = GitHub("unused")
        response = {"data": {"rateLimit": {"remaining": 300, "resetAt": "unknown"},
                             "r0": None, "r1": {"isPrivate": True, "nameWithOwner": "example/private"}},
                    "errors": [{"type": "NOT_FOUND", "path": ["r0"]}]}
        api.request = lambda *_: response
        self.assertEqual(api.metrics(("example/missing", "example/private")), {"example/missing": None, "example/private": None})
        response["errors"][0]["path"].append("object")
        with self.assertRaises(GitHubError):
            api.metrics(("example/missing", "example/private"))
