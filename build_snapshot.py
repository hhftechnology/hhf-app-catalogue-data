import hashlib
import json
import re
from datetime import date, datetime, timedelta


FILENAMES = {
    "apps": "apps.json",
    "metrics": "metrics.json",
    "tags": "tags.json",
    "alternatives": "alternatives.json",
    "ai_signals": "ai-signals.json",
    "history": "history.json",
}
APP_FIELDS = (
    "id", "reference", "source_reference", "name", "description", "website", "repository", "source",
    "license", "language", "sso", "platform", "tags", "alternatives", "created",
    "icon_source", "icon_override", "icon_modifier",
)
METRIC_FIELDS = ("stars", "forks", "commit", "trending", "archived")
REFERENCE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9-]{1,63}")
VERSION = re.compile(r"\d{4}\.\d{2}\.\d{2}\.[1-9][0-9]*")
MAX_INT = 2_147_483_647


class InvalidSnapshot(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InvalidSnapshot(message)


def validate_approved_metadata(app):
    key = app.get("icon_key")
    prefix = f"v1/icons/{app['reference']}/"
    require(
        key is None or (
            isinstance(key, str) and key.startswith(prefix)
            and re.fullmatch(r"[a-f0-9]{64}\.(png|svg|webp)", key.removeprefix(prefix))
        ),
        "invalid icon key",
    )
    description = app.get("description")
    require(description is None or (isinstance(description, str) and len(description) <= 300), "invalid description")


def encode(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()


def decode(body):
    def unique_keys(pairs):
        result = dict(pairs)
        require(len(result) == len(pairs), "duplicate JSON key")
        return result

    def invalid_constant(_):
        raise InvalidSnapshot("invalid non-finite JSON number")

    return json.loads(body, object_pairs_hook=unique_keys, parse_constant=invalid_constant)


def digest(body):
    return hashlib.sha256(body).hexdigest()


def integer(value, minimum=0):
    return type(value) is int and minimum <= value <= MAX_INT


def iso_date(value):
    return isinstance(value, str) and date.fromisoformat(value).isoformat() == value


def iso_time(value):
    return isinstance(value, str) and value.endswith("Z") and bool(datetime.fromisoformat(value))


def taxonomy(rows, role):
    require(isinstance(rows, list), "taxonomy must be a list")
    labels = {}
    slugs = set()
    for row in rows:
        require(isinstance(row, dict) and integer(row.get("id"), 1), "invalid taxonomy id")
        label = row.get("label")
        require(isinstance(label, str) and label.strip() and row["id"] not in labels, "invalid taxonomy label or duplicate id")
        if role == "tags":
            slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
            require(slug and slug not in slugs, "empty or duplicate tag slug")
            slugs.add(slug)
        labels[row["id"]] = label
    return [{"id": item, "label": labels[item]} for item in sorted(labels)]


def normalize_baseline(baseline, minimum=500):
    entries = baseline["apps"]["entries"]
    require(isinstance(entries, list) and len(entries) >= minimum, "baseline has too few apps")
    apps = []
    for row in entries:
        require(isinstance(row, dict), "app rows must have named fields")
        require(integer(row.get("id"), 1), "invalid app id")
        ref = row.get("reference")
        require(isinstance(ref, str) and REFERENCE.fullmatch(ref), "invalid app reference")
        source_ref = row.get("source_reference")
        require(
            source_ref is None or (isinstance(source_ref, str) and REFERENCE.fullmatch(source_ref)),
            "invalid source reference",
        )
        name = row.get("name")
        require(isinstance(name, str) and name.strip() and len(name) <= 120, "invalid app name")
        app = {key: row.get(key) for key in APP_FIELDS}
        for field in ("tags", "alternatives"):
            ids = row.get(field, [])
            require(isinstance(ids, list) and all(integer(value, 1) for value in ids), "invalid app taxonomy")
            app[field] = sorted(set(ids))
        platform = row.get("platform", [])
        require(isinstance(platform, list) and all(isinstance(value, str) for value in platform), "invalid platform list")
        app["platform"] = sorted(set(platform))
        require(all(app[key] is None or isinstance(app[key], str) for key in APP_FIELDS if key not in {"id", "platform", "tags", "alternatives"}), "invalid app metadata")
        for field, value in app.items():
            if isinstance(value, str) and not value.strip():
                app[field] = None
        require(app["sso"] in (None, "no", "yes", "enterprise", "plugin"), "invalid sso")
        require(app["created"] is None or iso_date(app["created"]), "invalid app creation date")
        if row.get("icon_key") is not None:
            app["icon_key"] = row["icon_key"]
        validate_approved_metadata(app)
        apps.append(app)
    ids = {app["id"] for app in apps}
    require(len(ids) == len(apps) == len({app["reference"] for app in apps}), "duplicate app id or reference")

    dictionaries = {role: taxonomy(baseline[role], role) for role in ("tags", "alternatives")}

    source_metrics = {}
    rows = baseline["metrics"]["apps"]
    require(isinstance(rows, list), "baseline metrics must be a list")
    for row in rows:
        require(isinstance(row, dict) and integer(row.get("id"), 1) and row["id"] not in source_metrics, "invalid or duplicate metric id")
        metric = {field: row.get(field) for field in METRIC_FIELDS}
        if metric["commit"] == "":
            metric["commit"] = None
        source_metrics[row["id"]] = metric
    require(len(source_metrics.keys() & ids) * 10 >= len(ids) * 9, "baseline metrics coverage below 90%")
    return sorted(apps, key=lambda app: app["reference"]), dictionaries, source_metrics


def build(apps, dictionaries, metrics, ai_signals, history, observations, today):
    cutoff = today - timedelta(days=89)
    days = {
        day: dict(stars) for day, stars in history.items()
        if cutoff <= date.fromisoformat(day) <= today
    }
    for day, stars in observations.items():
        if cutoff <= date.fromisoformat(day) <= today:
            days.setdefault(day, {}).update(stars)
    previous_week = days.get((today - timedelta(days=7)).isoformat(), {})
    days.setdefault(today.isoformat(), {})
    metrics_rows = []
    for app in apps:
        ref = app["reference"]
        row = metrics[ref]
        previous = previous_week.get(ref)
        stars = row["stars"]
        delta = stars - previous if stars is not None and previous is not None and not row["stale"] else None
        metrics_rows.append({**row, "id": app["id"], "reference": ref, "stars_prev_7d": previous, "stars_delta_7d": delta})
    return {
        "apps": {"entries": apps}, "metrics": {"apps": metrics_rows}, **dictionaries,
        "ai_signals": {"apps": [ai_signals[app["reference"]] for app in apps if app["reference"] in ai_signals]},
        "history": {"window_days": 90, "days": days},
    }


def validate(files, count):
    require(set(files) == set(FILENAMES), "snapshot roles mismatch")
    apps = files["apps"]["entries"]
    refs = {row["reference"]: row["id"] for row in apps}
    require(count > 0 and len(apps) == len(refs) == count, "snapshot count mismatch")
    require(len(set(refs.values())) == count, "duplicate snapshot id")
    app_fields = set(APP_FIELDS)
    require(all(set(row).difference({"icon_key"}) == app_fields and REFERENCE.fullmatch(row["reference"]) and integer(row["id"], 1) and row["name"].strip() for row in apps), "invalid snapshot app")
    for row in apps:
        validate_approved_metadata(row)
    metrics = files["metrics"]["apps"]
    require(len(metrics) == count and {row["reference"]: row["id"] for row in metrics} == refs, "metric identities mismatch")
    for row in metrics:
        for field in ("stars", "forks", "trending", "stars_prev_7d"):
            require(row[field] is None or integer(row[field]), f"invalid {field} metric")
        require(row["commit"] is None or iso_date(row["commit"]), "invalid last commit date")
        require(row["archived"] is None or type(row["archived"]) is bool, "invalid archived flag")
        require(type(row["stale"]) is bool, "invalid stale flag")
        expected = row["stars"] - row["stars_prev_7d"] if row["stars"] is not None and row["stars_prev_7d"] is not None and not row["stale"] else None
        delta = row["stars_delta_7d"]
        require((delta is None or type(delta) is int) and delta == expected, "invalid seven-day delta")
    require(sum(row["commit"] is None for row in metrics) * 100 <= count, "more than 1% of apps lack a last commit date")

    ai_refs = set()
    for row in files["ai_signals"]["apps"]:
        ref = row["reference"]
        require(ref in refs and ref not in ai_refs, "invalid or duplicate AI reference")
        ai_refs.add(ref)
        confidence = row["confidence"]
        require(type(confidence) in (int, float) and 0 <= confidence <= 1, "invalid AI confidence")
        require(type(row["assisted"]) is bool and row["assisted"] == (confidence >= 0.5) and iso_time(row["checked_at"]), "invalid AI state")
        evidence = row["evidence"]
        require(isinstance(evidence, list) and all(isinstance(item, dict) and type(item.get("weight")) in (int, float) and item["weight"] in (0.3, 0.6, 1.0) and isinstance(item.get("type"), str) and isinstance(item.get("detail"), str) and isinstance(item.get("url"), str) for item in evidence), "invalid AI evidence")
        require(confidence == min(1.0, round(sum(item["weight"] for item in evidence), 1)), "AI evidence and confidence mismatch")

    history = files["history"]
    days = history["days"]
    require(history["window_days"] == 90 and isinstance(days, dict) and 0 < len(days) <= 90, "invalid history window")
    for day, values in days.items():
        require(iso_date(day) and isinstance(values, dict), "invalid history day")
        require(all(REFERENCE.fullmatch(ref) and integer(stars) for ref, stars in values.items()), "invalid star history")
    require(date.fromisoformat(max(days)) - date.fromisoformat(min(days)) < timedelta(days=90), "history extends beyond retention window")
    for role in ("tags", "alternatives"):
        require(taxonomy(files[role], role) == files[role], "invalid or noncanonical taxonomy")


def package(files, version, built):
    require(isinstance(version, str) and VERSION.fullmatch(version) and iso_time(built), "invalid manifest version or built timestamp")
    date.fromisoformat(version[:10].replace(".", "-"))
    count = len(files["apps"]["entries"])
    validate(files, count)
    objects = {role: encode(value) for role, value in files.items()}
    manifest = {
        "schema_version": 1, "version": version, "built": built, "count": count,
        "files": {
            role: {"key": f"v1/snapshot/{version}/{FILENAMES[role]}", "sha256": digest(objects[role])}
            for role in FILENAMES
        },
    }
    return manifest, objects
