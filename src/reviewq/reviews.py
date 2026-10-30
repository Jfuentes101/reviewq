"""The queue: what was asked for, what is running, what came back.

SQLite because the answer to "is it done yet" has to survive a restart of this
process, and because one file is easier to look at than a log. Single writer,
sub-millisecond calls, no async wrapper worth its weight.

There is no worker pool. A request writes its row and starts a task that waits on
a semaphore, so "queued" is a real observable state rather than something hidden
inside a scheduler — which is the whole reason this exists as a queue and not as
a function call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from reviewq.config import Config
from reviewq.contract import Blocks, preamble, severity_count, verdict_understated
from reviewq.runner import Run, Target, kill_stale, run_review, snapshot
from reviewq.structure import recover

logger = logging.getLogger("reviewq.queue")

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace   TEXT    NOT NULL,
    base_ref    TEXT    NOT NULL,
    base_sha    TEXT,               -- resolved on the host: names mean something
    head_sha    TEXT,               -- else inside the clone. See runner.snapshot.
    context     TEXT    NOT NULL DEFAULT '',
    state       TEXT    NOT NULL CHECK (state IN ('queued','running','done','failed')),
    verdict     TEXT,
    summary     TEXT,
    findings    TEXT,               -- the JSON array, as the model emitted it
    raw         TEXT,               -- the whole response, for when parsing came up short
    error       TEXT,
    cost_usd    REAL,
    duration_s  REAL,
    created_at  INTEGER NOT NULL,
    started_at  INTEGER,
    finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_reviews_state ON reviews(state, id);
"""

Runner = Callable[[int, Path, Target, str], Awaitable[Run]]
Snapshotter = Callable[[Path, str], Awaitable[Target]]
Recoverer = Callable[[str], Awaitable[Blocks | None]]


def now_ms() -> int:
    return int(time.time() * 1000)


