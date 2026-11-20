import argparse
import os
import sys
from datetime import UTC, datetime, timedelta
from itertools import batched
from urllib.error import URLError

from build_snapshot import (
    METRIC_FIELDS, REFERENCE, InvalidSnapshot, build, decode, digest, encode, normalize_baseline,
    package, require,
)
from github_client import GitHub, GitHubError, GitHubTransient, repository_name
from upload_r2 import BASELINE, POINTER, R2, StoreError, read_previous


REVIEW_THRESHOLD = 25


def utc_stamp(now):
    return now.isoformat().replace("+00:00", "Z")


def refresh_metrics(api, app_rows, progress, save, now):
    pending = {name: rows for name, rows in app_rows.items() if name not in progress["repositories"]}
    for group in batched(pending, 100):
        for repo, result in api.metrics(group).items():
            progress["repositories"][repo] = result
            if result is not None:
                observed = progress["observations"].setdefault(now.date().isoformat(), {})
                observed.update({app["reference"]: result["stargazerCount"] for app in pending[repo]})
        save()


def refresh_ai(api, progress, candidates, ai_signals, app_repos, now, save, target):
    for group in batched(candidates, 50):
        canonical = {ref: app_repos[ref]["nameWithOwner"] for ref in group}
        names = tuple(dict.fromkeys(canonical.values()))
        try:
            configs = api.ai_configs(names)
        except GitHubTransient as error:
            # A scheduled run must still publish, and the contract allows an app to
            # have no AI row. One heavy repository must not cost the rest of the
            # batch its check, so retry the batch one repository at a time. A
            # targeted recheck reports the failure instead of degrading.
            if target:
                raise
            configs = {}
            for name in names:
                try:
                    configs.update(api.ai_configs((name,)))
                except GitHubTransient:
                    print(f"AI check skipped for {name}: {error}", file=sys.stderr)
        for ref in group:
            repo = canonical[ref]
            # Absent when the repository is unavailable, and also when its batch
            # could not be fetched; both leave the app unchecked.
            config = configs.get(repo)
            if config is None:
                require(ref != target, "targeted GitHub repository is unavailable")
                continue
            try:
                evidence = api.ai(repo, config, app_repos[ref]["defaultBranchRef"] is not None)
            except GitHubTransient as error:
                # The commit history for one repository must not abort the snapshot.
                if target:
                    raise
                print(f"AI check skipped for {repo}: {error}", file=sys.stderr)
                continue
            confidence = min(1.0, round(sum(item["weight"] for item in evidence), 1))
            row = {"reference": ref, "confidence": confidence, "assisted": confidence >= 0.5,
                   "evidence": evidence, "checked_at": utc_stamp(now)}
            ai_signals[ref] = progress["ai"][ref] = row
            save()


