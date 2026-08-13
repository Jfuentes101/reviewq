# reviewq

A second opinion on a local branch, from an agent that has never seen your
conversation. Queued, N at a time, served over MCP so your Claude instances can
ask for one and come back later.

```
Claude instance ──MCP/http──▶ reviewq ──docker run --rm──▶ reviewer
   request_review              queue, SQLite                clone /src, claude -p
   review_results              N concurrent                 dies with the review
```

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

Environment, all optional. `REVIEWQ_IMAGE`, `REVIEWQ_MAX_CONCURRENT` (2),
`REVIEWQ_BASE_REF` (`origin/main`), `REVIEWQ_REVIEW_COMMAND`
(`.claude/commands/code-review.md`), `REVIEWQ_TIMEOUT_S` (1800), `REVIEWQ_EFFORT`,
`REVIEWQ_PORT` (4030), `REVIEWQ_STATE_DIR`.

`REVIEWQ_CONTAINER_HOME` (`/home/reviewer`) is where the credentials get mounted
inside the image. A variable and not a constant because it belongs to whoever built
the image — point a child image's `REVIEWQ_IMAGE` at a base that names its user
something else and this is what you change with it. A mismatch breaks the mount with
no error that says so.

`REVIEWQ_NO_NEW_PRIVILEGES` is **off** by default: on an AppArmor host the
docker-default profile reads the exec-time transition as gaining privileges and
every `execve` in the container returns EPERM. It still runs unprivileged with all
capabilities dropped.

## Known ceilings

- **Untracked files are invisible.** The snapshot is `git stash create`, which sees
  tracked files only. `git add` a new file and it shows up.
- **No spend gate.** `max_concurrent` is the only brake — nothing stops a busy
  afternoon from costing more than you meant. Reviews run around $0.30 on a small
  diff and $1.50 on a real one.
- **The image carries no project toolchain.** No bundler, no node_modules, no
  database. The prompt tells the model to skip what it cannot run, silently, so a
  review command that shells out to a linter degrades rather than fails — but it
  degrades quietly, which is worth knowing before you wonder why nothing was linted.
- **One machine, one queue.** The server is local and unauthenticated by design; it
  binds `127.0.0.1`. Do not put it on a shared interface.
