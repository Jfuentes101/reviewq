"""The MCP surface: ask for a review, then come back for it.

Streamable HTTP on localhost, not stdio, and that is load-bearing. A stdio server
is started per client, so every Claude instance would get its own queue of one and
`max_concurrent` would mean nothing. One process, one queue, many clients.

Four tools and no more. Nothing here can cancel, retry or edit anything — asking
and reading is the whole surface.
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path
from typing import Any

import anyio
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from reviewq.config import Config
from reviewq.contract import CONTEXT_HELP
from reviewq.reviews import Queue, as_result

logger = logging.getLogger("reviewq.server")

# the Host header a client over the socket sends: http://reviewq/mcp
SOCKET_HOST = "reviewq"


def build_server(cfg: Config, queue: Queue) -> MCPServer:
    mcp: MCPServer = MCPServer(
        name="reviewq",
        version="0.1.0",
        instructions=(
            "A second-opinion code review, run by a fresh agent in a throwaway "
            "container against a read-only copy of a local workspace. Reviews take "
            "minutes: request one, keep working, come back for the result."
        ),
    )

    @mcp.tool(
        description=(
            "Queue a code review of a local git workspace. Returns an id "
            "immediately — the review itself takes minutes, so poll review_status "
            "and then read review_results.\n\n"
            "The reviewer is a FRESH agent: it has never seen your conversation, "
            "your plan, or your reasoning. That is the point, and it is also why "
            "`context` matters. " + CONTEXT_HELP + "\n\n"
            "Write it about the SYSTEM, never about the reviewer. A sentence whose "
            "subject is the reviewer — ignore this, skip that, do not flag the "
            "other — is not context and will not be honoured: it narrows what gets "
            "examined, which hides real defects in the same code. A statement of "
            "intended behaviour narrows what counts as a defect without narrowing "
            "what gets read, which is the whole trick.\n\n"
            "The test: could this sentence have been written before the code "
            "existed, in a ticket or a spec? Then it is context.\n\n"
            "Good: 'A group booking sends a notification for every reservation in "
            "it, not only the parent.' The fan-out stops being a finding, and a "
            "query-per-reservation inside it still is. Bad: 'ignore the "
            "notification changes' — that buries both. Also bad: 'we used a service "
            "object because the controller was crowded' — that is the design "
            "judgement you are paying for, so do not pre-empt it.\n\n"
            "Reviews the diff between base_ref and the working tree, uncommitted "
            "tracked changes included. Untracked files are invisible until added."
        )
    )
    async def request_review(
        workspace: str, context: str = "", base_ref: str = ""
    ) -> dict[str, Any]:
        try:
            path = _workspace(workspace)
        except ValueError as ex:
            # handed back as data: a tool that raises ends the caller's turn, and a
            # bad path is something the caller can fix and retry
            return {"error": str(ex)}
        try:
            review_id = await queue.submit(
                path, base_ref=base_ref or cfg.base_ref, context=context
            )
        except Exception as ex:  # noqa: BLE001 — same reasoning as the bad path above
            return {"error": f"could not snapshot {path}: {ex}"}
        return {
            "id": review_id,
            "state": "queued",
            "waiting": queue.waiting(),
            "note": "poll review_status; results land in review_results",
        }

    @mcp.tool(
        description=(
            "Every review this server knows about, newest first: id, workspace, "
            "state and verdict. Use it to see what is queued and what is running."
        )
    )
    async def queue_status(limit: int = 50) -> dict[str, Any]:
        rows = queue.listing(max(1, min(limit, 200)))
        return {
            "running": sum(1 for r in rows if r["state"] == "running"),
            "queued": sum(1 for r in rows if r["state"] == "queued"),
            "max_concurrent": cfg.max_concurrent,
            "reviews": [
                {
                    "id": r["id"],
                    "state": r["state"],
                    "verdict": r["verdict"],
                    "workspace": r["workspace"],
                    "base_ref": r["base_ref"],
                }
                for r in rows
            ],
        }

    @mcp.tool(
        description=(
            "Where one review stands: queued, running, done or failed. Cheap — poll "
            "this rather than review_results while you wait."
        )
    )
    async def review_status(review_id: int) -> dict[str, Any]:
        row = queue.get(review_id)
        if row is None:
            return {"error": f"no review {review_id}"}
        return {
            "id": row["id"],
            "state": row["state"],
            "verdict": row["verdict"],
            "error": row["error"],
        }

    @mcp.tool(
        description=(
            "The review itself: verdict (needs-work / comment / ok), a summary, and "
            "the findings as objects with path, line, severity, title and body. "
            "Only meaningful once review_status says done; a failed review carries "
            "the reason instead."
        )
    )
    async def review_results(review_id: int) -> dict[str, Any]:
        row = queue.get(review_id)
        if row is None:
            return {"error": f"no review {review_id}"}
        if row["state"] in ("queued", "running"):
            return {"id": row["id"], "state": row["state"],
                    "note": "not finished yet; poll review_status"}
        return as_result(row)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "queued": queue.waiting()})

    return mcp


def _workspace(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"workspace must be an absolute path, got {raw!r}")
    path = path.resolve()
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    if not (path / ".git").exists():
        raise ValueError(f"{path} is not a git repository; there is no diff to review")
    return path


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("REVIEWQ_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    cfg = Config.from_env()
    if (blocked := cfg.why_it_cannot_pay()) is not None:
        raise SystemExit(f"reviewq: {blocked}")
    queue = Queue(cfg)
    logger.info(
        "reviewq up on %s:%d — image=%s, auth=%s, %d concurrent, state in %s",
        cfg.host, cfg.port, cfg.image,
        f"{cfg.anthropic_base_url} model={cfg.model}" if cfg.anthropic_base_url
        else "api key" if cfg.api_key else "oauth session",
        cfg.max_concurrent, cfg.state_dir,
    )
    allowed = [f"{cfg.host}:{cfg.port}", cfg.host, f"localhost:{cfg.port}", "localhost"]
    if cfg.socket is not None:
        allowed.append(SOCKET_HOST)
    app = build_server(cfg, queue).streamable_http_app(
        # each Claude instance is a short-lived client; there is no session worth
        # keeping, and the queue's state lives in SQLite rather than in a session
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed, allowed_origins=allowed
        ),
        host=cfg.host,
    )
    anyio.run(uvicorn.Server(uvicorn.Config(app, log_level="info")).serve, listeners(cfg))


def listeners(cfg: Config) -> list[socket.socket]:
    """One server, one lifespan, both sockets: a second uvicorn would start the
    session manager twice, and it refuses."""
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp.bind((cfg.host, cfg.port))
    out = [tcp]
    if cfg.socket is not None:
        cfg.socket.parent.mkdir(parents=True, exist_ok=True)
        cfg.socket.unlink(missing_ok=True)
        uds = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        uds.bind(str(cfg.socket))
        # whoever can open this can queue a review of any repo on the host
        cfg.socket.chmod(0o600)
        out.append(uds)
    return out


if __name__ == "__main__":
    main()
