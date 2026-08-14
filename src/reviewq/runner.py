"""One container per review, then it dies.

The container is the whole safety story. A review needs a general-purpose shell —
sed, loops, pipes into scripts — so there is no allowlist of tools that separates
useful from destructive, and the agent runs with bypassPermissions. What keeps it
from wrecking anything is that your workspace is mounted read-only and the agent
works on a clone inside a `--rm` container: it can `rm -rf` all it likes, and the
blast radius dies with the process.

The clone is not overhead. It is what freezes the snapshot, so you can keep typing
while a review runs and its file:line still mean what they meant at request time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from reviewq.config import Config
from reviewq.contract import Blocks, parse_blocks

logger = logging.getLogger("reviewq.runner")

# Runs inside the container, with the prompt on stdin. Kept here rather than baked
# into an image so there is no second thing to rebuild when it changes.
#
# --shared makes the clone borrow objects from the mount instead of copying them:
# on a large repo that is a 2s start instead of 40s. Borrowing is also what lets us
# check out a `git stash create` commit that no ref points at — which is why the
# script writes the alternate itself rather than trusting the flag.
SCRIPT = r"""
set -euo pipefail
prompt="$(cat)"

git clone --quiet --shared /src /work/repo
cd /work/repo
# A SHALLOW source silently turns --shared off: git disables the local clone and
# fetches instead, and a fetch only carries reachable objects — so the unreferenced
# `git stash create` commit never arrives. Borrow the objects explicitly.
alt=.git/objects/info/alternates
grep -qxF /src/.git/objects "$alt" 2>/dev/null || echo /src/.git/objects >> "$alt"
git checkout --quiet --detach "$REVIEW_SHA"

# The repo's own review command, injected as text rather than loaded by the CLI:
# --setting-sources user means the CLI never reads the checkout's .claude/, and
# that is deliberate — one thing decides what runs, and it is this script.
cmd=""
if [ -f "$REVIEW_COMMAND" ]; then
  cmd="$(awk 'NR==1&&/^---/{f=1;next} f&&/^---/{f=0;next} !f' "$REVIEW_COMMAND")"
  # variable expansion, never eval: backticks in the command file stay literal data
  cmd="${cmd//\$ARGUMENTS/$REVIEW_TARGET}"
  cmd="

--- review command follows (\$ARGUMENTS = $REVIEW_TARGET) ---
$cmd"
fi

exec claude -p \
  --output-format json \
  --permission-mode bypassPermissions \
  --setting-sources user \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --effort "$REVIEW_EFFORT" \
  --no-session-persistence \
  <<<"$prompt$cmd"
