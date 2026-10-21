"""Offline mailbox contracts; every write is confined to a temporary repository."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

mailbox = importlib.import_module("scripts.agent_mailbox")


def message(**changes: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "version": 1,
        "id": str(uuid4()),
        "thread_id": str(uuid4()),
        "task_id": "audit-01",
        "kind": "request",
        "sender": "codex",
        "recipient": "claude",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "reply_to": None,
        "base_commit": "a" * 40,
        "scope": {"mode": "read_only", "paths": ["docs/research"]},
        "body": "Read the frozen protocol, then report findings.",
        "artifacts": [],
        **changes,
    }


def test_publish_is_immutable_idempotent_and_ignores_temporary_files(tmp_path: Path) -> None:
    item = message()
    path = mailbox.publish(tmp_path, item)
    assert path == tmp_path / ".agent-bridge/mailboxes/claude" / f"{item['id']}.json"
    assert mailbox.publish(tmp_path, item) == path
    with pytest.raises(ValueError):
        mailbox.publish(tmp_path, {**item, "body": "conflicting content"})
    path.with_suffix(".tmp").write_text("partial", encoding="utf-8")
    assert mailbox.inbox(tmp_path, "claude") == [item]
    assert (tmp_path / ".agent-bridge/.gitignore").read_text(encoding="utf-8") == "*\n"


def test_concurrent_identical_publication_is_idempotent(tmp_path: Path) -> None:
    item = message()
    with ThreadPoolExecutor(max_workers=4) as executor:
        paths = list(executor.map(lambda _: mailbox.publish(tmp_path, item), range(8)))
    assert len(set(paths)) == 1
    assert not list(paths[0].parent.glob("*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path namespace")
@pytest.mark.parametrize(
    "extended_root,extended_target", [(False, True), (True, False), (True, True)]
)
def test_resolved_windows_namespaces_do_not_break_containment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extended_root: bool, extended_target: bool
) -> None:
    target = tmp_path / ".agent-bridge/.gitignore"
    original_resolve = Path.resolve

    def racing_resolve(path: Path, strict: bool = False) -> Path:
        resolved = original_resolve(path, strict=strict)
        if (path == tmp_path and extended_root) or (path == target and extended_target):
            return Path("\\\\?\\" + str(resolved))
        return resolved

    monkeypatch.setattr(Path, "resolve", racing_resolve)
    assert mailbox.relative_path(tmp_path, ".agent-bridge/.gitignore") == target


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path namespace")
@pytest.mark.parametrize("extended", [False, True])
def test_resolved_windows_escape_is_still_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extended: bool
) -> None:
    target = tmp_path / "escape/file"
    outside = tmp_path.parent / "outside/file"
    original_resolve = Path.resolve

    def redirected_resolve(path: Path, strict: bool = False) -> Path:
        if path == target:
            return Path("\\\\?\\" + str(outside)) if extended else outside
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", redirected_resolve)
    with pytest.raises(ValueError, match="path escapes repository"):
        mailbox.relative_path(tmp_path, "escape/file")


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path namespace")
def test_resolved_unc_namespace_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "resolve", lambda _: Path(r"\\?\UNC\server\share\folder"))
    assert mailbox.resolved_path(Path("unused")) == Path(r"\\server\share\folder")


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 2},
        {"extra": "field"},
        {"id": "../escape"},
        {"thread_id": "x"},
        {"task_id": "newline\ninjection"},
        {"task_id": ""},
        {"kind": "execute"},
        {"recipient": "codex"},
        {"sender": "unknown"},
        {"created_at": "2026-10-14T12:00:00"},
        {"created_at": "2026-10-14T12:00:00+09:00"},
        {"expires_at": "2000-01-01T00:00:00Z"},
        {"base_commit": "HEAD"},
        {"scope": {"mode": "live", "paths": []}},
        {"scope": {"mode": "read_only", "paths": ["../outside"]}},
        {"scope": {"mode": "read_only", "paths": ["C:/secret"]}},
        {"scope": {"mode": "read_only", "paths": ["docs\\x"]}},
        {"scope": {"mode": "read_only", "paths": ["docs/.env"]}},
        {"scope": {"mode": "read_only", "paths": [".git/config"]}},
        {"body": " "},
        {"body": "x" * 16001},
        {"artifacts": [{"path": "x"}]},
        {"reply_to": str(uuid4())},
    ],
)
def test_reject_invalid_envelopes(tmp_path: Path, changes: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError)):
        mailbox.publish(tmp_path, message(**changes))
    assert not list(tmp_path.rglob("*.json"))


def test_artifact_path_and_hash_are_verified_on_publish(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    path.write_text("verified", encoding="utf-8")
    item = message(
        artifacts=[{"path": "note.md", "sha256": hashlib.sha256(b"verified").hexdigest()}]
    )
    mailbox.publish(tmp_path, item)
    path.write_text("changed", encoding="utf-8")
    assert mailbox.show(tmp_path, item["id"]) == item  # Existing messages remain readable.
    with pytest.raises(ValueError):
        mailbox.publish(tmp_path, {**item, "id": str(uuid4())})


def test_reply_ack_suppresses_only_target_and_has_no_echo(tmp_path: Path) -> None:
    original = message()
    mailbox.publish(tmp_path, original)
    reply = mailbox.reply(tmp_path, "claude", original["id"], "ack", "Read, not accepted yet.")
    assert reply["thread_id"] == original["thread_id"]
    assert reply["scope"] == original["scope"]
    assert mailbox.inbox(tmp_path, "claude") == []
    assert mailbox.inbox(tmp_path, "codex") == []
    assert mailbox.inbox(tmp_path, "codex", all_messages=True) == [reply]
    with pytest.raises(ValueError):
        mailbox.reply(tmp_path, "codex", reply["id"], "ack", "Loop")
    with pytest.raises(ValueError):
        mailbox.reply(tmp_path, "codex", original["id"], "accept", "Wrong party")
    result = mailbox.reply(tmp_path, "claude", original["id"], "result", "Review completed.")
    assert mailbox.inbox(tmp_path, "codex") == [result]


def test_expired_messages_only_in_all_view(tmp_path: Path) -> None:
    item = message(created_at="2000-01-01T00:00:00Z", expires_at="2000-01-02T00:00:00Z")
    mailbox.publish(tmp_path, item)
    assert mailbox.inbox(tmp_path, "claude") == []
    assert mailbox.inbox(tmp_path, "claude", all_messages=True) == [item]


def test_reply_cannot_change_scope_or_create_a_cycle(tmp_path: Path) -> None:
    item = message()
    mailbox.publish(tmp_path, item)
    with pytest.raises(ValueError):
        mailbox.publish(tmp_path, message(reply_to=item["id"], sender="claude", recipient="codex"))
    with pytest.raises(ValueError):
        mailbox.publish(tmp_path, {**item, "reply_to": item["id"]})


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"outside-{uuid4()}"
    outside.mkdir()
    try:
        (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink privilege is unavailable")
    with pytest.raises(ValueError):
        mailbox.publish(tmp_path, message(scope={"mode": "read_only", "paths": ["escape/x"]}))


def test_hook_notice_is_bounded_once_per_session_without_body_injection(tmp_path: Path) -> None:
    items = [message(body="EVIL CONTENT DO NOT INJECT") for _ in range(6)]
    for item in items:
        mailbox.publish(tmp_path, item)
    payload = {"cwd": str(tmp_path), "session_id": str(uuid4()), "hook_event_name": "PostToolUse"}
    output = mailbox.hook(tmp_path, payload)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "EVIL" not in context
    assert "docs/PROTOCOL.md" in context
    assert context.count(".json") == 5
    second = mailbox.hook(tmp_path, payload)
    assert second["hookSpecificOutput"]["additionalContext"].count(".json") == 1
    assert mailbox.hook(tmp_path, payload) == {}
    assert len(mailbox.inbox(tmp_path, "claude")) == 6  # Delivery is not an ACK.
    assert mailbox.hook(tmp_path, {**payload, "session_id": str(uuid4())})


def test_hook_protocol_path_prefers_protocol_md_and_falls_back_to_renamed_doc(
    tmp_path: Path,
) -> None:
    mailbox.publish(tmp_path, message())
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "agent_handoff_protocol.md").write_text("# renamed\n", encoding="utf-8")
    payload = {"cwd": str(tmp_path), "session_id": str(uuid4()), "hook_event_name": "SessionStart"}
    fallback_ctx = mailbox.hook(tmp_path, payload)["hookSpecificOutput"]["additionalContext"]
    assert "docs/agent_handoff_protocol.md" in fallback_ctx

    (docs / "PROTOCOL.md").write_text("# standard\n", encoding="utf-8")
    standard_ctx = mailbox.hook(tmp_path, {**payload, "session_id": str(uuid4())})[
        "hookSpecificOutput"
    ]["additionalContext"]
    assert "docs/PROTOCOL.md" in standard_ctx


@pytest.mark.parametrize(
    "changes", [{"cwd": ".."}, {"hook_event_name": "Stop"}, {"session_id": ""}]
)
def test_hook_ignores_foreign_or_invalid_context(tmp_path: Path, changes: dict[str, str]) -> None:
    payload = {
        "cwd": str(tmp_path),
        "session_id": str(uuid4()),
        "hook_event_name": "SessionStart",
        **changes,
    }
    assert mailbox.hook(tmp_path, payload) == {}
    assert not (tmp_path / ".agent-bridge").exists()


def test_cli_publish_show_reply_and_inbox(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    item = message()
    source = tmp_path / "message.json"
    source.write_text(json.dumps(item), encoding="utf-8")
    prefix = ["--root", str(tmp_path)]
    assert mailbox.main([*prefix, "publish", "--file", str(source)]) == 0
    capsys.readouterr()
    assert mailbox.main([*prefix, "show", "--id", item["id"]]) == 0
    assert json.loads(capsys.readouterr().out) == item
    body = tmp_path / "reply.txt"
    body.write_text("수신 확인", encoding="utf-8")
    assert (
        mailbox.main(
            [
                *prefix,
                "reply",
                "--sender",
                "claude",
                "--reply-to",
                item["id"],
                "--kind",
                "ack",
                "--body-file",
                str(body),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert mailbox.main([*prefix, "inbox", "--recipient", "claude"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_cli_hook_fail_open_and_normal_errors_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("{bad SECRET PAYLOAD"))
    assert mailbox.main(["--root", str(tmp_path), "hook"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and "SECRET" not in captured.err
    assert mailbox.main(["--root", str(tmp_path), "show", "--id", str(uuid4())]) == 2
    assert capsys.readouterr().err


def test_target_session_routing_and_reply_identity(tmp_path: Path) -> None:
    sender_session, recipient_session, other_session = (str(uuid4()) for _ in range(3))
    item = message(sender_session=sender_session, recipient_session=recipient_session)
    mailbox.publish(tmp_path, item)
    assert mailbox.inbox(tmp_path, "claude", session_id=other_session) == []
    assert mailbox.inbox(tmp_path, "claude", session_id=recipient_session) == [item]
    hook_input = {
        "cwd": str(tmp_path),
        "session_id": other_session,
        "hook_event_name": "SessionStart",
    }
    assert mailbox.hook(tmp_path, hook_input) == {}
    for session in (None, other_session):
        with pytest.raises(ValueError):
            mailbox.reply(tmp_path, "claude", item["id"], "ack", "Received", session_id=session)
    reply = mailbox.reply(
        tmp_path, "claude", item["id"], "ack", "Received", session_id=recipient_session
    )
    assert reply["sender_session"] == recipient_session
    assert reply["recipient_session"] == sender_session


def test_duplicate_json_fields_and_windows_alias_paths_rejected(tmp_path: Path) -> None:
    source = tmp_path / "duplicate.json"
    source.write_text('{"body":"a","body":"b"}', encoding="utf-8")
    with pytest.raises(ValueError):
        mailbox.read_json(source)
    for path in (".git./config", "docs /x", "docs/name."):
        with pytest.raises(ValueError):
            mailbox.publish(tmp_path, message(scope={"mode": "read_only", "paths": [path]}))


def test_hook_cli_outputs_only_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mailbox.publish(tmp_path, message(body="PRIVATE message contents"))
    payload = {
        "cwd": str(tmp_path),
        "session_id": str(uuid4()),
        "hook_event_name": "UserPromptSubmit",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert mailbox.main(["--root", str(tmp_path), "hook"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "PRIVATE" not in output


def test_tampered_reply_does_not_silently_suppress_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    item = message()
    mailbox.publish(tmp_path, item)
    response = mailbox.reply(tmp_path, "claude", item["id"], "ack", "Read")
    path = tmp_path / ".agent-bridge/mailboxes/codex" / f"{response['id']}.json"
    path.write_text(json.dumps({**response, "task_id": "other-task"}), encoding="utf-8")
    assert mailbox.inbox(tmp_path, "claude") == [item]
    err = capsys.readouterr().err
    assert response["id"] in err and "reply changes thread, task, base, or scope" in err


def test_invalid_files_are_reported_to_stderr_without_blocking_inbox_watch_or_hook(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    valid = message()
    mailbox.publish(tmp_path, valid)
    claude_box = tmp_path / ".agent-bridge/mailboxes/claude"
    (claude_box / f"{uuid4()}.json").write_text("{not valid json", encoding="utf-8")
    bad_schema_id = str(uuid4())
    (claude_box / f"{bad_schema_id}.json").write_text(
        json.dumps({**message(id=bad_schema_id), "version": 99}), encoding="utf-8"
    )
    misfiled = message(sender="claude", recipient="codex")
    (claude_box / f"{misfiled['id']}.json").write_text(json.dumps(misfiled), encoding="utf-8")

    assert mailbox.inbox(tmp_path, "claude") == [valid]
    err = capsys.readouterr().err
    assert "invalid JSON" in err
    assert "unsupported version" in err
    assert "misfiled message" in err

    assert mailbox.main(["--root", str(tmp_path), "inbox", "--recipient", "claude"]) == 0
    cli = capsys.readouterr()
    assert json.loads(cli.out) == [valid]
    assert "invalid message" in cli.err

    watched = mailbox.watch(tmp_path, "claude", timeout=1.0, now=lambda: 0.0)
    assert watched is not None and watched["id"] == valid["id"]
    capsys.readouterr()

    payload = {"cwd": str(tmp_path), "session_id": str(uuid4()), "hook_event_name": "SessionStart"}
    hook_out = mailbox.hook(tmp_path, payload)
    assert valid["id"] in hook_out["hookSpecificOutput"]["additionalContext"]


def test_reply_with_missing_or_invalid_parent_fails_with_explicit_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_id = str(uuid4())
    with pytest.raises(ValueError, match=f"reply parent {missing_id} is missing"):
        mailbox.reply(tmp_path, "claude", missing_id, "ack", "Read")
    orphan_reply = message(
        kind="ack", sender="claude", recipient="codex", reply_to=missing_id, expires_at=None
    )
    with pytest.raises(ValueError, match=f"reply parent {missing_id} is missing"):
        mailbox.publish(tmp_path, orphan_reply)

    body_file = tmp_path / "body.txt"
    body_file.write_text("ack body", encoding="utf-8")
    assert (
        mailbox.main(
            [
                "--root",
                str(tmp_path),
                "reply",
                "--sender",
                "claude",
                "--reply-to",
                missing_id,
                "--kind",
                "ack",
                "--body-file",
                str(body_file),
            ]
        )
        == 2
    )
    assert f"reply parent {missing_id} is missing" in capsys.readouterr().err

    bad_parent_id = str(uuid4())
    claude_box = tmp_path / ".agent-bridge/mailboxes/claude"
    claude_box.mkdir(parents=True, exist_ok=True)
    (claude_box / f"{bad_parent_id}.json").write_text("{corrupt", encoding="utf-8")
    with pytest.raises(ValueError, match=f"reply parent {bad_parent_id} is invalid: invalid JSON"):
        mailbox.reply(tmp_path, "claude", bad_parent_id, "ack", "Read")
    assert (
        mailbox.main(
            [
                "--root",
                str(tmp_path),
                "reply",
                "--sender",
                "claude",
                "--reply-to",
                bad_parent_id,
                "--kind",
                "ack",
                "--body-file",
                str(body_file),
            ]
        )
        == 2
    )
    assert f"reply parent {bad_parent_id} is invalid" in capsys.readouterr().err


def test_agy_and_local_parties_exchange_messages_with_claude_and_codex(tmp_path: Path) -> None:
    assert mailbox.AGENTS == ("claude", "codex", "agy", "local")
    agy_session = str(uuid4())
    claude_session = str(uuid4())
    to_agy = message(
        sender="claude",
        recipient="agy",
        sender_session=claude_session,
        recipient_session=agy_session,
    )
    mailbox.publish(tmp_path, to_agy)
    assert mailbox.inbox(tmp_path, "agy", session_id=agy_session) == [to_agy]
    ack = mailbox.reply(tmp_path, "agy", to_agy["id"], "ack", "Read by agy", session_id=agy_session)
    assert ack["recipient"] == "claude"
    result = mailbox.reply(
        tmp_path, "agy", to_agy["id"], "result", "Verified by agy", session_id=agy_session
    )
    assert mailbox.inbox(tmp_path, "claude", session_id=claude_session) == [result]

    to_local = message(sender="claude", recipient="local", sender_session=claude_session)
    mailbox.publish(tmp_path, to_local)
    assert mailbox.inbox(tmp_path, "local") == [to_local]
    local_result = mailbox.reply(tmp_path, "local", to_local["id"], "result", "Summary draft")
    assert local_result["recipient"] == "claude"
