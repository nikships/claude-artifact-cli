---
name: claude-artifact-cli
description: >-
  Publish, update, read, pull, list and inspect Claude Artifacts from a shell
  with the `claude-artifact` CLI, using the Claude Code login already on the
  machine. Use when publishing an artifact from a script, a Makefile, a git
  hook, CI, a non-interactive session, or a subagent without the Artifact tool;
  when the user says "claude-artifact", "publish it from the terminal", or asks
  to script or automate artifact publishing; when updating an existing artifact
  by slug or URL (claude.ai/code/artifact/… or claude.ai/artifact/…); when
  reading an artifact's files or pulling one into a directory to edit it; when
  publishing a directory or checking whether a local copy matches what is live
  (`claude-artifact status`); or when listing or inspecting artifacts from the
  command line. Never WebFetch or curl a claude.ai artifact URL to read it: it
  returns no page content; use `claude-artifact read SLUG --path index.html` or
  `claude-artifact pull`. Prefer the built-in Artifact tool for ordinary
  interactive publishing.
metadata:
  source: https://github.com/nikships/claude-artifact-cli (PyPI claude-artifact-cli)
  protocol: Claude Code CLI v2.1.273 / v2.1.287 "frame" API, api.anthropic.com
---

# claude-artifact CLI

A zero-dependency CLI that publishes and reads Claude Artifacts over the same API
the Claude Code Artifact tool uses (`/api/frame/*` on `api.anthropic.com`),
reading the machine's existing claude.ai OAuth login.

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

## Artifact URLs

Every command that takes a slug or URL (`SLUG`, `--slug`, `--url`) accepts:

- `https://claude.ai/code/artifact/<uuid>`
- `https://claude.ai/artifact/<short id>` — the form claude.ai and Claude
  Code's Artifact tool show
- a bare UUID or a bare short id

