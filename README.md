# reviewq

A second opinion on a local branch, from an agent that has never seen your
conversation. Queued, N at a time, served over MCP so your Claude instances can
ask for one and come back later.

```
Claude instance ──MCP/http──▶ reviewq ──docker run --rm──▶ reviewer
   request_review              queue, SQLite                clone /src, claude -p
   review_results              N concurrent                 dies with the review
```

## One branch, not a team

robbie is server-side infrastructure. It stands over a whole repository the way an
admin does: polls for every PR that asks for it, reviews many at once across many
authors, pins the policy it judges by, tracks what it spends, and reports to a
dashboard and a Slack channel. It answers to the team, which is why it has gates and
budgets and a thread you can argue with it in.

This administers nothing. It watches no repository, gates no merge, tracks no author,
enforces no policy. It does one thing: it runs a review with none of the main
session's bias. That is the whole product.

The bias is what you are buying. When the agent that wrote the code reviews the code,
it is holding the plan, the ticket, and the turns where it talked itself into the
approach — and it will not call that approach wrong, because it is the one that chose
it. So the reviewer here gets a separate process, a fresh context, the diff, and the
`context` paragraph you wrote deliberately. Nothing else reaches it. What comes back
is a judgement from someone with no stake in your reasoning.

|  | robbie | reviewq |
|---|---|---|
| scope | a repository, continuously | one branch, when asked |
| who it serves | the team, and whoever admins it | the single session that asked |
| how many at once | many PRs, many authors, budgeted | one queue, one machine |
| what it decides | gates, labels, what reaches a human | nothing — it reports and stops |
| its rules | pinned centrally, because a PR must not rewrite its own judge | read from the checkout |
| what it is | orchestrator, sidecars, dashboard, compose | one process, one SQLite file, `127.0.0.1` |

Same output contract as robbie, minus everything GitHub — no PR, no thread, no CI, no
findings anchored to diff lines, and `context` in their place. Which puts this inside
the loop and robbie at the end of it: an agent queues a review, keeps working, reads
the verdict, fixes what holds and goes again, with nobody waiting on it. `/refine` is
that loop. By the time robbie sees the branch, the cheap findings are gone.

## Why a container

A review needs a general-purpose shell — sed, loops, pipes into scripts — so there
is no allowlist that separates useful from destructive, and the agent runs with
`bypassPermissions`. What keeps it from wrecking anything is that your workspace is
mounted **read-only** and the agent works on a clone inside a `--rm` container. It
can `rm -rf` all it likes; the blast radius dies with the process.

The clone is not overhead. It is what freezes the snapshot, taken the moment you
ask, so you can keep typing while the review runs.

Nothing is pushed and nothing is fetched: the clone is from your disk, so unpushed
commits and uncommitted work both come along. Both ends of the diff are pinned to
**shas on the host** before the container starts, because `git clone /src` turns
this repo's local branches into the clone's remote-tracking refs — `origin/main`
inside means "the main branch on your machine", which is a different commit the
moment either has moved, and no ref at all when the base was never checked out
locally. A sha means one thing in both places.

## Setup

You need Python 3.11+, Docker, and either an `ANTHROPIC_API_KEY` or a machine where
you have run `claude` at least once.

```bash
# 1. the reviewer image. The UID args matter: the credentials file is mounted mode
#    600, so the container's user has to be you.
docker build -t reviewq-reviewer:latest \
  --build-arg UID=$(id -u) --build-arg GID=$(id -g) .

# 2. the server
python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/reviewq

# 3. point Claude Code at it, from anywhere
claude mcp add --transport http reviewq http://127.0.0.1:4030/mcp
```

`curl -s localhost:4030/healthz` to check it is alive. It refuses to start if it has
no way to pay for a review, and says which of the two to set.

**How it authenticates.** `ANTHROPIC_API_KEY` if set — passed to the container by
name, never on the command line, since an argv is world-readable through `/proc`.
Otherwise `~/.claude/.credentials.json`, mounted read-only. Only that file, never
`~/.claude`: that directory holds memories and project state, and the thing reading
it has every permission granted. The session is not refreshed from inside the
container — your own CLI keeps it current, and a review lasts minutes.

