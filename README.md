# ssh-mcp

`ssh_agent.py` — remote host(`cloud0`)에서 SSH stdio transport로 구동되는 Claude Code MCP 서버.
`ssh_run`/`ssh_bg_run`/`ssh_bg_poll`/`ssh_bg_kill`/`file_read`/`file_write`/`file_ls`/`file_grep`
8개 툴로 원격 셸 명령 실행과 파일 조작을 제공한다. 프로세스 lifecycle(유휴 자동 종료, job reap,
그룹 kill)은 `docs/02-design/features/ssh-mcp-process-lifecycle.design.md` 참고.

## Claude Code Skill

`skill/ssh-mcp/`에 이 MCP 서버의 8개 툴을 언제·어떻게 쓸지 안내하는 Claude Code skill 소스가
있다 (ssh_run vs ssh_bg_run+poll 선택 기준, job이 세션/서버 재시작을 넘어 살아남는 이유, file_*
우선 사용 등). RTL/FPGA 전용이 아니라 이 SSH MCP 서버를 쓰는 모든 세션에 적용되므로, 프로젝트
로컬이 아니라 **user-level**(`~/.claude/skills/`)에 설치해야 다른 프로젝트에서도 트리거된다.

### 설치 / 갱신

`skill/ssh-mcp/`를 수정한 뒤에는 아래 명령으로 다시 설치한다:

```bash
rm -rf ~/.claude/skills/ssh-mcp && cp -r skill/ssh-mcp ~/.claude/skills/ssh-mcp
```

별도 스크립트 없이 그냥 복사만 하면 된다 — 소스는 이 저장소의 `skill/ssh-mcp/`가 원본(source of
truth)이고, `~/.claude/skills/ssh-mcp/`는 그걸 반영한 설치본이다.
