# AGENTS.md

Zero-dependency Python CLI (`claude-artifact`) for Anthropic's private Claude Artifacts "frame" API. PyPI package `claude-artifact-cli`, import package `claude_artifact_cli`.

## Layout

- `src/claude_artifact_cli/cli.py`: argparse entry point `main`; one `cmd_*` function per subcommand; directory publishing (typed included) and its `.artifact.json` state file; `read --path`; `pull` and its three-way merge `_merge_plan`; `_destination`/`_write`, which refuse unsafe paths and symlinks; `status` comparison.
- `src/claude_artifact_cli/commands.py`: `cmd_*` for comments, `db`, `assets`, `types`/`create`, `contract`, `share`, `versions`, `rename`, `pin`/`unpin`, `delete` and `watch`. Their parsers are in `cli._add_more`.
- `src/claude_artifact_cli/api.py`: `FrameClient` HTTP transport (JSON, or raw bytes for asset uploads), one method per route, `Asset`, `Copy` (server-side file copies), manifest wire encoding, `EXTENSION_TYPES`/`SERVED_TYPES` (Claude Code's extension map and served types), large-publish staging, `publish` splitting manifests over `MANIFEST_MAX` entries into chained patches, `_deploy` (attempt id, re-send, and Claude Code's retry rules), `ConflictError` (409), `NetworkError` (no answer), token `redact`, `error_code`. `FileReader` reads published bytes and uploaded assets from the content host and verifies them against their sha256; `clean_path` validates and quotes a published path; `strip_served_page` recovers the page source from the served copy; `slug_from` accepts URLs, UUIDs and base58 short ids.
- `src/claude_artifact_cli/update.py`: daily PyPI check in a background thread, detached `uv tool upgrade` / `pipx upgrade` once the command finishes (a pip install gets a notice), and `cmd_update`. State in `~/.config/claude-artifact-cli/update.json`.
- `src/claude_artifact_cli/auth.py`: token lookup (flag → env → macOS Keychain → `~/.claude/.credentials.json`).
- `src/claude_artifact_cli/__init__.py`: `__version__`, the single version source (hatch reads it).
- `tests/`: offline unit tests; the transport is mocked, nothing hits the network.
- `skills/claude-artifact-cli/SKILL.md`: agent skill shipped with the repo. Update it when flags, behavior or errors change.
- `.github/workflows/publish.yml`: lint, tests, auto-version, build, PyPI publish, GitHub release.

## Commands

```bash
uvx ruff check .                                        # the only CI lint gate
PYTHONPATH=src python -m unittest discover -s tests -v  # unit tests; CI runs them on Linux, macOS, Windows x Python 3.10, latest
uv build && uvx twine check --strict dist/*             # packaging check
uv run --with . claude-artifact --help                  # smoke test
uv run --with . claude-artifact whoami                  # live auth check; needs `claude /login`
```

Validate changes with ruff, the unit tests, a build, and `--help` on affected subcommands. The tests are offline and mock the transport; add or update them with any behavior change. `whoami`, `list`, `read` (with or without `--path`), `status`, `pull`, `versions`, `comments list`, `db get|list|query|whoami|profiles`, `assets list|get`, `types`, `contract` (with or without `--defs`) and `share` without flags hit the live API, are read-only there, and are safe; `pull` writes only to its local directory. Everything else writes: `publish`, `create`, `rename`, `pin`/`unpin`, `delete`, `share` with flags, `comments reply|resolve`, `db` writes and `assets upload|delete|copy`. Run those only on artifacts you created for testing, or when the user asked.

## Hard constraints

- Standard library only. Do not add runtime dependencies.
- `requires-python = ">=3.10"`. Do not use newer syntax.
- An update check or upgrade never changes a command's stdout or exit code, never runs when `CI` is set, and never upgrades a plain pip install. `tests/__init__.py` sets `CLAUDE_ARTIFACT_NO_UPDATE_CHECK` so tests never reach PyPI.
- Never print, log or commit a token. `whoami` shows only its length and last characters; keep it that way. API responses carry short-lived tokens (`assetToken`, `subscriptionToken`, `__frame_t=` in thumbnail URLs); run them through `api.redact` before they reach stdout or disk.
- The `assetToken` never leaves `FileReader`: not in output, files, `.artifact.json`, error messages or exceptions. Raw boot responses stay inside `api.py`.
- Content-host requests (`*.frame.claudeusercontent.com`) carry only the asset token in the query and a non-default `User-Agent`. Never send them the OAuth token or an `Authorization` header.
- `pull` and `read --out-dir` write only clean relative paths under the target directory and never write through a symlink. Keep both checks.
- stdout carries only results (URL, JSON, tables, file bytes, saved paths, the pulled directory). Progress and errors go to stderr. `-q` must leave exactly the URL on stdout (for `pull`, the directory).
- Exit codes: `0` ok, `1` API error, `2` auth error, `3` version conflict (HTTP 409), `130` interrupt.
- Only `Authorization` and `anthropic-beta: oauth-2025-04-20` headers are load-bearing. Keep the `X-Frame-*` headers and `CLIENT_VERSION` mirroring the Claude Code CLI.
- `TEXT_TYPES` in `api.py` must match the Claude Code client's set exactly; other content types are base64 on the wire. `EXTENSION_TYPES` and `SERVED_TYPES` mirror Claude Code's extension map and served set the same way.
- A publish re-send (`resend=1`) reuses the first send's `attempt_id` and only goes for a named artifact. Never re-send a create blind, and never retry a plan-cap 429.
- Every route in "The protocol" was verified against the live API with the OAuth token. Do not guess routes or bodies: read them out of the Claude Code bundle (`strings` on `~/.local/share/claude/versions/<v>`, search `/api/frame/`) and verify live on a scratch artifact.
- `comments reply` posts as Claude and only on threads a person sent to Claude; keep the check that refuses other threads before posting, and keep the client id that makes a retry idempotent.
- `delete` refuses without `--yes`. `share` sends `If-Match` with the version it read.
- Protocol changes must update the "The protocol" section in `README.md`.

## Releases

Every push to `main` that touches `src/` or `pyproject.toml` releases automatically, once Ruff and the test matrix pass:

1. If `__version__` is not on PyPI yet, CI publishes it as-is. Otherwise CI bumps the patch, commits `chore(release): vX.Y.Z [skip ci]` to `main`, and publishes that.
2. Publishing uses PyPI trusted publishing (environment `pypi`, workflow `publish.yml`). No tokens.
3. CI then creates tag `vX.Y.Z` and a GitHub release with the wheel and sdist.

Consequences:

- Do not bump the patch version by hand. Edit `__version__` only for a minor or major bump.
- CI pushes to `main`. Pull before pushing again after any release.
- Docs-only or skill-only changes do not release.