If a project's review command runs its own toolchain, build a child image `FROM
reviewq-reviewer:latest` and point `REVIEWQ_IMAGE` at it.

## The tools

| tool | for |
|---|---|
| `request_review(workspace, context, base_ref)` | queue one; returns an id straight away |
| `queue_status()` | everything, newest first, with state and verdict |
| `review_status(id)` | queued / running / done / failed |
| `review_results(id)` | verdict, summary, findings as objects |

`workspace` is an absolute path to a git repo, and the only argument with no
default. What gets reviewed is `base_ref` against your **working tree** — staged and
unstaged changes to tracked files included — so there is nothing to commit first, and
nothing to squash between rounds of a loop. Then wait: a review takes five to ten minutes, so poll `review_status` on
that scale — it is the cheap one, and `review_results` before the end tells you the
same thing at more length. A `failed` review carries its reason there instead, and
`raw` holds the model's whole response, which is where a parse failure is diagnosed.

## `context` is the point

The reviewer is fresh, which is what you are buying and also what makes it report
deliberate behaviour as a bug. `context` is where you put **what the diff cannot
tell it**: how this behaves in production today, the failure being fixed, who
resolves what, which slice of a larger feature this is.

> Today a failed payment returns an error and the guest retries on another listing,
> creating duplicate reservations. This creates the reservation anyway and records
> the failure on it for the owner to resolve.

Write it about the **system**, never about the reviewer. That is what keeps the
parameter from becoming a way to switch off the half of the review you expected to
lose: a statement of intended behaviour narrows what counts as a defect, while
"ignore the notification changes" narrows what gets *read* and buries every real bug
in the same code.

> ✅ *A group booking sends a notification for every reservation in it, not only the
> parent.* — the fan-out stops being a finding; a query-per-reservation inside it
> still is.
>
> ❌ *Ignore the notification changes.* — buries both.

The test: could this sentence have been written before the code existed, in a ticket
or a spec? Then it is context. The reviewer is told the same rule and reads past an
instruction that reaches it anyway, so a bypass costs a round and gets you nothing.

What also does not go in there is the justification for the approach — that is the
judgement you are paying for. Facts about the world outside the repo, not arguments.

## Configuration

Everything is an environment variable and everything has a default that works.

| variable | default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | how it pays, if set. Wins over the session file |
| `REVIEWQ_CREDENTIALS` | `~/.claude/.credentials.json` | otherwise, the session it mounts |
| `REVIEWQ_REVIEW_COMMAND` | `.claude/commands/code-review.md` | read from the repo under review |
| `REVIEWQ_BASE_REF` | `origin/main` | what a review diffs against, per request overridable |
| `REVIEWQ_MAX_CONCURRENT` | `2` | containers at once; the only brake on spend |
| `REVIEWQ_IMAGE` | `reviewq-reviewer:latest` | |
| `REVIEWQ_CONTAINER_HOME` | `/home/reviewer` | where credentials mount inside it |
| `REVIEWQ_TIMEOUT_S` | `1800` | |
| `REVIEWQ_EFFORT` | `high` | |
| `REVIEWQ_CPUS` / `REVIEWQ_MEMORY` / `REVIEWQ_PIDS_LIMIT` | `2` / `4g` / `512` | |
| `REVIEWQ_HOST` / `REVIEWQ_PORT` | `127.0.0.1` / `4030` | |
| `REVIEWQ_STATE_DIR` | `~/.local/share/reviewq` | the queue's SQLite file |
| `REVIEWQ_LOG_LEVEL` | `INFO` | |
| `REVIEWQ_NO_NEW_PRIVILEGES` | `false` | see below |

**`REVIEWQ_REVIEW_COMMAND`** is a path *inside the repo being reviewed*, so one
server serving several projects wants the path they share. A repo that does not have
that file falls back to reviewq's own prompt — silently, and on purpose: a generic
review is a better outcome than a failed one.

**`REVIEWQ_CONTAINER_HOME`** belongs to whoever built the image. Point
`REVIEWQ_IMAGE` at a base that names its user something else and this is what you
change with it; a mismatch breaks the credentials mount with no error that says so.

**`REVIEWQ_NO_NEW_PRIVILEGES`** is off by default because on an AppArmor host
(Ubuntu, Pop!_OS) the docker-default profile reads the exec-time transition as
gaining privileges and every `execve` in the container returns EPERM. The container
still runs unprivileged with all capabilities dropped.

## Running it as a service

A systemd **user** unit, so it comes up when you log in and dies with your session.
`~/.config/systemd/user/reviewq.service`:

```ini
[Unit]
Description=reviewq — queued local second-opinion code review over MCP
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=exec
ExecStart=%h/work/reviewq/.venv/bin/reviewq
WorkingDirectory=%h/work/reviewq
Environment=PYTHONUNBUFFERED=1
Environment=REVIEWQ_REVIEW_COMMAND=.claude/commands/rp-cr.md
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now reviewq.service
systemctl --user status reviewq        # the boot line says which image and which auth
journalctl --user -u reviewq -f        # what it is doing
systemctl --user restart reviewq       # after any change to the code or the prompt
```

Do **not** add `PrivateTmp=true`. This bind-mounts host paths into containers, and a
private `/tmp` would silently mount an empty directory for any workspace under it.

Your user's docker group membership carries into the unit, so nothing else is needed
to reach the daemon. `loginctl enable-linger $USER` only if you want it up without
being logged in, which for a local dev tool you probably do not.

**The server holds the prompt in memory.** Editing `contract.py` changes nothing
until you restart the unit — the one way to be quietly reviewing with last week's
rules.

## Known ceilings

- **Untracked files are invisible.** The snapshot is `git stash create`, which sees
  tracked files only. `git add` a new file and it shows up — properly, not `git add
  -N`: an intent-to-add entry makes `stash create` refuse the whole tree, and the
  request fails saying so.
- **A shallow workspace starts slowly.** `git clone --shared` quietly stops sharing
  when the source has a `.git/shallow`, and copies the object store instead. The
  clone borrows the objects anyway — the script writes the alternate itself, which
  is what keeps the unreferenced snapshot commit reachable — but a 500MB repo pays
  half a minute per review for the copy nobody asked for.
- **No spend gate.** `max_concurrent` is the only brake — nothing stops a busy
  afternoon from costing more than you meant. Reviews run around $0.30 on a small
  diff and $1.50 on a real one.
- **The image carries no project toolchain.** No bundler, no node_modules, no
  database. The prompt tells the model to skip what it cannot run, silently, so a
  review command that shells out to a linter degrades rather than fails — but it
  degrades quietly, which is worth knowing before you wonder why nothing was linted.
- **Review workspaces you trust.** The read-only mount and the `--rm` bound the
  damage to *your files*, and that is all they bound. The repo's own
  `REVIEWQ_REVIEW_COMMAND` is injected as instructions into an agent running
  `bypassPermissions`, with your credentials mounted and the network open, so on a
  repo you did not write that file is attacker-controlled input next to your session
  token. Your own branches are the case this is built for; a stranger's is not.
- **One machine, one queue.** The server is local and unauthenticated by design; it
  binds `127.0.0.1`. Do not put it on a shared interface.
