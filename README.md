<div align="center">

<img src="https://raw.githubusercontent.com/nikships/claude-artifact-cli/main/assets/header.png" alt="claude-artifact-cli: publish Claude Artifacts from your terminal" width="100%" />

# claude-artifact-cli

Publish, update, pull, list and inspect Claude Artifacts from the terminal, using the Claude Code login already on your machine.

[![PyPI](https://img.shields.io/pypi/v/claude-artifact-cli?style=for-the-badge)](https://pypi.org/project/claude-artifact-cli/)
[![Python](https://img.shields.io/pypi/pyversions/claude-artifact-cli?style=for-the-badge)](https://pypi.org/project/claude-artifact-cli/)
[![CI](https://img.shields.io/github/actions/workflow/status/nikships/claude-artifact-cli/publish.yml?branch=main&style=for-the-badge)](https://github.com/nikships/claude-artifact-cli/actions)
[![License](https://img.shields.io/github/license/nikships/claude-artifact-cli?style=for-the-badge)](https://github.com/nikships/claude-artifact-cli/blob/main/LICENSE)
[![Stars](https://img.shields.io/github/stars/nikships/claude-artifact-cli?style=for-the-badge)](https://github.com/nikships/claude-artifact-cli/stargazers)

</div>

## What is this?

`claude-artifact` publishes an HTML page (plus any CSS, JS and images it uses) as a [Claude Artifact](https://claude.ai/code/artifacts) and prints the link, and pulls an artifact's files back down to edit and republish. It talks to the same API the Claude Code `Artifact` tool uses and reuses the claude.ai login from `claude /login`. No API key, no config file, no dependencies beyond Python 3.10+.

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
claude-artifact pull SLUG_OR_URL [DIR] [--force] [-q]
                                            download the artifact's files into DIR
claude-artifact status [PATH] [--slug S | --url U]
                                            compare local files with what's live
claude-artifact list [--scope mine|shared|all] [--limit N]
claude-artifact read SLUG_OR_URL [-o FILE]  metadata + published file manifest
claude-artifact read SLUG_OR_URL --path P [--path P ... --out-dir DIR]
                                            a published file's exact bytes
claude-artifact whoami                      check that auth works
claude-artifact update                      upgrade to the latest release now
claude-artifact --version
```

The URL goes to stdout and progress goes to stderr, so `URL=$(claude-artifact publish page.html -q)` captures only the link. Add `--json` to `publish`, `status`, `list` or `read` for raw output. Exit codes: `0` success, `1` API error, `2` auth error, `3` version conflict (HTTP 409), `130` interrupted.

Wherever a command takes a slug or URL (`SLUG_OR_URL`, `--slug`, `--url`), it accepts any of:

- `https://claude.ai/code/artifact/<uuid>`
- `https://claude.ai/artifact/<short id>`, the form claude.ai and Claude Code's Artifact tool show
- a bare UUID or a bare short id

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
| `--title` | Defaults to the page's `<title>`, else the file or directory name (for a typed directory, the title saved in `.artifact.json`). |
| `--favicon` | Emoji for the tab. Set it on a first publish. |
| `--description` | One-sentence subtitle. |
| `--icon`, `--label` | Generic icon word; short name for this publish. |
| `--file PUB[=SRC]` | Supporting file. Without `=`, published and source paths match. Repeatable. Not allowed with a directory. |
| `--root` | Base directory for `--file` sources. Published paths are unchanged. Not allowed with a directory. |
| `--remove PATH` | Delete a published file. Needs an existing artifact and patch mode; with no `--mode`, `--remove` uses patch. |
| `--mode patch\|replace` | `patch` overlays the manifest onto the base version, so published files you don't pass are kept. Default when updating a page. `replace` makes the manifest the whole artifact, so any published file you leave out is removed. Default for a new artifact and for the first publish of a directory. A directory with `.artifact.json` patches (see below). |
| `--base-version` | The version this publish was made against. If a newer version is live, the publish is refused with exit code `3`. Defaults to the version in a directory's `.artifact.json` (when publishing to the slug saved there). Without one, patch uses the live version and replace is not checked, so a concurrent publish goes undetected. |
| `--force` | Publish even if a newer version is live, discarding it. |
| `-q`, `--quiet` | Print only the URL. |

`--json` prints the deploy response with the artifact `url` added: `{"url", "slug", "version", …}`.

### Publish a directory

```bash
claude-artifact publish ./site --favicon 📊   # new artifact; writes ./site/.artifact.json
claude-artifact publish ./site                # later: updates the same artifact
```

Publishes every file under the directory, recursively, skipping dotfiles and dot-directories. `index.html` is required at the top level (except for a typed artifact, below); symlinks are refused. The first publish of a directory replaces: the directory is the whole artifact. Once `.artifact.json` lists its files, `publish DIR` patches: it sends the directory's files and removes the ones deleted locally since the last pull or publish, leaving files the directory never held (dot paths, a type's files) alone. `--mode replace` forces a full replace.

After each successful publish (and each `pull`) the CLI writes `DIR/.artifact.json`:

```json
{
  "slug": "…",
  "url": "https://claude.ai/code/artifact/…",
  "version": "…",
  "title": "…",
  "files": { "index.html": "<sha256>", "app.css": "<sha256>" }
}
```

`files` maps each published path to the sha256 of what was published or pulled; `pull` uses it as the merge base. It is never published (dotfile). The next `publish DIR` updates that slug without `--slug` and sends the saved version as the base version, so if someone else published in between, the publish fails with exit code `3` instead of overwriting their version. Commit it or ignore it as you prefer.

**Typed artifacts.** An artifact made from an Artifact type (Slides, Design, …) holds files the type supplies, `index.html` among them. They stay fixed and `pull` leaves them out, marking the directory `"typed": true` in `.artifact.json`. `publish DIR` on a typed directory patches only the artifact's own files: no `index.html` is needed, a local `index.html` is refused, `--mode replace` is refused, and a file deleted locally since the last pull or publish is removed from the artifact. Start from `pull`; the directory must be linked to the artifact.

### pull

```bash
claude-artifact pull <slug-or-url>              # into ./<slug>
claude-artifact pull <slug-or-url> ./site
claude-artifact publish ./site                  # after editing: same artifact, pulled version as base
```

Downloads the live version's files into `DIR` (default: a directory named after the slug), writes `DIR/.artifact.json`, and prints `DIR` on stdout. stderr reports the version, files written and removed, local edits kept, and files left out: those the artifact's type supplies, and dot paths such as `.well-known/…`, which directories never hold (`read --path` still fetches them). `-q` silences it.

Pulling into a directory already linked to the same artifact merges file by file against the hashes in `.artifact.json`:

| Local | Live | Result |
|-------|------|--------|
| unchanged | changed | live version written |
| changed | unchanged | local edit kept |
| unchanged | deleted | file removed, with any directory left empty |
| untracked | not published | kept |
| changed | changed | conflict |

A file added or deleted counts as changed.

A conflict lists the files and writes nothing. To resolve it, copy your versions of those files out of the directory, pull again with `--force` (it takes the live version of conflicting files and still keeps your other edits), merge your changes back in, and publish. Or pull into another directory to compare by hand. A non-empty directory that isn't linked to this artifact is refused unless `--force`, which writes the artifact's files over it and leaves other files alone.

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

Compares the sha256 of each local file with the published manifest; for older artifacts whose manifest has no sha256, it fetches the file to compare. Files an Artifact type supplies, and dot paths, are ignored: directories never hold them. The base version line appears only for a directory with `.artifact.json`, and reads `(up to date)` when it matches the live version. When anything differs, stderr suggests `merge the live version in: claude-artifact pull <slug> DIR` for a linked directory, else `live copy: claude-artifact pull <slug> <another-dir>`. `--json` prints `{"slug", "url", "live", "base", "behind", "files": [{"state", "path"}]}`. `PATH` defaults to `.`; `--slug`/`--url` override `.artifact.json`.

On exit code `3` from `publish`:

- **Directory:** `claude-artifact pull <slug> DIR` merges the live version in, resolve any conflicts, then `claude-artifact publish DIR`.
- **Page:** compare with `status`, merge the live changes by hand, then publish again with `--base-version <live>`.

Either way, `--force` publishes anyway and discards the other version.

### list and read

```bash
claude-artifact list --scope shared
claude-artifact read <slug-or-url>          # title, version, role, file manifest
claude-artifact read <slug> -o meta.json   # also save the metadata JSON

claude-artifact read <slug> --path index.html > index.html            # one file's exact bytes
claude-artifact read <slug> --path index.html --path app.css --out-dir ./out   # saves, prints the paths
```

`read` returns metadata, paths, sizes, content types and sha256 hashes. Short-lived tokens in responses (`assetToken`, `subscriptionToken`, and `__frame_t=` on `list` thumbnail URLs) are replaced with `[redacted]` before they reach stdout or `-o`.

`read --path P` prints the published file's exact bytes to stdout instead, checked against the manifest's sha256. Repeat `--path` with `--out-dir DIR` to save several files under `DIR` at their published paths; it prints each saved path. `--path` doesn't combine with `--json` or `-o`, more than one `--path` needs `--out-dir`, and `--out-dir` needs `--path`.

Never WebFetch or curl a `claude.ai` artifact URL: it is a login-walled app shell and returns no page content.

## Updates

A uv tool or pipx install keeps itself current. At most once a day, a command checks PyPI in the background (2-second timeout, never in CI). When a newer release is out, it starts `uv tool upgrade claude-artifact-cli` (or `pipx upgrade claude-artifact-cli`) in a detached process after the command finishes and says so on stderr; `-q` keeps that quiet. The upgrade's output goes to `~/.config/claude-artifact-cli/update.log`. A plain pip install isn't changed behind your back: it prints a notice instead. A failed check or upgrade never changes the command's output or exit code.

```bash
claude-artifact update     # upgrade now, in the foreground
```

| Variable | Effect |
|----------|--------|
| `CLAUDE_ARTIFACT_NO_AUTO_UPDATE=1` | Check and print a notice, but never upgrade. |
| `CLAUDE_ARTIFACT_NO_UPDATE_CHECK=1` | No check at all. `CI` set does the same. |

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
| `HTTP 409: a newer version (live version …)`, exit code `3` | Someone published since your base version. For a directory, `claude-artifact pull <slug> DIR`, resolve conflicts, `publish DIR`. For a page, `status`, merge, republish with `--base-version <live>`. Or pass `--force`. |
| `not a valid artifact slug or URL` | Not one of the accepted forms (see [Commands](#commands)). |
| CSS, JS or images gone after an update | Published with `--mode replace` (or with 0.1.x, where replace was the default) without passing them. Republish them. |
| `no index.html in DIR` | A directory publish needs `index.html` at its top level. For an artifact made from a type, `pull` it into the directory first. |
| `… is a symlink; copy the file in instead` | Directory publishes and pulls refuse symlinks. |
| `changed both locally and in the live version:` | `pull` conflict; nothing was written. Resolve as under [pull](#pull). |
| `… is not empty and was not pulled from or published to this artifact` | `pull` into an unrelated directory. Pick another, or pass `--force`. |
| `… exists and is not a directory` | `pull` target is a file. |
| `… is a symlink; refusing to write through it` | A symlink on the path of a file being written. Remove it. |
| `… is not a clean relative path`, `refusing to write unsafe path` | A published path with `..`, an empty segment, a backslash, `%` or a control character. The CLI won't read or write it. |
| `version … was published while reading …` | A publish landed mid-read. Run the command again. |
| `… doesn't match its published sha256` | The bytes read don't match the manifest. Run the command again. |
| `no file '…' in version …` | Not a published path. `claude-artifact read <slug>` lists them. |
| `this login can't read the artifact's files (the server issued no asset token …)` | No read access to the bytes, e.g. a public artifact from another organization. |
| `--path writes file bytes; it doesn't combine with --json or -o` | Drop `--json`/`-o`; use `--out-dir` to save. |
| `more than one --path needs --out-dir`, `--out-dir goes with --path` | As stated. |
| `an artifact made from a type can only be patched` | `--mode replace` on a typed directory. Leave `--mode` off. |
| `…/index.html belongs to the artifact's type and can't be published; remove it` | A typed directory holds an `index.html`. Delete it. |
| `429 … daily publish cap` | Plan limit. Resets at UTC midnight. |

## The protocol

Reverse-engineered from the Claude Code CLI v2.1.273 binary and checked against v2.1.287, where artifacts are called *frames*. API calls go to `api.anthropic.com`, even though the artifact is served from `claude.ai`; file bytes come from a per-artifact content host.

```
POST /api/frame/deploy/direct     publish
POST /api/frame/deploy/prepare    content-hash preflight (bodies over ~15 MB)
POST /api/frame/upload            stage blobs the preflight asked for
GET  /api/frame/{slug}?via=model_read   boot: metadata, file manifest, assetToken
GET  /api/frame/read/{slug}       ownership / sharing status
GET  /api/frame/frames?limit=N    list

GET  https://{slug}.frame.claudeusercontent.com/_f/{ver}/_src/{path}?__frame_t=<assetToken>
                                  published source bytes (see Reading files)
GET  https://{slug}.frame.claudeusercontent.com/_f/{ver}/{path}?__frame_t=<assetToken>
                                  served copy
```

Headers on `api.anthropic.com` calls:

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

### Reading files

The boot, `GET /api/frame/{slug}?via=model_read`, returns `ver`, the `files` manifest (`path`, `size`, `contentType`, `sha256`) and a short-lived `assetToken`. Bytes come from the content host `https://{slug}.frame.claudeusercontent.com`:

- `GET /_f/{ver}/_src/{path}?__frame_t=<assetToken>` is the stored source, byte-identical to what was published. It works for an owner, writer, commenter or reader. Claude Code skips it only for an artifact shared in from outside the organization (boot `mode: "external"`) when `perm.role` isn't `writer`; the CLI does the same.
- `GET /_f/{ver}/{path}?__frame_t=<assetToken>` is the served copy, the fallback when `_src` doesn't answer 200. It is identical to the source for every file except the page, which is served at `/_f/{ver}/`. The served page has the frame runtime injected between `<!-- frame-runtime -->` and `<!-- /frame-runtime -->`, and may end with a `<script type="application/json" id="__frame_comments__">` trailer. Stripping both restores the source exactly.
- Send no `Authorization` header to the content host; the asset token in the query is the only credential. Send a non-default `User-Agent`: the CDN rejects Python-urllib's default with Cloudflare `error code: 1010` (HTTP 403).
- A missing file is `404`. A missing or expired token is `403`; the CLI boots again once for a fresh token, and fails if a new version went live in between. Redirects aren't followed.
- Path segments are percent-encoded. The CLI refuses paths with `..`, empty segments, backslashes, `%` or control characters.
- The CLI verifies every file against the manifest's `sha256`. Manifests of older artifacts lack `sha256`; `status` fetches those files to compare.
- Manifest entries with `src: {slug, ver}` belong to the Artifact type the artifact was made from, `index.html` included. They aren't the artifact's own files: `pull` and `status` skip them, and a typed publish patches around them.

The `assetToken` is redacted from every output and never written to disk.

### Artifact URLs

`https://claude.ai/code/artifact/<uuid>` carries the slug, a UUID. `https://claude.ai/artifact/<short id>` carries the same UUID as base58 (Bitcoin alphabet). The CLI decodes either, and accepts a bare UUID or short id.

### Not supported

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
    ├── cli.py               argparse entry point
    └── update.py            daily update check, background upgrade, `update`
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
| [The protocol](#the-protocol) | Endpoints, headers, deploy body and file reads |

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
