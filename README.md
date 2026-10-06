<div align="center">

<img src="https://raw.githubusercontent.com/nikships/claude-artifact-cli/main/assets/header.png" alt="claude-artifact-cli: publish Claude Artifacts from your terminal" width="100%" />

# claude-artifact-cli

Publish, update, list and inspect Claude Artifacts from the terminal, using the Claude Code login already on your machine.

[![PyPI](https://img.shields.io/pypi/v/claude-artifact-cli?style=for-the-badge)](https://pypi.org/project/claude-artifact-cli/)
[![Python](https://img.shields.io/pypi/pyversions/claude-artifact-cli?style=for-the-badge)](https://pypi.org/project/claude-artifact-cli/)
[![CI](https://img.shields.io/github/actions/workflow/status/nikships/claude-artifact-cli/publish.yml?branch=main&style=for-the-badge)](https://github.com/nikships/claude-artifact-cli/actions)
[![License](https://img.shields.io/github/license/nikships/claude-artifact-cli?style=for-the-badge)](https://github.com/nikships/claude-artifact-cli/blob/main/LICENSE)
[![Stars](https://img.shields.io/github/stars/nikships/claude-artifact-cli?style=for-the-badge)](https://github.com/nikships/claude-artifact-cli/stargazers)

</div>

## What is this?

`claude-artifact` publishes an HTML page (plus any CSS, JS and images it uses) as a [Claude Artifact](https://claude.ai/code/artifacts) and prints the link. It talks to the same API the Claude Code `Artifact` tool uses and reuses the claude.ai login from `claude /login`. No API key, no config file, no dependencies beyond Python 3.10+.

Use it where the built-in tool can't reach: other coding agents (Droid, Codex, Cursor, OpenCode), subagents, shell scripts, Makefiles, git hooks and CI jobs.

> [!NOTE]
> Unofficial and not affiliated with Anthropic. It uses a private, undocumented API that can change or break without notice.

## Quick Start

```bash
uv tool install claude-artifact-cli     # or: pipx install claude-artifact-cli
claude-artifact whoami                  # confirms the Claude Code login works
claude-artifact publish report.html --favicon 📊
# https://claude.ai/code/artifact/1c5586a4-acec-4266-a9aa-03dfa0912b43
```

Run it once without installing:

```bash
uvx --from claude-artifact-cli claude-artifact publish report.html
```

## Commands

```
claude-artifact publish FILE|DIR [options]   publish or update an artifact
claude-artifact status [PATH] [--slug S | --url U]
                                            compare local files with what's live
claude-artifact list [--scope mine|shared|all] [--limit N]
claude-artifact read SLUG_OR_URL [-o FILE]  metadata + published file manifest
claude-artifact whoami                      check that auth works
```

The URL goes to stdout and progress goes to stderr, so `URL=$(claude-artifact publish page.html -q)` captures only the link. Add `--json` to any command for raw output. Exit codes: `0` success, `1` API error, `2` auth error, `3` version conflict (HTTP 409), `130` interrupted.

### publish

```bash
# new artifact; the title comes from the page's <title>
claude-artifact publish dashboard.html --favicon 📊 --description "Q3 numbers"

# update in place, same URL; published files you don't pass are kept
claude-artifact publish dashboard.html --url https://claude.ai/code/artifact/<slug>
claude-artifact publish dashboard.html --slug <slug>

# update, and make these files the whole artifact (anything else is removed)
claude-artifact publish dashboard.html --slug <slug> --mode replace --file app.css

# multi-file page
claude-artifact publish index.html \
  --root ./site \
  --file app.css --file app.js --file img/logo.png

# rename a file's published path, and drop one
claude-artifact publish index.html --slug <slug> \
  --file styles.css=src/main.css \
  --remove old.js
```

| Option | Meaning |
|--------|---------|
| `--slug`, `--url` | Update this artifact. Without one, a publish creates a new artifact (a directory with `.artifact.json` updates its own). |
| `--title` | Defaults to the page's `<title>`, else the file or directory name. |
| `--favicon` | Emoji for the tab. Set it on a first publish. |
| `--description` | One-sentence subtitle. |
| `--icon`, `--label` | Generic icon word; short name for this publish. |
| `--file PUB[=SRC]` | Supporting file. Without `=`, published and source paths match. Repeatable. Not allowed with a directory. |
| `--root` | Base directory for `--file` sources. Published paths are unchanged. Not allowed with a directory. |
| `--remove PATH` | Delete a published file. Needs an existing artifact and patch mode; with no `--mode`, `--remove` uses patch. |
| `--mode patch\|replace` | `patch` overlays the manifest onto the base version, so published files you don't pass are kept. Default when updating a page. `replace` makes the manifest the whole artifact, so any published file you leave out is removed. Default for a new artifact and for a directory. |
| `--base-version` | The version this publish was made against. If a newer version is live, the publish is refused with exit code `3`. Defaults to the version in a directory's `.artifact.json` (when publishing to the slug saved there). Without one, patch uses the live version and replace is not checked, so a concurrent publish goes undetected. |
| `--force` | Publish even if a newer version is live, discarding it. |
| `-q`, `--quiet` | Print only the URL. |

`--json` prints the deploy response with the artifact `url` added: `{"url", "slug", "version", …}`.

### Publish a directory

```bash
claude-artifact publish ./site --favicon 📊   # new artifact; writes ./site/.artifact.json
claude-artifact publish ./site                # later: updates the same artifact
```

Publishes every file under the directory, recursively, skipping dotfiles and dot-directories. `index.html` is required at the top level; symlinks are refused. A directory defaults to `--mode replace`: it is the whole artifact, so a file deleted locally is deleted on the next publish.

After each successful publish the CLI writes `DIR/.artifact.json`:

```json
{ "slug": "…", "url": "https://claude.ai/code/artifact/…", "version": "…", "title": "…" }
```

It is never published (dotfile). The next `publish DIR` updates that slug without `--slug` and sends the saved version as the base version, so if someone else published in between, the publish fails with exit code `3` instead of overwriting their version. Commit it or ignore it as you prefer.

### status

```bash
claude-artifact status                          # current directory, slug from .artifact.json
claude-artifact status ./site
claude-artifact status page.html --slug <slug>  # compares page.html with the published index.html only
claude-artifact status ./site --json
```

```
https://claude.ai/code/artifact/<slug>
  live version  1789541006-9f63
  base version  1789540112-04aa  (someone published since - merge first)
  same          app.css
  local-only    img/new.png
  changed       index.html
  remote-only   old.js
```

Compares the sha256 of each local file with the published manifest. The base version line appears only for a directory with `.artifact.json`, and reads `(up to date)` when it matches the live version. `--json` prints `{"slug", "url", "live", "base", "behind", "files": [{"state", "path"}]}`. `PATH` defaults to `.`; `--slug`/`--url` override `.artifact.json`.

On exit code `3` from `publish`: run `status`, merge the other version's changes into your files, then publish again with `--base-version <live>`, or with `--force` to discard the other version.

### list and read

```bash
claude-artifact list --scope shared
claude-artifact read <slug-or-url>          # title, version, role, file manifest
claude-artifact read <slug> -o meta.json   # also save the metadata JSON
```

`read` returns metadata, paths, sizes, content types and sha256 hashes, not the page bytes. Short-lived tokens in responses (`assetToken`, `subscriptionToken`, and `__frame_t=` on `list` thumbnail URLs) are replaced with `[redacted]` before they reach stdout or `-o`.

## Auth

The token is looked up in this order:

1. `--token`
2. `$CLAUDE_ARTIFACT_TOKEN`, then `$CLAUDE_CODE_ARTIFACTS_API_TOKEN`
3. macOS Keychain, generic password service `Claude Code-credentials`
4. `~/.claude/.credentials.json`

It must be the **claude.ai OAuth token** from `claude /login`. The artifacts routes reject an `ANTHROPIC_API_KEY` with a 401. The token is short-lived; Claude Code refreshes it on launch and this tool re-reads it on every run, so there is nothing to rotate. `whoami` shows only the token's length and last few characters.

## Agent skill

[`skills/claude-artifact-cli/SKILL.md`](skills/claude-artifact-cli/SKILL.md) teaches any coding agent (Droid, Codex, Cursor, OpenCode, Claude Code subagents) when and how to use the CLI. Install it in the shared agent skills directory:

```bash
mkdir -p ~/.agents/skills/claude-artifact-cli
curl -fsSL https://raw.githubusercontent.com/nikships/claude-artifact-cli/main/skills/claude-artifact-cli/SKILL.md \
  -o ~/.agents/skills/claude-artifact-cli/SKILL.md
```

## Troubleshooting

| Symptom | Cause |
|---------|-------|
| `401 unauthorized` | An API key instead of the OAuth token, or an expired login. Start Claude Code once or run `claude /login`. |
| `removing a file needs --mode patch` | `--remove` with `--mode replace`. In replace mode, leave the file out instead. |
| `removing a file needs --slug/--url` | `--remove` on a new artifact. |
| `404 … isn't yours to update` | Wrong slug, deleted artifact, or another org. |
| `HTTP 409: a newer version (live version …)`, exit code `3` | Someone published since your base version. Run `claude-artifact status`, merge, then republish with `--base-version <live>`, or pass `--force`. |
| CSS, JS or images gone after an update | Published with `--mode replace` (or with 0.1.x, where replace was the default) without passing them. Republish them. |
| `no index.html in DIR` | A directory publish needs `index.html` at its top level. |
| `… is a symlink; copy the file in instead` | Directory publishes refuse symlinks. |
| `429 … daily publish cap` | Plan limit. Resets at UTC midnight. |

## The protocol

Reverse-engineered from the Claude Code CLI v2.1.273 binary and checked against v2.1.287, where artifacts are called *frames*. Every call goes to `api.anthropic.com`, even though the artifact is served from `claude.ai`.

```
POST /api/frame/deploy/direct     publish
POST /api/frame/deploy/prepare    content-hash preflight (bodies over ~15 MB)
POST /api/frame/upload            stage blobs the preflight asked for
GET  /api/frame/{slug}?via=model_read   metadata + file manifest
GET  /api/frame/read/{slug}       ownership / sharing status
GET  /api/frame/frames?limit=N    list
```

Headers:

```
Authorization: Bearer <claude.ai OAuth access token>
anthropic-beta: oauth-2025-04-20
Accept: application/json, application/vnd.ant.frame-refusal+json
X-Frame-CP: go
X-Frame-Surface: code
X-Frame-Platform: cli
X-Frame-Client-Version: 2.1.287
```

Only `Authorization` and `anthropic-beta` are load-bearing.

### Deploy body

```jsonc
{
  "slug": "…",                       // omit to create a new artifact
  "title": "My Page",
  "favicon": "📊",
  "description": "…",
  "mode": "replace",                 // or "patch" (requires baseVersion)
  "baseVersion": "1789541006-9f63",  // refused with 409 if a newer version is live
  "force": true,                     // publish anyway
  "manifest": {
    "index.html": { "content": "<!doctype html>…", "contentType": "text/html" },
    "logo.png":   { "sha256": "<64 hex>",          "contentType": "image/png" },
    "old.js":     null
  }
}
```

A manifest value with `content` inlines the file; one with `sha256` references a blob already staged via `/upload`; `null` deletes that path (patch mode only).

**Wire encoding.** These content types go as raw UTF-8 strings; everything else is base64: `text/html`, `text/css`, `text/plain`, `text/markdown`, `text/csv`, `text/javascript`, `application/javascript`, `application/json`, `application/manifest+json`, `application/xml`, `text/xml`, `image/svg+xml`.

Response: `{"slug", "version", "read", "shared", "kind"}`. The page lives at `https://claude.ai/code/artifact/<slug>`.

**Modes and conflicts.** `replace` makes the manifest the whole artifact; `patch` overlays it onto `baseVersion`, keeping paths it doesn't name. Both modes honor `baseVersion`: if the live version is newer, the deploy is refused with

```
HTTP 409
{"conflict": true, "live": "<live version>", …}
```

`patch` requires a `baseVersion`; when the caller has none, the CLI reads the live `ver` from `GET /api/frame/{slug}?via=model_read` and uses that, which cannot detect a concurrent publish. The CLI exits `3` on a 409.

### Large publishes

Over ~15 MB inline, the client switches to `prepare` → `upload` → `deploy`. `prepare` takes `{slug?, shas:[…]}` and answers `{slug, missing:[…]}`. The missing blobs go to `/upload` in batches of at most 15 MB, and the final deploy references them by `sha256`: the hex sha256 of the raw file bytes.

### Not supported

- **Reading published bytes.** Pages are served from a separate sandboxed host behind a short-lived asset token, and the `claude.ai/code/artifact/…` URL is a login-walled app shell, so fetching it returns no page content. `read` returns the manifest, and `status` compares local files with it by sha256. Tokens in responses (`assetToken`, `subscriptionToken`, `__frame_t`) are redacted to `[redacted]`.
- **Delete and pin.** Both go through a relay-only route that isn't reachable on the direct path. Use `/artifacts` in Claude Code, or claude.ai.
- **Capabilities.** Runtime capability declarations (`capabilities`, `contract`) are accepted by the API but not exposed as flags.
- **Comments, watching, thumbnails.**

## Project Structure

```
.github/
└── workflows/
    └── publish.yml          lint, tests, auto-version, PyPI publish, GitHub release
assets/
└── header.png
skills/
└── claude-artifact-cli/
    └── SKILL.md             agent skill
src/
└── claude_artifact_cli/
    ├── __init__.py          version
    ├── __main__.py          python -m claude_artifact_cli
    ├── api.py               frame API client
    ├── auth.py              token lookup
    └── cli.py               argparse entry point
tests/                       unit tests (offline; transport mocked)
AGENTS.md
LICENSE
README.md
pyproject.toml
```

## Documentation

| Resource | Description |
|----------|-------------|
| [SKILL.md](skills/claude-artifact-cli/SKILL.md) | Agent skill: when to use the CLI and how |
| [AGENTS.md](AGENTS.md) | Constraints and commands for coding agents working in this repo |
| [publish.yml](.github/workflows/publish.yml) | CI and release pipeline |
| [The protocol](#the-protocol) | Endpoints, headers and deploy body |

## Contributing

Issues and pull requests are welcome. Keep the package dependency-free and run `uvx ruff check .` and `PYTHONPATH=src python -m unittest discover -s tests -v` before pushing. Every merge to `main` that touches `src/` or `pyproject.toml` is released to PyPI automatically with a patch bump.

<a href="https://github.com/nikships/claude-artifact-cli/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=nikships/claude-artifact-cli" />
</a>

## License

[Apache-2.0](LICENSE)

---

<div align="center">

[![Star History Chart](https://api.star-history.com/svg?repos=nikships/claude-artifact-cli&type=Date)](https://star-history.com/#nikships/claude-artifact-cli&Date)

</div>
