---
name: claude-artifact-cli
description: >-
  Publish, update, list and inspect Claude Artifacts from a shell with the
  `claude-artifact` CLI, using the Claude Code login already on the machine. Use
  when publishing an artifact from a script, a Makefile, a git hook, CI, a
  non-interactive session, or a subagent without the Artifact tool; when the user
  says "claude-artifact", "publish it from the terminal", or asks to script or
  automate artifact publishing; when updating an existing artifact by slug or
  URL; when publishing a directory or checking whether a local copy matches what
  is live (`claude-artifact status`); or when listing or inspecting artifacts
  from the command line. Never WebFetch or curl a claude.ai artifact URL to read
  it: it returns no page content. Prefer the built-in Artifact tool for ordinary
  interactive publishing.
metadata:
  source: https://github.com/nikships/claude-artifact-cli (PyPI claude-artifact-cli)
  protocol: Claude Code CLI v2.1.273 / v2.1.287 "frame" API, api.anthropic.com
---

# claude-artifact CLI

A zero-dependency CLI that publishes Claude Artifacts over the same API the
Claude Code Artifact tool uses (`/api/frame/*` on `api.anthropic.com`), reading
the machine's existing claude.ai OAuth login.

## Prefer the built-in Artifact tool when it is available

In an interactive Claude Code session with the `Artifact` tool, use that tool.
It handles the design pass, runtime capabilities, thumbnails, comments and
watching; this CLI handles none of that. Reach for the CLI when:

- the publish has to happen from a shell — a script, Makefile, git hook, or CI,
- the session has no `Artifact` tool (a subagent, a plain shell, another agent),
- the user explicitly asks for the CLI or for a scriptable/automatable publish.

## Check it is there

```bash
claude-artifact whoami
```

Prints the token length and last few characters, then makes a real authenticated
call. Success means both the login and the network path are good. If the command
is missing:

```bash
uv tool install claude-artifact-cli
# or, without installing:
uvx --from claude-artifact-cli claude-artifact whoami
# upgrade an existing install:
uv tool upgrade claude-artifact-cli
```

Never echo the token itself, and never pass it on a command line that gets logged.

## Publish

```bash
# new artifact — title comes from the page's <title>
claude-artifact publish report.html --favicon 📊 --description "Q3 numbers"

# a whole directory (index.html required) — see "Round-trip a directory"
claude-artifact publish ./site --favicon 📊
```

Prints the artifact URL on stdout and progress on stderr, so `URL=$(claude-artifact
publish page.html -q)` captures just the URL. `--json` prints the deploy response
with `url`, `slug` and `version`.

Options: `--title`, `--favicon` (emoji), `--description` (one sentence),
`--icon` (one generic word), `--label` (short name for this publish),
`-q/--quiet`, `--json`.

Give `--favicon` on a first publish; the tool falls back to a generic emoji
rather than failing, which is rarely what the user wants.

### Update an existing artifact

Target it by URL or slug — the URL keeps working and viewers see the new version:

```bash
claude-artifact publish report.html --url https://claude.ai/code/artifact/<slug>
claude-artifact publish report.html --slug <slug>
```

An update defaults to **patch**: only the files you pass change, and every other
published file (CSS, JS, images) is kept. Publishing just the page is safe.

Publishing **without** `--url`/`--slug` creates a new artifact at a new URL
(except a directory with `.artifact.json`, which updates its own artifact). When
the user wants "the same page, updated", you must target it — recover the slug
with `claude-artifact list` or by asking, rather than silently creating a second
artifact and announcing a new link.

### Multi-file pages

```bash
claude-artifact publish index.html \
  --root ./site \
  --file app.css --file app.js --file img/logo.png
```

`--file PUB[=SRC]` maps a published path to a source file; with no `=` they are
the same. `--root` is the base for source paths only — it never changes published
paths. Published paths are relative, with no leading slash, and are exactly what
the HTML references. For more than a few files, publish the directory instead;
`--file`/`--root` are refused with a directory.

### Removing files, and the two modes

- `patch` overlays the manifest onto a base version: files you leave out are
  kept. Default for updating a page (`--slug`/`--url`). The only mode that
  takes explicit deletions.
- `replace` makes the manifest the whole artifact: **every published file you
  leave out is removed**. Default for a new artifact and for a directory publish.
  Pass `--mode replace` on a page update only when the files you pass really are
  the whole artifact.

```bash
claude-artifact publish index.html --slug <slug> --remove old.js
```

`--remove` needs an existing artifact (`--slug`/`--url`, or a directory's
`.artifact.json`) and patch mode; with `--remove` and no `--mode`, patch is used,
directories included.

### Concurrent edits

Both modes honor a base version: if a newer version is live (someone else
published, or a viewer saved from inside the page), the publish is refused with
HTTP 409 and **exit code 3**. The error names the live version.

Where the base version comes from:

- a directory publish: the version saved in `.artifact.json` — real conflict
  detection;
- `--base-version <ver>`: the version you last read or published (from
  `--json`, `read`, or `status`);
- neither, in patch mode: the live version, and stderr says
  `no base version known - patching onto the live version`. That cannot detect a
  concurrent publish.

On exit 3: run `claude-artifact status`, merge their changes into yours, then
publish again with `--base-version <live>`. Only pass `--force` when the user
has explicitly said to discard that specific version.

