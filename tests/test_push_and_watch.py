"""Push to Codex (``notify`` -> ``codex queue``) and wait for Claude (``watch``)."""

from __future__ import annotations

import importlib
import io
import subprocess
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from tests.test_agent_mailbox import message

mailbox = importlib.import_module("scripts.agent_mailbox")

CODEX_THREAD = "22222222-2222-4222-8222-222222222222"
CLAUDE_SESSION = "11111111-1111-4111-8111-111111111111"


def to_codex(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "sender": "claude",
        "recipient": "codex",
        "sender_session": CLAUDE_SESSION,
        "recipient_session": CODEX_THREAD,
    }
    fields.update(changes)
    return message(**fields)


class FakeRun:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        assert kwargs["timeout"] == 60 and kwargs["check"] is False
        return subprocess.CompletedProcess(argv, self.code, "Queued message x for thread y.", "")


def test_notify_runs_codex_queue_on_the_named_thread_with_a_safe_one_line_text(
    tmp_path: Path,
) -> None:
    item = to_codex(kind="request")
    mailbox.publish(tmp_path, item)
    run = FakeRun()
    result = mailbox.notify(tmp_path, item["id"], codex="codex.cmd", run=run)
    assert result["queued"] is True and result["thread"] == CODEX_THREAD
    argv = run.calls[0]
    assert argv[:4] == ["codex.cmd", "queue", "--thread", CODEX_THREAD]
    assert argv[4] == "--message"
    text = argv[5]
    assert item["id"] in text and f".agent-bridge/mailboxes/codex/{item['id']}.json" in text
    assert "not a new user approval" in text
    assert mailbox.SAFE_TEXT.fullmatch(text)


def test_dry_run_prints_the_command_without_running_anything(tmp_path: Path) -> None:
    item = to_codex(kind="handoff")
    mailbox.publish(tmp_path, item)
    run = FakeRun()
    result = mailbox.notify(tmp_path, item["id"], dry_run=True, run=run)
    assert result["queued"] is False and result["argv"][1] == "queue"
    assert run.calls == []


def test_acks_messages_to_claude_and_missing_threads_are_never_pushed(tmp_path: Path) -> None:
    request = to_codex(kind="request")
    mailbox.publish(tmp_path, request)
    no_thread = to_codex(recipient_session=None)
    mailbox.publish(tmp_path, no_thread)
    to_claude = message()
    mailbox.publish(tmp_path, to_claude)
    to_agy = message(sender="claude", recipient="agy", recipient_session=str(mailbox.uuid4()))
    mailbox.publish(tmp_path, to_agy)
    to_local = message(sender="claude", recipient="local")
    mailbox.publish(tmp_path, to_local)
    reply = mailbox.reply(tmp_path, "codex", request["id"], "ack", "read", session_id=CODEX_THREAD)
    run = FakeRun()
    assert mailbox.notify(tmp_path, to_claude["id"], run=run)["queued"] is False
    agy_notify = mailbox.notify(tmp_path, to_agy["id"], run=run)
    assert agy_notify["queued"] is False and "Codex-only" in agy_notify["reason"]
    local_notify = mailbox.notify(tmp_path, to_local["id"], run=run)
    assert local_notify["queued"] is False and "Codex-only" in local_notify["reason"]
    with pytest.raises(ValueError, match="only messages to codex"):
        mailbox.notify_argv(tmp_path, to_agy, "codex")
    with pytest.raises(ValueError, match="recipient_session"):
        mailbox.notify(tmp_path, no_thread["id"], codex="codex", run=run)
    assert reply["recipient"] == "claude"
    ack_to_codex = mailbox.reply(
        tmp_path, "claude", to_claude["id"], "ack", "read", session_id=None
    )
    with pytest.raises(ValueError, match="never pushed"):
        mailbox.notify(tmp_path, ack_to_codex["id"], codex="codex", run=run)
    assert run.calls == []


def test_notify_help_explicitly_states_codex_only() -> None:
    top_help = " ".join(mailbox.parser().format_help().split())
    assert "Codex" in top_help and "agy has no queue command" in top_help


