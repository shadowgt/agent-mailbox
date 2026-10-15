"""Walk one task through the whole mailbox lifecycle in a throwaway repository.

    python examples/demo.py                 # run and print each step
    python examples/demo.py --save-samples  # also copy the envelopes to examples/envelopes/

Codex sends a read-only review request to Claude; Claude ACKs, accepts and returns a result;
Codex closes the task. Every message goes through ``publish`` / ``reply`` of the real tool, so
the saved samples are valid envelopes. Nothing outside a temporary directory is written (except
the samples with ``--save-samples``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import agent_mailbox as mailbox  # noqa: E402

CLAUDE_SESSION = "11111111-1111-4111-8111-111111111111"
CODEX_THREAD = "22222222-2222-4222-8222-222222222222"


def show_inbox(repo: Path, who: str, session: str) -> None:
    pending = mailbox.inbox(repo, who, session_id=session)
    print(f"  {who} inbox: {[(m['kind'], m['task_id']) for m in pending] or 'empty'}")


def make_request(repo: Path) -> dict[str, Any]:
    design = repo / "docs" / "design.md"
    design.parent.mkdir(parents=True, exist_ok=True)
    design.write_text("# design under review\n", encoding="utf-8")
    now = datetime.now(UTC)
    return {
        "version": 1,
        "id": str(uuid4()),
        "thread_id": str(uuid4()),
        "task_id": "design-review-01",
        "kind": "request",
        "sender": "codex",
        "recipient": "claude",
        "sender_session": CODEX_THREAD,
        "recipient_session": CLAUDE_SESSION,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "reply_to": None,
        "base_commit": "0" * 40,
        "scope": {"mode": "read_only", "paths": ["docs/design.md"]},
        "body": (
            "Please review docs/design.md read-only.\n"
            "User authorization: the user asked Codex in chat on <date> to request this review.\n"
            "Deliverable: findings as CRITICAL/HIGH/MEDIUM/LOW with file:line, by tomorrow.\n"
            "Do not edit any file."
        ),
        "artifacts": [
            {"path": "docs/design.md", "sha256": hashlib.sha256(design.read_bytes()).hexdigest()}
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--save-samples", action="store_true")
    args = parser.parse_args()
    repo = Path(tempfile.mkdtemp(prefix="mailbox-demo-"))
    request = make_request(repo)
    steps: list[tuple[str, dict[str, Any]]] = []

    print("1. Codex publishes a request")
    mailbox.publish(repo, request)
    steps.append(("01-request", request))
    show_inbox(repo, "claude", CLAUDE_SESSION)

    print("2. Claude reads it and ACKs (read and understood, not yet accepted)")
    ack = mailbox.reply(
        repo, "claude", request["id"], "ack", "Read and understood.", session_id=CLAUDE_SESSION
    )
    steps.append(("02-ack", ack))

    print("3. Claude accepts (owner, files, completion criteria)")
    accept = mailbox.reply(
        repo,
        "claude",
        request["id"],
        "accept",
        "Accepted: read-only review of docs/design.md, result by tomorrow.",
        session_id=CLAUDE_SESSION,
    )
    steps.append(("03-accept", accept))
    show_inbox(repo, "claude", CLAUDE_SESSION)

    print("4. Claude returns the result")
    result = mailbox.reply(
        repo,
        "claude",
        request["id"],
        "result",
        "No CRITICAL or HIGH findings. LOW: section 2 lacks an example.",
        session_id=CLAUDE_SESSION,
    )
    steps.append(("04-result", result))
    show_inbox(repo, "codex", CODEX_THREAD)

    print("5. Codex verifies and closes the task")
    close = mailbox.reply(
        repo, "codex", result["id"], "close", "Verified; closing.", session_id=CODEX_THREAD
    )
    steps.append(("05-close", close))
    show_inbox(repo, "codex", CODEX_THREAD)
    show_inbox(repo, "claude", CLAUDE_SESSION)

    print("6. An ACK of an ACK is refused (no reply loops)")
    try:
        mailbox.reply(repo, "codex", ack["id"], "ack", "ack of ack", session_id=CODEX_THREAD)
    except ValueError as exc:
        print(f"  refused: {exc}")

    history = mailbox.inbox(repo, "claude", all_messages=True)
    print(f"7. Full history addressed to Claude: {[m['kind'] for m in history]}")
    if args.save_samples:
        out = ROOT / "examples" / "envelopes"
        out.mkdir(parents=True, exist_ok=True)
        for name, item in steps:
            text = json.dumps(item, indent=2, ensure_ascii=False) + "\n"
            (out / f"{name}.json").write_text(text, encoding="utf-8")
        print(f"samples written to {out}")
    shutil.rmtree(repo, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
