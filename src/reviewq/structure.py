"""A second chance for a review that judged the code but never said so in the blocks.

Small local models read the diff, find the bug, and then end on prose: the review is
there, the three blocks are not. Failing that run throws away the expensive half. Ollama
can constrain a reply to a JSON schema, so one more short call, with no tools and no
repo, only the reviewer's own words, reads the verdict, summary and findings back out.

It only ever reads. It is told to copy findings, never to add one, and when the text
holds no judgement it answers "none" instead of inventing an `ok`, because `ok` is what
a caller stops on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

from reviewq.config import Config
from reviewq.contract import SEVERITIES, VERDICTS, Blocks

logger = logging.getLogger("reviewq.structure")

NO_JUDGEMENT = "none"

# Appended to the summary so whoever reads the result knows the blocks are a reading
# of the review, not the reviewer's own.
NOTE = (
    "_The reviewer wrote no verdict blocks; "
    "a structured-output pass read them out of its review._"
)

SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": [*VERDICTS, NO_JUDGEMENT]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": [s.title() for s in SEVERITIES]},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["path", "line", "severity", "title", "body"],
            },
        },
    },
    "required": ["verdict", "summary", "findings"],
}

INSTRUCTIONS = """\
You turn a code review someone else wrote into JSON. Copy, never judge: every finding \
must be a problem the review itself states, with the file and line it gives (line 0 \
when it gives none). Do not add, merge or soften findings.

severity: map the review's own words. Bugs, crashes, data loss, security holes and \
anything it calls blocking, P0 or P1 are Critical or Must-Fix; improvements it asks \
for are Should-Fix; style and wording are Nitpick.

verdict: needs-work if any finding is Critical or Must-Fix; comment if the worst is \
Should-Fix; ok if the review finished and found nothing above a nitpick; none if the \
text is not a finished review (it stops mid-way, only plans what to read, or never \
judges the code).

summary: two or three sentences in the review's own voice."""


async def recover(cfg: Config, raw: str) -> Blocks | None:
    """The blocks the review implies, or None when there is no reading to be had.
    Never raises: a failed second chance leaves the run as failed as it was."""
    model = cfg.structure_model or cfg.model
    if not cfg.ollama_url or not model or not raw.strip():
        return None
    # No "think": false, though it would be faster. On Ollama 0.20 a thinking model
    # asked not to think ignores `format` and answers in prose; left to think, its
    # answer is held to the schema.
    body = {
        "model": model,
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": INSTRUCTIONS},
            {"role": "user", "content": raw},
        ],
    }
    url = cfg.ollama_url.rstrip("/") + "/api/chat"
    try:
        reply = await asyncio.to_thread(_post, url, body, cfg.structure_timeout_s)
        return as_blocks(json.loads(reply["message"]["content"]))
    except (OSError, ValueError, KeyError, TypeError) as ex:
        logger.warning("structured recovery against %s failed: %s", url, ex)
        return None


def as_blocks(data: object) -> Blocks | None:
    """The schema is a request, not a guarantee: check it again on this side."""
    if not isinstance(data, dict):
        return None
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        return None
    findings = data.get("findings")
    summary = str(data.get("summary") or "").strip()
    return Blocks(
        verdict=verdict,
        summary=f"{summary}\n\n{NOTE}" if summary else NOTE,
        findings=[f for f in findings if isinstance(f, dict)] if isinstance(findings, list) else [],
    )


def _post(url: str, body: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        reply = json.loads(response.read().decode())
    if not isinstance(reply, dict):
        raise ValueError("the reply is not a JSON object")
    return reply