Pass whichever the user gave you; no conversion needed.

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
claude-artifact publish report.html --url https://claude.ai/artifact/<short id>
claude-artifact publish report.html --slug <slug>
```

An update defaults to **patch**: only the files you pass change, and every other
published file (CSS, JS, images) is kept. Publishing just the page is safe.

Publishing **without** `--url`/`--slug` creates a new artifact at a new URL
(except a directory with `.artifact.json`, which updates its own artifact). When
the user wants "the same page, updated", you must target it — recover the slug
with `claude-artifact list` or by asking, rather than silently creating a second
artifact and announcing a new link.

To change an artifact you don't have the source for, pull it first (see
"Round-trip a directory").

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
  kept. Default for updating a page (`--slug`/`--url`) and for a typed directory.
  The only mode that takes explicit deletions.
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

- a directory publish: the version saved in `.artifact.json` by the last pull or
  publish — real conflict detection;
- `--base-version <ver>`: the version you last read or published (from
  `--json`, `read`, or `status`);
- neither, in patch mode: the live version, and stderr says
  `no base version known - patching onto the live version`. That cannot detect a
  concurrent publish.

On exit 3:

- **A directory:** `claude-artifact pull <slug> ./site` merges the live version
  in, resolve any conflicts (below), then `claude-artifact publish ./site`.
- **A page:** `claude-artifact read <slug> --path index.html > live.html`,
  merge the live changes into your page, then publish again with
  `--base-version <live>`.

Only pass `--force` to `publish` when the user has explicitly said to discard
that specific version.

## Round-trip a directory

```bash
claude-artifact pull <slug-or-url> ./site     # existing artifact; or publish ./site --favicon 📊 for a new one
# ... edit files in ./site ...
claude-artifact status ./site                 # what differs from what's live
claude-artifact publish ./site                # same artifact, no --slug needed
```

- `publish DIR` publishes every non-dot file under the directory, recursively.
  `index.html` is required at the top (except for a typed artifact). Symlinks
  are refused; copy the file in.
- Replace mode by default (patch for a typed artifact): the directory **is** the
  artifact, so files deleted locally are deleted on publish.
- `pull` and each successful `publish` write `.artifact.json`: `slug`, `url`,
  `version`, `title`, `files` (published path → sha256), and `"typed": true` for
  a typed artifact. It is a dotfile, so it is never published. The next
  `publish DIR` updates that slug and sends that version as the base version, so
  a newer publish by someone else fails with exit 3 instead of being overwritten.
- `pull` prints the directory on stdout; stderr says the version, files written
  and removed, local edits kept, and type files left out. Default directory: one
  named after the slug.

### Pulling into a directory you already have

`pull` into a directory linked to the same artifact merges file by file against
the hashes in `.artifact.json`:

- changed only live → the live version is written;
- changed only locally → your edit is kept;
- deleted live, unchanged locally → removed (and any directory left empty);
- local files the artifact never had → kept;
- changed on both sides (an add or delete counts) → **conflict**: the error
  lists the files and nothing is written.

Resolve a conflict:

1. Copy your versions of the listed files outside the directory.
2. `claude-artifact pull <slug> ./site --force` — takes the live version of the
   conflicting files and applies every other live change, keeping your other
   edits.
3. Merge your changes back into those files.
4. `claude-artifact publish ./site`.

`pull` refuses a non-empty directory that isn't linked to this artifact. Pick
another directory; pass `--force` only when overwriting the artifact's paths
there is what the user wants (other files are left alone).

## Typed artifacts

An artifact made from an Artifact type (Slides, Design, …) holds files the type
supplies — `index.html`, `SKILL.md`, `artifact-type/…` — which are fixed.

- `pull` leaves them out and marks `.artifact.json` `"typed": true`; `status`
  ignores them.
- Edit only the artifact's own files, the ones `pull` wrote. Never add
  `index.html`, `SKILL.md` or `artifact-type/` to the directory.
- `publish DIR` patches only those files. A local `index.html` is refused, as is
  `--mode replace`. A file deleted locally since the last pull or publish is
  removed from the artifact.
- Always start from `pull`: without its `.artifact.json`, `publish DIR` treats
  the directory as a plain artifact and asks for `index.html`.

## Read and inspect

**Never WebFetch or curl a claude.ai artifact URL.** `claude.ai/code/artifact/…`
and `claude.ai/artifact/…` are a login-walled app shell; fetching one returns no
page content. Get content with the CLI:

```bash
claude-artifact read <slug-or-url> --path index.html          # exact bytes to stdout
claude-artifact read <slug> --path index.html --path app.css --out-dir ./live   # saves; prints paths
claude-artifact pull <slug-or-url> ./site                     # every file, ready to edit
```

`read --path` prints the published file's exact bytes, checked against the
manifest's sha256. More than one `--path` needs `--out-dir`, which saves each file
under the directory at its published path. `--path` doesn't combine with `--json`
or `-o`.

To change an artifact: pull it, work in the pulled directory, check with
`status`, update with `publish DIR`.

```bash
claude-artifact status                    # current dir; slug from .artifact.json
claude-artifact status ./site
claude-artifact status page.html --slug <slug>   # page vs published index.html only
claude-artifact status ./site --json      # {slug,url,live,base,behind,files:[{state,path}]}
```

`status` compares local sha256 against the published manifest (fetching a file
when an older manifest has no sha256) and prints the live version, the base
version (directories with `.artifact.json`) with `(up to date)` or
`(someone published since - merge first)`, and one row per path: `same`,
`changed`, `local-only`, `remote-only`. Default `PATH` is `.`. When anything
differs, stderr names the next step: `merge the live version in: claude-artifact
pull <slug> DIR` for a linked directory, else `live copy: claude-artifact pull
<slug> <another-dir>`.

```bash
claude-artifact list                      # slug, date, title
claude-artifact list --scope shared       # mine | shared | all
claude-artifact read <slug-or-url>        # title, version, role, file manifest
claude-artifact read <slug> --json
```

`read` without `--path` returns metadata plus published paths, sizes, content
types and sha256. Short-lived tokens in responses (`assetToken`,
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
| `not a valid artifact slug or URL` | not one of the forms under "Artifact URLs" |
| `removing a file needs --mode patch` | `--remove` with `--mode replace`; just leave the file out instead |
| `removing a file needs --slug/--url` | `--remove` on a new artifact |
| `couldn't determine the current version to patch onto` | the live-version lookup returned no version; pass `--base-version` |
| `404 … isn't yours to update` | wrong slug, deleted artifact, or another org |
| `HTTP 409: a newer version (live version …)`, exit 3 | someone published since your base version — directory: `pull <slug> DIR`, resolve, `publish DIR`; page: merge, retry with `--base-version <live>` |
| CSS/JS/images gone after an update | published with `--mode replace` (or a 0.1.x CLI, where replace was the default) without passing them; republish them |
| `no index.html in DIR` | directory publish needs `index.html` at its top level; for a typed artifact, `pull` into the directory first |
| `… is a symlink; copy the file in instead` | directory publishes and pulls refuse symlinks; copy the file in |
| `changed both locally and in the live version:` | `pull` conflict, nothing written; resolve as under "Pulling into a directory you already have" |
| `… is not empty and was not pulled from or published to this artifact` | `pull` into an unrelated directory; pick another |
| `… exists and is not a directory` | `pull` target is a file |
| `… is a symlink; refusing to write through it` | a symlink on the path of a file being written; remove it |
| `… is not a clean relative path` / `refusing to write unsafe path` | published path with `..`, an empty segment, `\`, `%` or a control character; the CLI won't read or write it |
| `version … was published while reading …` | a publish landed mid-read; run the command again |
| `… doesn't match its published sha256` | bytes read don't match the manifest; run the command again |
| `no file '…' in version …` | not a published path; `claude-artifact read <slug>` lists them |
| `this login can't read the artifact's files (the server issued no asset token …)` | no access to the bytes (e.g. a public artifact from another org); ask the user for the content |
| `--path writes file bytes; it doesn't combine with --json or -o` | drop `--json`/`-o`; save with `--out-dir` |
| `more than one --path needs --out-dir` / `--out-dir goes with --path` | as stated |
| `an artifact made from a type can only be patched` | `--mode replace` on a typed directory; leave `--mode` off |
| `…/index.html belongs to the artifact's type and can't be published; remove it` | typed directory holds an `index.html`; delete it |
| `429 … daily publish cap` | plan limit; resets at UTC midnight |

Full protocol notes: https://github.com/nikships/claude-artifact-cli#the-protocol