class Queue:
    def __init__(
        self,
        cfg: Config,
        *,
        runner: Runner | None = None,
        snapshotter: Snapshotter | None = None,
        recoverer: Recoverer | None = None,
    ) -> None:
        self.cfg = cfg
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is safe while this stays one process with one
        # writer; sqlite3 itself is serialized.
        self.conn = sqlite3.connect(cfg.db_path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._sem = asyncio.Semaphore(cfg.max_concurrent)
        # tasks are only referenced here; without this the loop may collect one
        # mid-review and the row would sit in 'running' forever
        self._tasks: set[asyncio.Task[None]] = set()
        self._run: Runner = runner or self._spawn
        self._snapshot: Snapshotter = snapshotter or snapshot
        self._recover: Recoverer | None = recoverer or (self._structure if cfg.ollama_url else None)
        self._reap()

    def _reap(self) -> None:
        """Anything left in flight belonged to a process that is gone.

        Queued too, not just running: the task that would have picked it up died
        with it, so nothing is ever going to move that row.

        The row is the cheap half. A 'running' one had a container, and that
        container outlived the process that was waiting on it — still reviewing,
        still spending — so the row and the container get closed together.
        """
        orphans = [
            int(row["id"]) for row in self.conn.execute(
                "SELECT id FROM reviews WHERE state IN ('queued','running')"
            )
        ]
        if not orphans:
            return
        self.conn.execute(
            "UPDATE reviews SET state='failed', error='reviewq restarted', finished_at=? "
            "WHERE state IN ('queued','running')",
            (now_ms(),),
        )
        logger.warning("marked %d orphaned review(s) failed at boot", len(orphans))
        kill_stale(self.cfg, orphans)

    async def _spawn(self, review_id: int, workspace: Path, target: Target, prompt: str) -> Run:
        return await run_review(self.cfg, review_id, workspace, target=target, prompt=prompt)

    async def _structure(self, raw: str) -> Blocks | None:
        return await recover(self.cfg, raw)

    async def _recovered(self, review_id: int, run: Run) -> Run:
        """A run that reviewed but wrote no verdict gets one structured-output pass
        over its own words before it counts as failed. Only that case: a run with a
        verdict is the reviewer's judgement and is never read twice."""
        if (self._recover is None or not run.ok or run.blocks is None
                or run.blocks.verdict or not run.raw.strip()):
            return run
        blocks = await self._recover(run.raw)
        if blocks is None or not blocks.verdict:
            return run
        logger.info("review %d: verdict read out of the raw review (%s)", review_id, blocks.verdict)
        return replace(run, blocks=blocks)

    # ----- what the MCP tools call ---------------------------------------

    async def submit(self, workspace: Path, *, base_ref: str, context: str) -> int:
        """Freeze the snapshot, record the request, start waiting for a slot.

        The snapshot is taken HERE and not when the slot frees, which is the whole
        promise of the queue: you asked for a review of what you had at this moment,
        so you can keep typing. Taken at start time it would judge whatever the
        workspace happened to hold minutes later, possibly a half-finished edit.

        Returns as soon as the row exists — a review takes minutes and no tool call
        should be held open for that. A workspace git cannot read fails right here,
        where the caller is still listening.
        """
        target = await self._snapshot(workspace, base_ref)
        cur = self.conn.execute(
            "INSERT INTO reviews "
            "(workspace, base_ref, base_sha, head_sha, context, state, created_at) "
            "VALUES (?,?,?,?,?, 'queued', ?)",
            (str(workspace), base_ref, target.base_sha, target.sha, context, now_ms()),
        )
        review_id = int(cur.lastrowid or 0)
        task = asyncio.create_task(self._work(review_id, workspace, context, target))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return review_id

    def get(self, review_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM reviews WHERE id=?", (review_id,)
        ).fetchone()

    def listing(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT id, workspace, base_ref, state, verdict, created_at, finished_at "
            "FROM reviews ORDER BY id DESC LIMIT ?",
            (limit,),
        ))

    def waiting(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE state='queued'"
        ).fetchone()[0])

    # ----- the lifecycle --------------------------------------------------

    async def _work(
        self, review_id: int, workspace: Path, context: str, target: Target
    ) -> None:
        try:
            async with self._sem:
                self.conn.execute(
                    "UPDATE reviews SET state='running', started_at=? WHERE id=?",
                    (now_ms(), review_id),
                )
                prompt = preamble(
                    base_ref=target.base_ref, base_sha=target.base_sha,
                    head=target.sha, branch=target.branch, context=context,
                )
                run = await self._recovered(
                    review_id, await self._run(review_id, workspace, target, prompt)
                )
        except Exception as ex:  # noqa: BLE001 — a crashed task must leave a readable row
            logger.exception("review %d crashed", review_id)
            self._fail(review_id, str(ex))
            return
        self._finish(review_id, run)

    def _fail(self, review_id: int, error: str) -> None:
        self.conn.execute(
            "UPDATE reviews SET state='failed', error=?, finished_at=? WHERE id=?",
            (error, now_ms(), review_id),
        )

    def _finish(self, review_id: int, run: Run) -> None:
        if not run.ok or run.blocks is None:
            self.conn.execute(
                "UPDATE reviews SET state='failed', error=?, raw=?, duration_s=?, "
                "cost_usd=?, finished_at=? WHERE id=?",
                (run.error or "unknown", run.raw, run.duration_s, run.cost_usd,
                 now_ms(), review_id),
            )
            return
        blocks = run.blocks
        # A run with no verdict is a failed review, not a done one with a hole in
        # it: whoever asked is waiting on a judgement, and "" is not one. The raw
        # response is kept either way, which is what makes that recoverable by hand.
        state = "done" if blocks.verdict else "failed"
        self.conn.execute(
            "UPDATE reviews SET state=?, verdict=?, summary=?, findings=?, raw=?, "
            "error=?, cost_usd=?, duration_s=?, finished_at=? WHERE id=?",
            (
                state, blocks.verdict, blocks.summary, json.dumps(blocks.findings), run.raw,
                None if blocks.verdict else "the run emitted no verdict block",
                run.cost_usd, run.duration_s, now_ms(), review_id,
            ),
        )

    async def drain(self) -> None:
        """Wait for everything in flight. Tests use it; nothing else needs to."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    def close(self) -> None:
        self.conn.close()


def as_result(row: sqlite3.Row) -> dict:
    """One row, shaped for whoever asked. Counts included because the first thing
    a caller does with findings is ask how bad it is."""
    findings = json.loads(row["findings"] or "[]")
    result = {
        "id": row["id"],
        "state": row["state"],
        "verdict": row["verdict"],
        "summary": row["summary"] or "",
        "findings": findings,
        "blocking": severity_count(findings, blocking=True),
        "should_fix": severity_count(findings, blocking=False),
        # The verdict is the model's and stays the model's — it is the judgement
        # being bought, so this reports the contradiction rather than editing it.
        # It matters because `ok` is what a loop stops on, and a Must-fix under an
        # `ok` would end the round by agreeing with nobody.
        "verdict_understated": verdict_understated(row["verdict"], findings),
        "workspace": row["workspace"],
        "base_ref": row["base_ref"],
        "base_sha": row["base_sha"],
        "head_sha": row["head_sha"],
        "cost_usd": row["cost_usd"],
        "duration_s": row["duration_s"],
        "error": row["error"],
    }
    # Failures only. `raw` repeats the summary and the findings verbatim, so on a
    # good review it is a third copy in the caller's context and pays for nothing;
    # on a failed one it is the only place the answer still exists.
    if row["state"] == "failed":
        result["raw"] = row["raw"] or ""
    return result
