"""Everything tunable, from the environment.

No YAML: this runs on one machine for one person, and every value here has a
default that works on that machine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    image: str = "reviewq-reviewer:latest"
    # The container CLI. "podman" gets rootless-friendly flags (see runner._argv).
    runtime: str = "docker"
    # An Anthropic-compatible endpoint to run the reviewer against instead of
    # Anthropic itself, e.g. a local Ollama (http://host.containers.internal:11434).
    # Set, it replaces the API key / session as the way the review is paid for.
    anthropic_base_url: str | None = None
    # Model name that endpoint serves, passed to the CLI as ANTHROPIC_MODEL.
    model: str | None = None
    # Bearer token sent to that endpoint. Ollama ignores it but the CLI wants one.
    auth_token: str = "ollama"
    # --network for the container. Unset = the runtime's default. "host" is the
    # blunt way to reach a service bound to the host's localhost.
    network: str | None = None
    # Where the queue and the results live. One file, one writer.
    state_dir: Path = Path.home() / ".local/share/reviewq"
    # How the reviewer pays for itself, in the order they are tried. An API key
    # wins because a machine that has one is usually a machine nobody logs into
    # interactively, which is also the one where the session would go stale.
    api_key: str | None = None  # ANTHROPIC_API_KEY
    # Otherwise the account's own session, mounted read-only into each throwaway
    # container. Not refreshed from in there: the host CLI keeps it current, and a
    # review lasts minutes. Only the file, never ~/.claude — that directory holds
    # memories and project state a bypassPermissions agent has no business reading.
    credentials: Path = Path.home() / ".claude/.credentials.json"
    # HOME inside the image, which is where the CLI looks for its credentials. A
    # variable and not a constant because it belongs to whoever built the image: a
    # rebuild that renames the user breaks the mount with no error that says so.
    container_home: Path = Path("/home/reviewer")
    # Read from the checkout, not from a pinned ref. Robbie pins it because a PR
    # must not rewrite the rules it is judged by; here the repo IS the authority.
    review_command: str = ".claude/commands/code-review.md"
    # origin/, not the local branch: inside the clone `origin` IS your workspace, so
    # this is the base as your machine has it, and it exists even when the local
    # branch was never checked out in the clone
    base_ref: str = "origin/main"
    effort: str = "high"
    max_concurrent: int = 2
    timeout_s: int = 1800
    cpus: str = "2"
    memory: str = "4g"
    pids_limit: int = 512
    # OFF by default: on an AppArmor host (Ubuntu, Pop!_OS) the docker-default
    # profile reads the exec-time transition as gaining privileges and every
    # execve in the container returns EPERM. It still runs unprivileged with all
    # caps dropped. Robbie learned this one the hard way; see its DockerConfig.
    no_new_privileges: bool = False
    host: str = "127.0.0.1"
    port: int = 4030
    # a second listener, for a container that gets this file and nothing else of
    # the host's network. Unset = TCP only
    socket: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.state_dir / "reviewq.db"

    @classmethod
    def from_env(cls) -> Config:
        d = cls()
        return cls(
            image=os.environ.get("REVIEWQ_IMAGE", d.image),
            runtime=os.environ.get("REVIEWQ_RUNTIME", d.runtime),
            anthropic_base_url=os.environ.get("REVIEWQ_ANTHROPIC_BASE_URL", "").strip() or None,
            model=os.environ.get("REVIEWQ_MODEL", "").strip() or None,
            auth_token=os.environ.get("REVIEWQ_ANTHROPIC_AUTH_TOKEN", d.auth_token),
            network=os.environ.get("REVIEWQ_NETWORK", "").strip() or None,
            state_dir=Path(os.environ.get("REVIEWQ_STATE_DIR", d.state_dir)),
            api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip() or None,
            credentials=Path(os.environ.get("REVIEWQ_CREDENTIALS", d.credentials)),
            container_home=Path(os.environ.get("REVIEWQ_CONTAINER_HOME", d.container_home)),
            review_command=os.environ.get("REVIEWQ_REVIEW_COMMAND", d.review_command),
            base_ref=os.environ.get("REVIEWQ_BASE_REF", d.base_ref),
            effort=os.environ.get("REVIEWQ_EFFORT", d.effort),
            max_concurrent=int(os.environ.get("REVIEWQ_MAX_CONCURRENT", d.max_concurrent)),
            timeout_s=int(os.environ.get("REVIEWQ_TIMEOUT_S", d.timeout_s)),
            cpus=os.environ.get("REVIEWQ_CPUS", d.cpus),
            memory=os.environ.get("REVIEWQ_MEMORY", d.memory),
            pids_limit=int(os.environ.get("REVIEWQ_PIDS_LIMIT", d.pids_limit)),
            no_new_privileges=_flag("REVIEWQ_NO_NEW_PRIVILEGES", d.no_new_privileges),
            host=os.environ.get("REVIEWQ_HOST", d.host),
            port=int(os.environ.get("REVIEWQ_PORT", d.port)),
            socket=Path(s) if (s := os.environ.get("REVIEWQ_SOCKET", "").strip()) else None,
        )


    def why_it_cannot_pay(self) -> str | None:
        """Why no review can run, if none can. Asked at boot rather than on the
        first request: a machine with neither is a setup that never worked, and
        finding out ten minutes into a review is the expensive way to learn it."""
        if self.anthropic_base_url or self.api_key or self.credentials.is_file():
            return None
        return (
            f"no model credentials: set ANTHROPIC_API_KEY, set REVIEWQ_ANTHROPIC_BASE_URL, or log in with `claude` "
            f"so that {self.credentials} exists (REVIEWQ_CREDENTIALS points elsewhere)"
        )


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in ("1", "true", "yes") if raw else default