## Round-trip a directory

```bash
claude-artifact publish ./site --favicon 📊   # writes ./site/.artifact.json
# ... edit files in ./site ...
claude-artifact status ./site                 # what differs from what's live
claude-artifact publish ./site                # same artifact, no --slug needed
```

- Publishes every non-dot file under the directory, recursively. `index.html` is
  required at the top. Symlinks are refused; copy the file in.
- Replace mode by default: the directory **is** the artifact, so files deleted
  locally are deleted on publish.
- After each successful publish it writes `.artifact.json` (`slug`, `url`, `version`,
  `title`). It is a dotfile, so it is never published. The next `publish DIR`
  updates that slug and sends that version as the base version, so a newer
  publish by someone else fails with exit 3 instead of being overwritten.
- On exit 3: `status` shows which files differ. You do not have the live bytes,
  so get them first (step 3 under "Read and inspect"), merge, then
  `claude-artifact publish ./site --base-version <live>`.

## Read and inspect

**Never WebFetch or curl a claude.ai artifact URL.** `claude.ai/code/artifact/…`
is a login-walled app shell; the page bytes are served from a sandboxed host
behind a short-lived token. Fetching the URL returns no page content. The CLI
cannot download page bytes either.

To work on an artifact's content, in order:

1. **Use the local source** — the published directory (with `.artifact.json`),
   or the file you published.
2. **Check it matches what's live** with `claude-artifact status`. A
   `remote-only` row, a `changed` row for a file you did not edit, or a base
   version marked `(someone published since - merge first)` means someone else
   changed the live copy and you do not have those bytes.
3. **If you need live bytes you don't have**, ask the user for them, or, if this
   session has Claude Code's built-in `Artifact` tool, use its `read` action.

```bash
claude-artifact status                    # current dir; slug from .artifact.json
claude-artifact status ./site
claude-artifact status page.html --slug <slug>   # page vs published index.html only
claude-artifact status ./site --json      # {slug,url,live,base,behind,files:[{state,path}]}
```

`status` compares local sha256 against the published manifest and prints the
live version, the base version (directories with `.artifact.json`) with
`(up to date)` or `(someone published since - merge first)`, and one row per
path: `same`, `changed`, `local-only`, `remote-only`. Default `PATH` is `.`.

```bash
claude-artifact list                      # slug, date, title
claude-artifact list --scope shared       # mine | shared | all
claude-artifact read <slug-or-url>        # title, version, role, file manifest
claude-artifact read <slug> --json
```

`read` returns metadata plus published paths, sizes, content types and sha256 —
not the page bytes. Short-lived tokens in responses (`assetToken`,
`subscriptionToken`, `__frame_t=` on thumbnail URLs) come back as `[redacted]`.

## Auth

Resolution order: `--token` → `$CLAUDE_ARTIFACT_TOKEN` →
`$CLAUDE_CODE_ARTIFACTS_API_TOKEN` → macOS Keychain (`Claude Code-credentials`)
→ `~/.claude/.credentials.json`.

This needs the **claude.ai OAuth token** from `claude /login`. An
`ANTHROPIC_API_KEY` is rejected — a 401 almost always means an API key was used,
or the login expired. Fix: start Claude Code once (it refreshes on launch), or
re-run `claude /login`. The token is short-lived and re-read on every invocation,
so there is nothing to rotate or bake in.

## Not supported

- **Downloading page bytes.** See "Read and inspect".
- **Delete and unpin/pin.** These go through a relay-only route the CLI cannot
  reach. Do not guess at endpoints. Direct the user to `/artifacts` in Claude
  Code, or to claude.ai.
- **Runtime capabilities.** No flags for `capabilities`/`contract`. A page needing
  live data, persistence, shared state or file storage must go through the
  built-in Artifact tool.
- **Comments, watching, thumbnails.**

## Publishing rules still apply

The CLI is a transport, not an exemption. Read any file before publishing it —
publishing distributes it. Do not publish pages that impersonate a real person or
organization, fabricated records presented as genuine, or flows that collect
credentials under false pretenses. Confirm before overwriting an artifact you did
not just publish, and never delete or `--force` over someone's work unasked.

## Troubleshooting

Exit codes: `0` ok, `1` API error, `2` auth error, `3` version conflict, `130`
interrupted.

| Symptom | Cause |
|---|---|
| `401 unauthorized` | API key instead of OAuth, or expired login |
| `removing a file needs --mode patch` | `--remove` with `--mode replace`; just leave the file out instead |
| `removing a file needs --slug/--url` | `--remove` on a new artifact |
| `couldn't determine the current version to patch onto` | the live-version lookup returned no version; pass `--base-version` |
| `404 … isn't yours to update` | wrong slug, deleted artifact, or another org |
| `HTTP 409: a newer version (live version …)`, exit 3 | someone published since your base version — `status`, merge, retry with `--base-version <live>` |
| CSS/JS/images gone after an update | published with `--mode replace` (or a 0.1.x CLI, where replace was the default) without passing them; republish them |
| `no index.html in DIR` | directory publish needs `index.html` at its top level |
| `… is a symlink` | directory publishes refuse symlinks; copy the file in |
| `429 … daily publish cap` | plan limit; resets at UTC midnight |

Full protocol notes: https://github.com/nikships/claude-artifact-cli#the-protocol
