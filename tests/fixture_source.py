from datetime import UTC, datetime

from build_snapshot import digest, encode
from upload_r2 import BASELINE, StoreError


BUILD_TIME = datetime(2026, 11, 15, 12, tzinfo=UTC)
BASELINE_INPUT = {
    "apps": {"entries": [
        {"id": 1, "name": "Sample App", "reference": "sample-app", "description": "A synthetic fixture",
         "repository": "https://github.com/example/sample-app", "platform": ["Docker"], "tags": [7], "alternatives": [5],
         "icon_key": f"v1/icons/sample-app/{'0' * 64}.png"},
        {"id": 2, "name": "Sample Other", "reference": "sample-other", "tags": [7]},
    ]},
    "metrics": {"apps": [
        {"id": 1, "stars": 1, "forks": 0, "commit": "2026-11-14"},
        {"id": 2, "stars": None, "forks": None, "commit": "2026-11-12"},
    ]},
    "tags": [{"id": 7, "label": "Automation"}],
    "alternatives": [{"id": 5, "label": "Sample Alternative"}],
}


class MemoryStore:
    def __init__(self, baseline=BASELINE_INPUT):
        self.objects = {BASELINE: encode(baseline)}
        self.writes = []
        self.fail_key = None

    def get(self, key):
        body = self.objects.get(key)
        return (body, digest(body)) if body is not None else (None, None)

    def put(self, key, body, **condition):
        previous, etag = self.get(key)
        if key == self.fail_key:
            raise StoreError("simulated storage failure")
        if condition.get("IfNoneMatch") == "*" and previous is not None:
            raise StoreError("immutable object already exists")
        if "IfMatch" in condition and condition["IfMatch"] != etag:
            raise StoreError("pointer changed")
        self.objects[key] = body
        self.writes.append(key)

    def next_version(self, today):
        prefix = f"v1/snapshot/{today:%Y.%m.%d}."
        sequences = [int(key.removeprefix(prefix).split("/")[0]) for key in self.objects if key.startswith(prefix)]
        return f"{today:%Y.%m.%d}.{max(sequences, default=0) + 1}"


class ExampleGitHub:
    def __init__(self):
        self.metric_calls = []
        self.ai_calls = []
        self.stars = 11
        self.unavailable = False
        self.no_commit = False

    def metrics(self, names):
        self.metric_calls.extend(names)
        return {name: None if self.unavailable else {
            "nameWithOwner": name, "stargazerCount": self.stars, "forkCount": 2, "createdAt": "2026-01-01T00:00:00Z",
            "isArchived": False, "licenseInfo": {"spdxId": "MIT", "pseudoLicense": False}, "primaryLanguage": {"name": "Python"},
            "defaultBranchRef": None if self.no_commit else {"target": {"committedDate": "2026-11-14T12:00:00Z"}},
        } for name in names}

    def ai_configs(self, names):
        return {name: {} for name in names}

    def ai(self, name, config, has_commits):
        self.ai_calls.append(name)
        return []