def test_a_failing_codex_queue_is_an_error(tmp_path: Path) -> None:
    item = to_codex()
    mailbox.publish(tmp_path, item)
    with pytest.raises(ValueError, match="exit code 3"):
        mailbox.notify(tmp_path, item["id"], codex="codex", run=FakeRun(code=3))


def test_the_codex_path_comes_from_the_flag_then_the_environment_then_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "codex.cmd"
    exe.write_text("@echo off\n", encoding="utf-8")
    assert mailbox.codex_executable(str(exe)) == str(exe)
    monkeypatch.setenv(mailbox.CODEX_ENV, str(exe))
    assert mailbox.codex_executable() == str(exe)
    monkeypatch.delenv(mailbox.CODEX_ENV)
    monkeypatch.setattr(mailbox.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="codex CLI not found"):
        mailbox.codex_executable()


def test_an_unsafe_repository_path_is_replaced_in_the_text(tmp_path: Path) -> None:
    odd = tmp_path / "a&b(c)"
    odd.mkdir()
    item = to_codex()
    mailbox.publish(odd, item)
    text = mailbox.notify_text(odd, item)
    assert "the shared repository" in text and "&" not in text


def test_publish_with_notify_from_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = to_codex()
    path = tmp_path / "request.json"
    path.write_bytes(mailbox.encode(item))
    run = FakeRun()
    monkeypatch.setattr(mailbox.subprocess, "run", run)
    exe = tmp_path / "codex.cmd"
    exe.write_text("@echo off\n", encoding="utf-8")
    out = io.StringIO()
    with redirect_stdout(out):
        code = mailbox.main(
            [
                "--root",
                str(tmp_path),
                "publish",
                "--file",
                str(path),
                "--notify",
                "--codex",
                str(exe),
            ]
        )
    assert code == 0 and '"queued": true' in out.getvalue()
    assert run.calls[0][3] == CODEX_THREAD


def test_notify_failure_on_the_command_line_says_why(tmp_path: Path) -> None:
    item = to_codex(recipient_session=None)
    mailbox.publish(tmp_path, item)
    err = io.StringIO()
    with redirect_stderr(err):
        code = mailbox.main(["--root", str(tmp_path), "notify", "--id", item["id"], "--dry-run"])
    assert code == 2 and "recipient_session" in err.getvalue()


def test_watch_returns_a_new_message_once_and_times_out_otherwise(tmp_path: Path) -> None:
    clock = [0.0]

    def tick(seconds: float) -> None:
        clock[0] += seconds

    found = mailbox.watch(tmp_path, "claude", timeout=5, poll=1, now=lambda: clock[0], sleep=tick)
    assert found is None and clock[0] >= 5
    item = message()
    mailbox.publish(tmp_path, item)
    assert mailbox.watch(tmp_path, "claude", timeout=1, now=lambda: 0.0)["id"] == item["id"]
    clock[0] = 0.0
    again = mailbox.watch(tmp_path, "claude", timeout=2, poll=1, now=lambda: clock[0], sleep=tick)
    assert again is None  # the same message never wakes the same watcher twice


def test_watch_wakes_when_another_process_publishes(tmp_path: Path) -> None:
    item = message(recipient_session=CLAUDE_SESSION)
    timer = threading.Timer(0.3, lambda: mailbox.publish(tmp_path, item))
    timer.start()
    out = io.StringIO()
    with redirect_stdout(out):
        code = mailbox.main(
            [
                "--root",
                str(tmp_path),
                "watch",
                "--recipient",
                "claude",
                "--session-id",
                CLAUDE_SESSION,
                "--timeout",
                "10",
                "--poll",
                "0.1",
            ]
        )
    timer.join()
    assert code == 0 and item["id"] in out.getvalue()
    err = io.StringIO()
    with redirect_stderr(err):
        code = mailbox.main(
            [
                "--root",
                str(tmp_path),
                "watch",
                "--recipient",
                "claude",
                "--session-id",
                CLAUDE_SESSION,
                "--timeout",
                "0.2",
                "--poll",
                "0.1",
            ]
        )
    assert code == 1 and "no new message" in err.getvalue()
