---
name: claude-artifact-cli
description: >-
  Publish, update, list and inspect Claude Artifacts from a shell with the
  `claude-artifact` CLI, using the Claude Code login already on the machine. Use
  when publishing an artifact from a script, a Makefile, a git hook, CI, a
  non-interactive session, or a subagent without the Artifact tool; when the user
  says "claude-artifact", "publish it from the terminal", or asks to script or
  automate artifact publishing; when updating an existing artifact by slug or
  URL; or when listing or inspecting artifacts from the command line. Prefer the
  built-in Artifact tool for ordinary interactive publishing.
metadata:
  source: https://github.com/nikships/claude-artifact-cli (PyPI claude-artifact-cli)
  protocol: Claude Code CLI v2.1.273 "frame" API, api.anthropic.com
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
```

Prints the artifact URL on stdout and progress on stderr, so `URL=$(claude-artifact
publish page.html -q)` captures just the URL.

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

Publishing **without** `--url`/`--slug` always creates a new artifact at a new
URL. When the user wants "the same page, updated", you must pass one of them —
recover the slug with `claude-artifact list` or by asking, rather than silently
creating a second artifact and announcing a new link.

### Multi-file pages

```bash
claude-artifact publish index.html \
  --root ./site \
  --file app.css --file app.js --file img/logo.png
```

`--file PUB[=SRC]` maps a published path to a source file; with no `=` they are
the same. `--root` is the base for source paths only — it never changes published
paths. Published paths are relative, with no leading slash, and are exactly what
the HTML references.

### Removing files, and the two modes

`replace` (the default) treats the manifest as the whole artifact, so **omitting
a file already removes it**. That is usually what you want.

`patch` overlays onto the current version and is the only mode that takes
explicit deletions:

```bash
claude-artifact publish index.html --slug <slug> --remove old.js
```

`--remove` implies `--mode patch` and requires `--slug`/`--url`. Patch needs a
`baseVersion`; the CLI looks the current one up for you, so do not pass
`--base-version` unless you are deliberately guarding against a concurrent
publish.

### Concurrent edits

A publish is refused if a newer version exists (someone else published, or a
viewer saved from inside the page). Re-read, merge, then publish again. Only pass
`--force` when the user has explicitly said to discard that specific version.

## List and inspect

```bash
claude-artifact list                      # slug, date, title
claude-artifact list --scope shared       # mine | shared | all
claude-artifact read <slug-or-url>        # title, version, role, file manifest
claude-artifact read <slug> --json
```

`read` returns metadata plus published paths, sizes, content types and sha256 —
**not the page bytes**, which are served from a separate sandboxed host. To
recover a page's content, use the local source file, or the built-in Artifact
tool's `read` action.

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

| Symptom | Cause |
|---|---|
| `401 unauthorized` | API key instead of OAuth, or expired login |
| `400 … null deletes a path in mode:"patch"` | `--remove` without patch mode |
| `400 mode "patch" requires baseVersion` | patching an artifact the lookup could not resolve |
| `404 … isn't yours to update` | wrong slug, deleted artifact, or another org |
| `409 … newer version exists` | concurrent publish — re-read, merge, retry |
| `429 … daily publish cap` | plan limit; resets at UTC midnight |

Full protocol notes: https://github.com/nikships/claude-artifact-cli#the-protocol