"""


@dataclass(frozen=True)
class Run:
    ok: bool
    blocks: Blocks | None = None
    raw: str = ""
    cost_usd: float | None = None
    duration_s: float = 0.0
    error: str | None = None


@dataclass(frozen=True)
class Target:
    sha: str  # what to review, uncommitted work included
    branch: str
    base_sha: str  # what to review it against
    base_ref: str  # the name that resolved to it, for the prompt to say out loud


async def snapshot(workspace: Path, base_ref: str) -> Target:
    """Everything a review needs, pinned to shas on this side of the mount.

    `git stash create` builds a commit out of the working tree without touching it
    or the stash list. It is unreferenced, which is fine: the clone borrows objects
    from this repo, so the container can check out something no ref names.

    The base is resolved HERE for a sharper reason. `git clone /src` turns this
    repo's local BRANCHES into the clone's remote-tracking refs, so `origin/main`
    inside the container silently means "the main branch on this machine" — which
    is a different commit from the real `origin/main` the moment either has moved,
    and no ref at all when the base is a branch never checked out locally. A sha
    means one thing in both places, and the alternates make the object reachable.

    ponytail: tracked files only, which is what `stash create` sees. A brand-new
    file nobody has `git add`ed is invisible to the review; add it and it shows up.
    """
    try:
        stashed = await _git(workspace, "stash", "create")
    except RuntimeError as ex:
        raise RuntimeError(
            f"cannot snapshot the working tree: {ex}. An intent-to-add entry "
            "(`git add -N`, or `git add -p` on a new file) breaks `git stash create`; "
            "`git add` the file properly, or commit it."
        ) from ex
    return Target(
        sha=stashed or await _git(workspace, "rev-parse", "HEAD"),
        branch=await _git(workspace, "rev-parse", "--abbrev-ref", "HEAD"),
        base_sha=await _base(workspace, base_ref),
        base_ref=base_ref,
    )


async def _base(workspace: Path, base_ref: str) -> str:
    """`base_ref` as a sha, falling back through the ways a base gets named.

    A configured `origin/main` is wrong on a repo that has never fetched and right
    everywhere else, so trying the bare name second costs nothing and covers it.
    """
    tried = [base_ref, base_ref.split("/", 1)[-1] if "/" in base_ref else f"origin/{base_ref}"]
    for name in tried:
        try:
            return await _git(workspace, "rev-parse", "--verify", f"{name}^{{commit}}")
        except RuntimeError:
            continue
    raise RuntimeError(
        f"cannot resolve a base to diff against: tried {' and '.join(tried)}. "
        "Pass base_ref with something this repo actually has."
    )


async def run_review(cfg: Config, review_id: int, workspace: Path, *,
                     target: Target, prompt: str) -> Run:
    name = f"reviewq-{review_id}"
    argv = _argv(cfg, name, workspace, target=target)
    logger.info("spawning %s for %s", name, workspace)

    loop = asyncio.get_running_loop()
    started = loop.time()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **_env(cfg)},
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(prompt.encode()), timeout=cfg.timeout_s
        )
    except TimeoutError:
        await _kill(name)
        await proc.wait()  # the client exits once the container is gone
        return Run(ok=False, duration_s=loop.time() - started,
                   error=f"timed out after {cfg.timeout_s}s")
    elapsed = loop.time() - started

    if proc.returncode != 0:
        return Run(ok=False, duration_s=elapsed,
                   error=_why(proc.returncode, out, err, cfg.image))
    try:
        envelope = json.loads(out.decode(errors="replace"))
    except json.JSONDecodeError as ex:
        return Run(ok=False, duration_s=elapsed, error=f"could not parse the envelope: {ex}")

    text = envelope.get("result") or ""
    return Run(
        ok=True,
        blocks=parse_blocks(text),
        raw=text,
        cost_usd=_as_float(envelope.get("total_cost_usd")),
        duration_s=elapsed,
    )


def _argv(cfg: Config, name: str, workspace: Path, *, target: Target) -> list[str]:
    argv = [
        "docker", "run", "--rm", "-i",
        "--name", name,
        "--cpus", cfg.cpus,
        "--memory", cfg.memory,
        "--pids-limit", str(cfg.pids_limit),
        "--cap-drop", "ALL",
        # the one mount that matters: your work, unwritable
        "-v", f"{workspace}:/src:ro",
        "-e", f"REVIEW_SHA={target.sha}",
        "-e", f"REVIEW_COMMAND={cfg.review_command}",
        "-e", f"REVIEW_EFFORT={cfg.effort}",
        # a review command written for pull requests takes a branch name here, and
        # that is the input shape it already has a local path for
        "-e", f"REVIEW_TARGET={target.branch}",
    ]
    if cfg.no_new_privileges:
        argv += ["--security-opt", "no-new-privileges"]
    if cfg.api_key:
        # by name, not by value: an argv is world-readable through /proc, so the
        # docker client reads this out of its own environment instead (see _env)
        argv += ["-e", "ANTHROPIC_API_KEY"]
    elif cfg.credentials.is_file():
        # the file, not ~/.claude: that directory holds memories and project state,
        # and this container runs an agent with every permission granted
        argv += [
            "-v",
            f"{cfg.credentials}:{cfg.container_home}/.claude/.credentials.json:ro",
        ]
    argv += ["--entrypoint", "bash", cfg.image, "-c", SCRIPT]
    return argv


def _env(cfg: Config) -> dict[str, str]:
    """What `docker run -e NAME` picks up, kept off the command line."""
    return {"ANTHROPIC_API_KEY": cfg.api_key} if cfg.api_key else {}


def _why(code: int | None, out: bytes, err: bytes, image: str) -> str:
    """Whatever the run itself said, envelope first: stderr opens with enough git
    chatter to fill an excerpt on its own."""
    try:
        envelope = json.loads(out.decode(errors="replace"))
        said = str(envelope.get("result") or envelope.get("error") or "").strip()
        if said:
            return f"container exited {code}: {said[:300]}"
    except Exception:  # noqa: BLE001 — not JSON, so fall through to stderr
        pass
    stderr = err.decode(errors="replace").strip()
    if "Unable to find image" in stderr or "pull access denied" in stderr:
        # docker's own words are about a registry it was never going to find this
        # in, which reads as a network problem on a first run rather than a
        # missing build step
        return (
            f"the reviewer image {image} does not exist on this machine. Build it: "
            f"docker build -t {image} --build-arg UID=$(id -u) --build-arg "
            "GID=$(id -g) ."
        )
    return f"container exited {code}: {stderr[-400:]}"


async def _git(workspace: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(workspace), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {err.decode(errors='replace').strip()}")
    return out.decode().strip()


async def _kill(name: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "docker", "kill", name,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
