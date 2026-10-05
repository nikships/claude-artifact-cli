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

Use it where the built-in tool can't reach: shell scripts, Makefiles, git hooks, CI jobs, subagents and other coding agents.

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
claude-artifact publish FILE [options]   publish or update an artifact
claude-artifact list [--scope mine|shared|all]
claude-artifact read SLUG_OR_URL         metadata + published file manifest
claude-artifact whoami                   check that auth works
```

The URL goes to stdout and progress goes to stderr, so `URL=$(claude-artifact publish page.html -q)` captures only the link. Add `--json` to any command for raw output. Exit codes: `0` success, `1` API error, `2` auth error.

### publish

```bash
# new artifact; the title comes from the page's <title>
claude-artifact publish dashboard.html --favicon 📊 --description "Q3 numbers"

# update in place, same URL
claude-artifact publish dashboard.html --url https://claude.ai/code/artifact/<slug>
claude-artifact publish dashboard.html --slug <slug>

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
| `--slug`, `--url` | Update this artifact. Without one, every publish creates a new artifact. |
| `--title` | Defaults to the page's `<title>`. |
| `--favicon` | Emoji for the tab. Set it on a first publish. |
| `--description` | One-sentence subtitle. |
| `--icon`, `--label` | Generic icon word; short name for this publish. |
| `--file PUB[=SRC]` | Supporting file. Without `=`, published and source paths match. Repeatable. |
| `--root` | Base directory for `--file` sources. Published paths are unchanged. |
| `--remove PATH` | Delete a published file. Implies `--mode patch`. |
| `--mode replace\|patch` | `replace` (default) makes the manifest the whole artifact, so omitting a file removes it. `patch` overlays onto the current version. |
| `--base-version` | Expected current version for patch mode. Looked up automatically. |
| `--force` | Overwrite a newer version someone else published. |
| `-q`, `--quiet` | Print only the URL. |

### list and read

```bash
claude-artifact list --scope shared
claude-artifact read <slug-or-url>          # title, version, role, file manifest
claude-artifact read <slug> -o meta.json   # also save the metadata JSON
```

`read` returns metadata, paths, sizes, content types and sha256 hashes, not the page bytes.

## Auth

The token is looked up in this order:

1. `--token`
2. `$CLAUDE_ARTIFACT_TOKEN`, then `$CLAUDE_CODE_ARTIFACTS_API_TOKEN`
3. macOS Keychain, generic password service `Claude Code-credentials`
4. `~/.claude/.credentials.json`

It must be the **claude.ai OAuth token** from `claude /login`. The artifacts routes reject an `ANTHROPIC_API_KEY` with a 401. The token is short-lived; Claude Code refreshes it on launch and this tool re-reads it on every run, so there is nothing to rotate. `whoami` shows only the token's length and last few characters.

## Agent skill

[`skills/claude-artifact-cli/SKILL.md`](skills/claude-artifact-cli/SKILL.md) teaches Claude Code, Droid, Codex and other agents when and how to use the CLI. Install it for Claude Code:

```bash
mkdir -p ~/.claude/skills/claude-artifact-cli
curl -fsSL https://raw.githubusercontent.com/nikships/claude-artifact-cli/main/skills/claude-artifact-cli/SKILL.md \
  -o ~/.claude/skills/claude-artifact-cli/SKILL.md
```

## Troubleshooting

| Symptom | Cause |
|---------|-------|
| `401 unauthorized` | An API key instead of the OAuth token, or an expired login. Start Claude Code once or run `claude /login`. |
| `400 … null deletes a path in mode:"patch"` | `--remove` without patch mode. |
| `404 … isn't yours to update` | Wrong slug, deleted artifact, or another org. |
| `409 … newer version exists` | Someone else published. Re-read, merge, retry, or pass `--force`. |
| `429 … daily publish cap` | Plan limit. Resets at UTC midnight. |

## The protocol

Reverse-engineered from the Claude Code CLI v2.1.273 binary, where artifacts are called *frames*. Every call goes to `api.anthropic.com`, even though the artifact is served from `claude.ai`.

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
X-Frame-Client-Version: 2.1.273
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
  "baseVersion": "1789541006-9f63",
  "force": true,
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

### Large publishes

Over ~15 MB inline, the client switches to `prepare` → `upload` → `deploy`. `prepare` takes `{slug?, shas:[…]}` and answers `{slug, missing:[…]}`. The missing blobs go to `/upload` in batches of at most 15 MB, and the final deploy references them by `sha256`: the hex sha256 of the raw file bytes.

### Not supported

- **Reading published bytes.** Pages are served from a separate sandboxed host behind a short-lived asset token.
- **Delete and pin.** Both go through a relay-only route that isn't reachable on the direct path. Use `/artifacts` in Claude Code, or claude.ai.
- **Capabilities.** Runtime capability declarations (`capabilities`, `contract`) are accepted by the API but not exposed as flags.
- **Comments, watching, thumbnails.**

## Project Structure

```
.github/
└── workflows/
    └── publish.yml          lint, auto-version, PyPI publish, GitHub release
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

Issues and pull requests are welcome. Keep the package dependency-free and run `uvx ruff check .` before pushing. Every merge to `main` that touches `src/` or `pyproject.toml` is released to PyPI automatically with a patch bump.

<a href="https://github.com/nikships/claude-artifact-cli/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=nikships/claude-artifact-cli" />
</a>

## License

[Apache-2.0](LICENSE)

---

<div align="center">

[![Star History Chart](https://api.star-history.com/svg?repos=nikships/claude-artifact-cli&type=Date)](https://star-history.com/#nikships/claude-artifact-cli&Date)

</div>
