# HHF app catalogue data publisher

The publisher refreshes public GitHub repository facts and development evidence for a reviewed catalogue baseline. GitHub Actions runs every six hours at minute 17 UTC. A manual run can refresh the full catalogue or recheck one baseline app. The live catalogue input, generated snapshots, and publisher checkpoints live in a private Cloudflare R2 bucket.

## Private baseline

Upload a reviewed UTF-8 JSON object to `v1/input/baseline.json` in the target bucket. Build it from the site's approved seed, keeping its stable `id` and `reference` for each included app. Review source permissions and retain provenance with the approved seed. The publisher reads this private baseline on each run.

| Field | Input |
| --- | --- |
| `apps.entries` | Array of named app records. Each app has a positive integer `id`, unique `reference` with 2–64 ASCII letters, digits or hyphens, and a `name`. Include approved metadata such as `repository`, `website`, `description`, `source`, `license`, `language`, `sso`, `platform`, `tags`, `alternatives`, `created`, and icon hints when available. `tags` and `alternatives` use integer IDs. |
| `metrics.apps` | Array of approved initial metrics keyed by positive integer `id`. The publisher accepts `stars`, `forks`, `commit` (`YYYY-MM-DD`), `trending`, and `archived` as initial values. |
| `tags`, `alternatives` | Arrays of named `{"id": 1, "label": "Example"}` rows. Tags must also produce distinct ASCII slugs. References to missing labels stay unresolved. |
| `approved_additions` | Optional private review object written by the publisher when more than 25 new entries need review. |

Production requires at least 500 apps and baseline metrics for at least 90% of them. After the first snapshot, count remains within 5% of the live snapshot. At most 1% of apps can lack a last-commit date. Preserve unknown values as JSON `null`. GitHub is authoritative for current public repository facts. An inaccessible or non-GitHub repository keeps its previously known metrics with `stale: true`; trending keeps its last supplied value. Staff curation belongs to the catalogue application.

Include descriptions of up to 300 characters only after provenance review. For an approved icon already uploaded to private R2, include its optional `icon_key` in the app row as `v1/icons/<reference>/<sha256>.<png|svg|webp>`. Keep the source and licence record alongside the reviewed input. The catalogue generates expiring URLs from the key.

## Snapshot contract

`v1/manifest.json` contains `schema_version: 1`, `version` (`YYYY.MM.DD.N`), `built` (UTC ISO 8601), `count`, and `files`. Each role points to `{"key": "v1/snapshot/<version>/<filename>", "sha256": "<lowercase digest>"}`. Digests are computed over the exact UTF-8 response bytes. All objects have `Content-Type: application/json` and `Cache-Control: private, no-store`.

| Role | File | JSON shape |
| --- | --- | --- |
| `apps` | `apps.json` | `{"entries": [...]}` with named fields and upstream integer IDs. |
| `metrics` | `metrics.json` | `{"apps": [...]}`. Each row has `id`, `reference`, `stars`, `forks`, `commit` (`YYYY-MM-DD` or `null`), `trending`, `archived`, `stale`, `stars_prev_7d` and `stars_delta_7d`. The seven-day comparison uses the exact UTC date and remains `null` for a missing sample. |
| `tags` | `tags.json` | Array of `{id, label}` rows. |
| `alternatives` | `alternatives.json` | Array of `{id, label}` rows. |
| `ai_signals` | `ai-signals.json` | `{"apps": [...]}` with `reference`, `assisted`, `confidence`, `evidence` and UTC `checked_at`. Each evidence item has `type`, `weight`, `detail`, and `url`. An unchecked app has no row. A completed check with no evidence has `assisted: false` and an empty list. |
| `history` | `history.json` | `{"window_days": 90, "days": {"YYYY-MM-DD": {"app-reference": 42}}}` with the latest observed stars per app per UTC date. |

AI evidence uses the most recent 200 default-branch commits, known tooling files, explicit development topics, and explicit development declarations in `README.md`. One entry per evidence class contributes to the confidence capped at 1. The `assisted` threshold is 0.5. Feature phrases and unchecked repositories never produce evidence. Scheduled runs check up to 200 apps per UTC week, with a 30-day cooldown per app. A targeted recheck bypasses the cooldown.

## Hosted setup

1. Protect the default branch. Create the `r2-publish` GitHub environment, limit deployment to that branch, and add its secrets `HHF_APPS_GITHUB_TOKEN`, `R2_ACCOUNT_ID`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY`. Use a GitHub credential with public repository read access only and an R2 Object Read & Write credential scoped to the target bucket. GitHub Actions tokens have `contents: read` permissions. Fork pull requests run credential-free fixture checks.
2. Use a private staging bucket and its own secrets for the first hosted run. Dispatch **Publish catalogue snapshot** from the default branch with `reference` blank. Verify that the catalogue consumes the resulting manifest and reports the expected count and digest. Switch the environment to the intended bucket after verifying staging.
3. To recheck an app, dispatch from the same branch with its approved baseline `reference`. The app must resolve to a public GitHub repository. A failed lookup or validation leaves the live pointer unchanged.

The CLI takes the same variables from the environment. In an activated Python 3.13 virtual environment, install the publisher and run it with `python -m pip install --only-binary=:all: --require-hashes -r requirements.txt` and `python publisher.py`. `python publisher.py --reference sample-app` refreshes that app and publishes through the same gate. Use staging credentials when running manually and keep a single producer active per bucket.

## Operations

- The publisher verifies the current manifest and file hashes before using them, then validates and uploads all candidate files with create-only writes. It reads each back to verify the SHA-256. The last write atomically replaces the pointer only if its ETag still matches. Matching file digests skip publishing. Retain immutable snapshots for the desired rollback horizon.
- Failure before the pointer update leaves the last good snapshot readable. Retry with **Run workflow** after the GitHub rate-limit reset or after resolving the failure. Partial progress under `v1/state/` resumes for up to two days with the same baseline and live pointer. After a bad upstream response, remove the affected checkpoint only after diagnosing the source if a fresh query is required. An interrupted immutable upload can leave unreferenced objects, and a rerun allocates a fresh version.
- An increase over 25 new entries stages immutable files and writes a review object under `v1/review/<version>.json`. After reviewing against the private baseline, copy the complete review object into `approved_additions` in that baseline and rerun. Approval includes the exact reference set and the version used for comparison.
- Monitor schedule runs and the age of the live `built` timestamp. A run reporting `unchanged` still succeeded. Investigate a pointer older than 24 hours. GitHub can disable schedules in inactive public repositories and can delay runs. Check workflow status and use a manual dispatch to recover. [GitHub schedule events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- To roll back, pause publishing and select a known published version from the run history. Verify `v1/manifests/<version>.json` against every immutable object, then replace the private pointer using its current ETag. Catalogue consumer rollback follows its operator procedure. Resume publishing after resolving the cause, since the baseline can otherwise produce the superseded result again.

## Contract fixture

Run `python -m unittest discover -s tests` using Python 3.13. Tests need no dependencies, credentials, or network. `python -m tests.generate_fixture tests/fixtures` regenerates the synthetic manifest and six fixtures beneath `tests/fixtures/v1`. The publisher and the consumer can verify the same bytes and checksums. [Cloudflare R2 conditional requests](https://developers.cloudflare.com/r2/api/s3/conditional-operations/)