def run(store, api, target=None, now=None, minimum=500):
    now = now or datetime.now(UTC)
    require(target is None or REFERENCE.fullmatch(target), "invalid target reference")
    baseline_bytes, _ = store.get(BASELINE)
    require(baseline_bytes is not None, "private R2 baseline is missing")
    baseline = decode(baseline_bytes)
    apps, dictionaries, base_metrics = normalize_baseline(baseline, minimum)
    app_by_ref = {app["reference"]: app for app in apps}
    require(target is None or target in app_by_ref, "reference is absent from private baseline")

    previous, files, etag = read_previous(store)
    previous_refs = {app["reference"]: app for app in files["apps"]["entries"]} if files else {}
    if previous:
        require(abs(len(apps) - previous["count"]) * 100 <= previous["count"] * 5, "app count changed by more than 5%")
        require(now > datetime.fromisoformat(previous["built"]), "live manifest has an equal or newer build time")
    additions = sorted(app_by_ref.keys() - previous_refs.keys()) if previous else []

    related = {
        app["reference"] for app in apps if (other := previous_refs.get(app["reference"]))
        and (app["id"], app["repository"]) == (other["id"], other["repository"])
    }
    previous_metrics = {
        row["reference"]: row for row in files["metrics"]["apps"] if row["reference"] in related
    } if files else {}
    ai_signals = {
        row["reference"]: row for row in files["ai_signals"]["apps"] if row["reference"] in related
    } if files else {}
    history = files["history"]["days"] if files else {}
    reset_refs = (app_by_ref.keys() & previous_refs.keys()) - related
    history = {day: {ref: stars for ref, stars in rows.items() if ref not in reset_refs} for day, rows in history.items()}

    key = f"v1/state/target/{target}.json" if target else "v1/state/scheduled.json"
    checkpoint, _ = store.get(key)
    identity = {"baseline": digest(baseline_bytes), "pointer": etag}
    progress = decode(checkpoint) if checkpoint else {}
    if progress.get("identity") != identity or now - datetime.fromisoformat(progress["started_at"]) > timedelta(days=2):
        progress = {"identity": identity, "started_at": utc_stamp(now), "repositories": {}, "observations": {}, "ai": {}}

    def save():
        store.put(key, encode(progress))

    app_rows = {}
    for app in apps:
        if target is None or app["reference"] == target:
            repo = repository_name(app["repository"])
            if repo:
                app_rows.setdefault(repo, []).append(app)
    require(target is None or app_rows, "target has no supported GitHub repository")
    refresh_metrics(api, app_rows, progress, save, now)

    metrics = {}
    app_repos = {}
    for app in apps:
        ref = app["reference"]
        observed = progress["repositories"].get(repository_name(app["repository"]))
        was_targeted = target is None or ref == target
        observed = observed if was_targeted else None
        older_metric = previous_metrics.get(ref, base_metrics.get(app["id"], {}))
        metrics[ref] = {field: older_metric.get(field) for field in METRIC_FIELDS}
        metrics[ref]["stale"] = older_metric.get("stale", True) if not was_targeted else observed is None
        if ref in related:
            for field in ("created", "language", "license"):
                app[field] = previous_refs[ref][field]
        if observed is not None:
            app_repos[ref] = observed
            default_branch = observed["defaultBranchRef"]
            last_commit = default_branch["target"]["committedDate"] if default_branch else None
            metrics[ref].update(stars=observed["stargazerCount"], forks=observed["forkCount"],
                                commit=datetime.fromisoformat(last_commit).astimezone(UTC).date().isoformat() if last_commit is not None else None,
                                archived=observed["isArchived"])
            app["created"] = datetime.fromisoformat(observed["createdAt"]).astimezone(UTC).date().isoformat()
            app["language"] = (observed["primaryLanguage"] or {}).get("name")
            license_info = observed["licenseInfo"]
            app["license"] = license_info["spdxId"] if license_info and not license_info["pseudoLicense"] else None

    ai_signals.update(progress["ai"])
    if target:
        require(target in app_repos, "targeted GitHub repository is unavailable")
        candidates = [] if target in progress["ai"] else [target]
    else:
        week_start = (now.date() - timedelta(days=now.weekday())).isoformat()
        budget = max(0, 200 - sum(row["checked_at"][:10] >= week_start for row in ai_signals.values()))
        recheck_before = utc_stamp(now - timedelta(days=30))
        candidates = [ref for ref in app_repos if ref not in ai_signals or ai_signals[ref]["checked_at"] < recheck_before]
        candidates.sort(key=lambda ref: (ref in ai_signals, -(metrics[ref]["stars"] or 0), ref))
        candidates = candidates[:budget]
    refresh_ai(api, progress, candidates, ai_signals, app_repos, now, save, target)

    snapshot = build(apps, dictionaries, metrics, ai_signals, history, progress["observations"], now.date())
    version = store.next_version(now.date())
    manifest, objects = package(snapshot, version, utc_stamp(now))
    if previous and all(manifest["files"][role]["sha256"] == previous["files"][role]["sha256"] for role in objects):
        # Clear the completed checkpoint so the next scheduled run observes fresh results.
        store.put(key, encode({}))
        return "unchanged"

    for role, body in objects.items():
        entry = manifest["files"][role]
        store.put(entry["key"], body, IfNoneMatch="*")
        stored, _ = store.get(entry["key"])
        require(stored is not None and digest(stored) == entry["sha256"], "uploaded bytes failed checksum validation")
    approval = {"base_version": previous["version"], "references": additions} if previous else None
    if len(additions) > REVIEW_THRESHOLD and approval != baseline.get("approved_additions"):
        store.put(f"v1/review/{version}.json", encode(approval), IfNoneMatch="*")
        raise InvalidSnapshot(f"review required for {len(additions)} new apps; see private v1/review/{version}.json")
    manifest_bytes = encode(manifest)
    store.put(f"v1/manifests/{version}.json", manifest_bytes, IfNoneMatch="*")
    condition = {"IfMatch": etag} if etag is not None else {"IfNoneMatch": "*"}
    store.put(POINTER, manifest_bytes, **condition)
    return f"published {version} ({len(apps)} apps)"


def main():
    parser = argparse.ArgumentParser(description="Refresh and publish the private app catalogue snapshot")
    parser.add_argument("--reference", help="Recheck a single baseline app for AI evidence")
    args = parser.parse_args()
    try:
        require(args.reference is None or REFERENCE.fullmatch(args.reference), "invalid target reference")
        print(run(R2(), GitHub(os.environ["HHF_APPS_GITHUB_TOKEN"]), args.reference))
    except (InvalidSnapshot, GitHubError, StoreError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except (KeyError, ValueError, TypeError, URLError, OSError) as error:
        print(f"Publisher could not read inputs or reach an upstream service ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
