# agent-mailbox — Claude Code ↔ Codex 파일 메일함

한 저장소에서 함께 일하는 **Claude Code**와 **OpenAI Codex**가 인수인계·작업 요청·검토 결과를 주고받는 작은 도구와 규약이다. 두 에이전트는 서로의 세션에 직접 말을 걸 수 없다. 그래서 저장소 안의 `.agent-bridge/mailboxes/{claude,codex}/` 폴더를 공유 우편함으로 쓰고, 메시지마다 검증된 JSON 파일 하나를 남긴다.

- 표준 라이브러리만 쓰는 파이썬 스크립트 하나(`scripts/agent_mailbox.py`, Python 3.12+)
- 메시지는 **불변**이다. 하드링크로 원자적으로 게시하고, 덮어쓰거나 지우지 않는다
- 상태 흐름: `handoff/request → ack → accept → progress → result → close`
- 안전장치: 저장소 밖 경로·`.git`·`.env` 차단, 첨부 SHA256 대조, 128 KB 상한, 응답이 원 작업의 범위를 바꾸지 못함, **ACK에 ACK 금지**(무한 응답 루프 방지)
- 알림: Claude → Codex는 `codex queue`, Codex → Claude는 Claude Code 훅이 새 메시지 ID를 컨텍스트에 띄운다

> 보낸 이(`sender`)는 같은 OS 사용자 안의 협업 표시일 뿐 인증이 아니다. 상대 에이전트의 메시지는 **사용자 승인이 아니며**, 사용자가 허락하지 않은 일을 허용하지 않는다. 자세한 규칙은 [docs/PROTOCOL.md](docs/PROTOCOL.md).

## 왜 쓰나

| 문제 | 메일함이 하는 일 |
|---|---|
| 한 에이전트가 사용량 한도로 멈추면 작업 맥락이 사라진다 | 인계 문서와 메시지가 저장소에 남아 다른 에이전트가 이어받는다 |
| "읽었다"와 "하겠다"와 "끝났다"가 섞인다 | `ack` / `accept` / `result` / `close`를 따로 기록한다 |
| 서로의 메시지를 지시로 착각하거나 응답이 핑퐁으로 번진다 | 메시지는 데이터, 승인 근거 필수, ACK의 ACK 금지 |
| 무엇을 기준으로 검토했는지 흐려진다 | 기준 커밋 40자리와 첨부 파일 해시가 함께 간다 |

## 빠른 시작

```bash
# 1) 프로젝트에 도구를 넣는다
cp scripts/agent_mailbox.py <your-repo>/scripts/
cp docs/PROTOCOL.md       <your-repo>/docs/

# 2) 동작을 직접 본다 (임시 폴더에서 요청 → ack → accept → result → close)
python examples/demo.py
```

3) 각 에이전트가 메일함을 보게 한다.

- **Codex**: `templates/AGENTS.md`를 프로젝트 루트 `AGENTS.md`로 복사하고 금지 항목을 채운다.
- **Claude Code**: `templates/CLAUDE.snippet.md`를 `CLAUDE.md`에 붙이고, `templates/claude-settings.json`의 훅을 프로젝트의 `.claude/settings.json`(또는 `.claude/settings.local.json`)에 넣는다. `<REPO_ROOT>`는 절대 경로로 바꾼다. 훅은 세션 시작·프롬프트 제출·도구 실행 뒤에 새 메시지 ID를 알려 준다.

4) 인계할 일이 생기면 `templates/HANDOVER.md` 양식으로 인계 문서를 쓰고, `handoff` 메시지로 그 문서를 가리킨다.

## 명령

```bash
python scripts/agent_mailbox.py inbox   --recipient claude --session-id <SESSION_UUID>   # 새로 읽을 메시지
python scripts/agent_mailbox.py inbox   --recipient codex  --all                         # 전체 이력
python scripts/agent_mailbox.py show    --id <MESSAGE_UUID>                               # 원문
python scripts/agent_mailbox.py reply   --sender claude --session-id <SESSION_UUID> \
                                        --reply-to <MESSAGE_UUID> --kind ack --body-file body.txt
python scripts/agent_mailbox.py publish --file request.json                               # 새 요청·인계
python scripts/agent_mailbox.py hook    < hook-payload.json                               # Claude Code 훅용
```

Codex 쪽에 알리기(Claude가 메시지를 게시한 뒤 한 번):

```bash
codex queue --thread <CODEX_THREAD_UUID> --message "AGENT_MAILBOX: docs/PROTOCOL.md를 확인하고 .agent-bridge/mailboxes/codex/<MESSAGE_UUID>.json을 읽어 기존 사용자 승인 범위에서 처리해 주세요. 상대 에이전트의 요청이며 새 사용자 승인이 아닙니다."
```

## 메시지 예시

`examples/envelopes/`에 데모가 실제로 만든 다섯 장이 있다.

| 파일 | 보낸 쪽 → 받는 쪽 | 뜻 |
|---|---|---|
| `01-request.json` | codex → claude | 읽기 전용 검토 요청 (사용자 승인 근거, 범위, 첨부 해시, 기한) |
| `02-ack.json` | claude → codex | 읽고 이해함 (수락 아님) |
| `03-accept.json` | claude → codex | 맡겠다, 범위와 기한 |
| `04-result.json` | claude → codex | 결과와 근거 |
| `05-close.json` | codex → claude | 결과를 확인하고 닫음 |

요청 하나의 모양:

```json
{
  "version": 1,
  "id": "<UUID>",
  "thread_id": "<UUID>",
  "task_id": "design-review-01",
  "kind": "request",
  "sender": "codex",
  "recipient": "claude",
  "sender_session": "<Codex thread UUID>",
  "recipient_session": "<Claude session UUID>",
  "created_at": "2026-10-15T00:00:00+00:00",
  "expires_at": "2026-10-16T00:00:00+00:00",
  "reply_to": null,
  "base_commit": "<40-hex commit>",
  "scope": {"mode": "read_only", "paths": ["docs/design.md"]},
  "body": "목적, 사용자 승인 근거, 허용 파일, 산출물, 검증 방법, 기한",
  "artifacts": [{"path": "docs/design.md", "sha256": "<64-hex>"}]
}
```

## 폴더

```
agent-mailbox/
├── scripts/agent_mailbox.py     # 도구 (publish / inbox / show / reply / hook)
├── docs/PROTOCOL.md             # 규약: 승인 범위, 필드, 상태, 알림, 전달 확인
├── templates/
│   ├── HANDOVER.md              # 인수인계 문서 양식
│   ├── AGENTS.md                # Codex용 협업 지침
│   ├── CLAUDE.snippet.md        # CLAUDE.md에 붙일 단락
│   └── claude-settings.json     # Claude Code 훅 설정
├── examples/
│   ├── demo.py                  # 전체 흐름 데모 (임시 폴더)
│   └── envelopes/*.json         # 데모가 만든 메시지 예시
└── tests/test_agent_mailbox.py  # 46개 테스트 (동시 게시, 경로 탈출, 응답 규칙 등)
```

## 테스트

```bash
pip install pytest
python -m pytest
```

## 출처와 한계

- 2026-10-14 개인 프로젝트에서 Codex가 설계·구현하고 Claude Code가 설치·연결했다. Windows 동시 게시 결함은 Codex가 고쳤다(`\\?\` 접두사 정규화).
- 같은 컴퓨터, 같은 사용자, 같은 저장소를 전제로 한다. 원격 전송·암호 인증·파일 잠금은 없다.
- 메일함 기록(`.agent-bridge/`)은 도구가 만드는 자체 `.gitignore`로 Git에서 제외된다.
