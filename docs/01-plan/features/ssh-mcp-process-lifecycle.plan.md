# ssh-mcp-process-lifecycle Planning Document

> **Summary**: `ssh_agent.py`의 콜드 spawn stdio 모델로 인한 cloud0 프로세스 누적 위험 + `ssh_bg_run` 백그라운드 job의 좀비/고아 프로세스 방치를 해결한다.
>
> **Project**: ssh-mcp (`Todoc/fpga/ssh-mcp/`)
> **Author**: hoseung.lee
> **Date**: 2026-07-07
> **Status**: Draft
> **Found in**: xcelium-mcp의 `xcelium-mcp-server-process-lifecycle` PDCA 사이클(Plan→Design) 진행 중, 사용자가 "SSH는 ssh-mcp를 쓰는데 xcelium-mcp의 클라이언트 launch 설정을 건드리는 게 의미가 있냐"고 질문한 것을 계기로 `~/.claude.json`을 직접 확인 — `xcelium-mcp`와 `ssh`(ssh-mcp) 항목이 서로 독립된 별개의 `ssh` 서브프로세스임을 확인하고, ssh-mcp 쪽 코드(`ssh_agent.py`)를 검토하다 발견

---

## Executive Summary

| Perspective | Content |
|-------------|---------|
| **Problem** | `ssh_agent.py`는 xcelium-mcp가 겪었던 것과 동일하게 `mcp.server.stdio.stdio_server()` 1 connection = 1 cold process 모델로 동작해, `~/.claude.json`의 `ssh cloud0 python3 ssh_agent.py` launch가 세션 재시작마다 새 프로세스를 띄우고 정리 로직이 없다. 추가로 `ssh_bg_run`으로 띄운 백그라운드 자식 프로세스는 `ssh_bg_poll`을 다시 호출해 `.poll()`이 실행되지 않으면 좀비로 남고, 부모(`ssh_agent.py`) 프로세스 자체가 죽으면 그 자식들은 고아가 되어 아무도 정리하지 않는다. |
| **Solution** | xcelium-mcp에서 검증한 조사·설계 방법론(실측 기반 아키텍처 선택, `/proc` 순수 관찰 등)을 재사용하되, ssh-mcp는 하드웨어 연결 등 무거운 상태가 없어 xcelium-mcp의 안C+(프리포크 수퍼바이저)를 그대로 이식하기보다 더 가벼운 대안(자체 idle self-exit, 외부 cron reaper 등)을 Design 단계에서 비교해 선택한다. 별도로 `ssh_bg_run`의 자식 프로세스 reap을 명시적으로 보장한다. |
| **Function/UX Effect** | 기존 7개 tool(`ssh_run`/`ssh_bg_run`/`ssh_bg_poll`/`file_read`/`file_write`/`file_ls`/`file_grep`) 시그니처·동작은 변경 없음. 신규 `ssh_bg_kill` tool 1개 추가로 백그라운드 job을 명시적으로 취소 가능(SSH 재연결로 부모가 바뀐 뒤에도 취소 가능). cloud0에 상주하는 ssh-mcp 관련 프로세스 수가 무한 누적되지 않고, 백그라운드 job을 폴링하지 않고 방치해도 좀비/고아가 남지 않음. |
| **Core Value** | ssh-mcp는 `~/.claude.json`의 **사용자 전역 설정**이라 fpga 하위 모든 프로젝트(xcelium-mcp뿐 아니라 venezia-fpga, alamo 계열 등)의 Claude Code 세션이 공유한다 — 한 곳에서 고치면 전 프로젝트가 혜택을 보지만, 반대로 방치하면 모든 프로젝트의 사용 빈도가 누적되어 xcelium-mcp보다 더 빨리 문제가 커질 수 있다. |

---

## Context Anchor

| Key | Value |
|-----|-------|
| **WHY** | `ssh_agent.py`가 xcelium-mcp와 동일한 콜드 spawn·무정리 구조이면서, 사용자 전역 설정이라 사용 빈도가 더 높고, `ssh_bg_run`은 임의 명령을 배경 실행하므로 방치 시 위험이 더 큼(단순 서버 프로세스가 아니라 사용자가 지정한 임의 워크로드) |
| **WHO** | ssh-mcp를 사용하는 hoseung.lee 계정의 모든 Claude Code 세션(프로젝트 무관, 전역 설정) |
| **RISK** | 프로세스/좀비 누적으로 cloud0 리소스(메모리·PID) 고갈. 특히 `ssh_bg_run` 좀비는 사용자가 시작한 임의 백그라운드 작업이 방치될 수 있어 단순 리소스 낭비를 넘어 "무슨 작업이 아직 돌고 있는지 아무도 모르는" 상태로 이어질 수 있음 |
| **SUCCESS** | (1) 동일 클라이언트로 반복 연결/해제해도 cloud0 상주 ssh-mcp 프로세스 수가 상한 내로 유지, (2) `ssh_bg_run`으로 시작한 job이 `ssh_bg_poll` 없이 방치되거나 부모가 죽어도 좀비/고아로 남지 않음 |
| **SCOPE** | F-A'(본 프로세스 lifecycle, 아키텍처는 Design에서 3안 비교 후 결정 — 안C+ 그대로 재사용 아님) + F-B'(`ssh_bg_run` 백그라운드 job 명시적 reap) |

---

## 1. Overview

### 1.1 Purpose

`ssh_agent.py`(cloud0에서 `ssh` stdio transport로 상주하는 원격 셸/파일 접근 MCP 서버)의 프로세스 lifecycle을 xcelium-mcp와 동일한 수준으로 안전하게 만든다 — 무한 누적 방지 + 좀비/고아 프로세스 방지.

### 1.2 Background

xcelium-mcp의 `server-process-lifecycle` PDCA 사이클(Plan→Design, 2026-07-06~07)에서 "stdio 1:1 콜드 spawn 모델이 프로세스 무한 누적을 유발한다"는 구조적 문제를 발견하고 안C+(프리포크 수퍼바이저)로 해결 설계를 진행했다. 그 논의 중 "클라이언트 ssh 옵션(`ServerAliveInterval`)을 추가하는 게 의미가 있냐, 우리는 ssh-mcp를 쓰지 않냐"는 질문이 나와 실제 `~/.claude.json`을 확인한 결과:

```json
"xcelium-mcp": {"command": "ssh", "args": [..., "cloud0", "/opt/mcp-env/bin/xcelium-mcp"]}
"ssh":         {"command": "ssh", "args": [..., "cloud0", "/opt/mcp-env/bin/python3", "/opt/ssh-mcp/ssh_agent.py"]}
```

두 항목은 서로 완전히 독립된 `ssh` 서브프로세스이며(하나가 다른 하나를 프록시하지 않음), `ssh-mcp`(`ssh_agent.py`, `Todoc/fpga/ssh-mcp/`)를 열어보니 xcelium-mcp의 개선 전 구조와 동일한 문제를 그대로 갖고 있었다 — 이 Plan은 그 후속 조치다.

**참고 — cloud0 실측 스냅샷(2026-07-07)**: 이 시점에는 `ssh_agent.py` 프로세스 1개(pid 7299)만 떠 있었고 좀비/방치 job 파일은 0개였다. 이는 "아직 문제가 안 터졌다"는 뜻이지 구조적으로 안전하다는 뜻은 아니다 — xcelium-mcp도 하루 3회 재연결 후에야 3쌍 누적이 관찰됐다.

### 1.3 Related Documents

- [xcelium-mcp-server-process-lifecycle.plan.md](../../../../xcelium-mcp/docs/01-plan/features/xcelium-mcp-server-process-lifecycle.plan.md) — 동일 근본 문제(stdio 1:1 콜드 spawn)의 원조 사례. 실측 방법론(SSH 직접 접속해 systemd/cron/권한 확인)을 그대로 재사용할 것.
- [xcelium-mcp-server-process-lifecycle.design.md](../../../../xcelium-mcp/docs/02-design/features/xcelium-mcp-server-process-lifecycle.design.md) — 안C+ 아키텍처, `/proc` 기반 순수 관찰 idle-culler 설계. ssh-mcp Design 단계에서 "재사용 가능한 부분"과 "ssh-mcp엔 과한 부분"을 가려낼 때 참고.

---

## 2. Scope

### 2.1 In Scope

- [ ] F-A': `ssh_agent.py` 본 프로세스의 lifecycle 개선 — 무한 누적 방지(구체 아키텍처는 Design에서 결정)
- [ ] F-B': `ssh_bg_run`으로 시작된 백그라운드 자식 프로세스의 명시적 reap 보장 — (a) 부모가 정상 종료할 때 살아있는 job을 정리(SIGTERM 등), (b) `ssh_bg_poll` 없이 job이 끝난 경우도 좀비로 남지 않도록(주기적 자체 reap 또는 `SIGCHLD` 핸들러)
- [ ] `/tmp/mcp_job_*.txt` 출력 파일의 정리 정책(무기한 누적 방지) — F-B'의 부수 항목
- [ ] F-C' (신규, 2026-07-07 추가): `ssh_bg_run`으로 시작한 job을 `job_id`로 명시적으로 취소하는 `ssh_bg_kill` tool 추가 — SSH 재연결로 부모 프로세스가 바뀌어도(F-A'/F-B' 설계상 PID를 사이드카 파일에 영속화) 취소 가능해야 함

### 2.2 Out of Scope

- 기존 7개 tool(`ssh_run`/`ssh_bg_run`/`ssh_bg_poll`/`file_read`/`file_write`/`file_ls`/`file_grep`)의 **기존 시그니처** 변경 — 순수 lifecycle/리소스 정리 범위 유지(단, F-C'로 `ssh_bg_kill` 1개 tool **추가**는 In Scope로 전환, 2026-07-07)
- xcelium-mcp 자체의 안C+ 구현 — 별개 PDCA 사이클(진행 중)에서 다룸
- sshd 쪽 서버 설정(`ClientAliveInterval` 등, root 필요) — 운영 가이드로만 다룰 사안이면 별도 문서

---

## 3. Requirements

### 3.1 Functional Requirements

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| FR-01 | `ssh_agent.py` 프로세스가 동일 클라이언트의 반복 연결/해제에도 cloud0에 무한 누적되지 않는다 | High | Pending |
| FR-02 | `ssh_bg_run`으로 시작한 백그라운드 프로세스는 `ssh_bg_poll` 호출 여부와 무관하게 최종적으로 reap된다(좀비로 남지 않는다) | High | Pending |
| FR-03 | `ssh_agent.py` 프로세스가 종료될 때, 그 프로세스가 시작한(아직 완료되지 않은) 백그라운드 job은 고아로 방치되지 않고 명시적으로 처리된다(종료 또는 최소한 추적 가능하게 로그) | Medium | Pending |
| FR-04 | `/tmp/mcp_job_*.txt` 출력 파일이 무기한 누적되지 않는다 | Low | Pending |
| FR-05 | 신규 `ssh_bg_kill(job_id)` tool로 실행 중인 백그라운드 job을 명시적으로 취소할 수 있다 — SIGTERM 전송 후 5초 대기, 미종료 시 SIGKILL 자동 승격(atexit_handler와 동일 패턴). 부모 프로세스가 재시작(SSH 재연결로 인한 콜드 spawn 포함)되어 `_jobs`가 비어있어도, PID를 사이드카 파일(`/tmp/mcp_job_{job_id}.pid`)에서 조회해 취소 가능해야 한다 | High | Pending |

### 3.2 Non-Functional Requirements

| Category | Criteria | Measurement Method |
|----------|----------|-------------------|
| 회귀 없음 | 7개 tool의 기존 동작·응답 포맷이 그대로 유지된다 | 수동 스모크 테스트(현재 자동 테스트 스위트 없음 — §5 리스크 참조) |
| 권한 | root/sudo 없이 배포 가능해야 한다(xcelium-mcp Plan §1.3에서 확인된 cloud0 제약과 동일 환경) | cloud0 SSH로 직접 검증 |
| 이식성 | 여러 프로젝트가 공유하는 전역 설정이므로, 특정 프로젝트(xcelium-mcp 등)에 종속적인 가정을 두지 않는다 | 코드 리뷰 |

---

## 4. Success Criteria

### 4.1 Definition of Done

- [ ] FR-01~FR-04 구현 완료
- [ ] cloud0에서 반복 연결/해제 스모크 테스트로 프로세스 수 상한 확인
- [ ] `ssh_bg_run` → 의도적으로 `ssh_bg_poll` 미호출 → 프로세스가 결국 정리됨을 확인
- [ ] Design 문서 작성 및 아키텍처(안 A/B/C) 비교·선택 완료

### 4.2 Quality Criteria

- [ ] 기존 7개 tool 수동 스모크 전부 통과(자동 테스트 없으므로 최소 스모크 체크리스트 작성)
- [ ] 새로 추가되는 lifecycle 코드에 대한 최소 단위 테스트 추가(현재 프로젝트에 테스트 인프라가 전혀 없음 — 이번 기회에 최소 골격 마련 권장)

---

## 5. Risks and Mitigation

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| 이 저장소에 테스트 인프라가 전혀 없어(현재 233줄 단일 파일, 테스트 0개) 회귀를 자동으로 잡을 수 없음 | Medium | High | Do 단계에서 최소 pytest 골격(7개 tool 각각의 happy-path 스모크) 먼저 추가 후 lifecycle 변경 진행 |
| 전역 설정이라 다른 프로젝트(venezia-fpga 등)의 세션에도 영향을 미침 — 배포 타이밍에 따라 다른 작업 세션이 재연결을 겪을 수 있음 | Medium | Medium | 배포는 사용자가 활성 세션이 적은 시점에 수동으로 진행(자동 배포 금지, 사용자 승인 후 진행 — memory: `feedback_confirm_before_action`) |
| `ssh_bg_run`이 이미 시작해둔 장기 실행 job이 lifecycle 변경 도중 예기치 않게 종료될 위험 | Low | Low | 배포 전 `ssh_bg_poll`로 진행 중 job 유무 확인, 없을 때 배포 |

---

## 6. Impact Analysis

### 6.1 Changed Resources

| Resource | Type | Change Description |
|----------|------|--------------------|
| `ssh_agent.py` 메인 프로세스 lifecycle | Process | 콜드 spawn 후 무정리 → 상한 관리(구체안은 Design) |
| `ssh_bg_run`/`_jobs` 딕셔너리 | In-memory state + subprocess | 부모 종료/미폴링 시 자식 미reap → 명시적 reap 경로 추가 |

### 6.2 Current Consumers

| Resource | Operation | Code Path | Impact |
|----------|-----------|-----------|--------|
| `ssh` MCP 서버(전체) | 호출 | `~/.claude.json`(사용자 전역) → fpga 하위 모든 프로젝트의 Claude Code 세션 | Needs verification — 이 저장소를 쓰는 모든 프로젝트 세션이 영향받음(venezia-fpga, alamo 계열 등 확인 필요) |
| `ssh_run`/`file_*` | 호출 | 즉시 반환형 tool, 상태 없음 | None — lifecycle 변경과 무관 |
| `ssh_bg_run`/`ssh_bg_poll` | 호출 | 백그라운드 job 추적 | Needs verification — reap 로직 추가 시 기존 "poll 늦게 해도 결과는 파일에 남아있다"는 현재 동작(outfile 기반)은 유지해야 함 |

### 6.3 Verification

- [ ] 위 컨슈머 목록에서 실제로 ssh-mcp를 사용 중인 다른 프로젝트가 있는지 확인(최소 grep으로 각 프로젝트의 세션 히스토리/메모리 확인)
- [ ] `ssh_bg_run`의 outfile 기반 결과 조회 방식은 reap 로직 추가 후에도 그대로 동작해야 함(reap이 outfile을 지우면 안 됨)

---

## 7. Architecture Considerations

> 이 프로젝트는 웹앱이 아니므로 템플릿의 Level/Framework 선택은 해당 없음(N/A). 대신 xcelium-mcp Design 문서(§2)의 방법론을 재사용해 Design 단계에서 3가지 구현안을 비교한다.

### 7.1 Design 단계에서 비교할 안 (사용자 확정, 2026-07-07)

| 안 | 설명 | 비고 |
|---|---|---|
| **A — 자체 idle self-exit** | `ssh_agent.py`가 N분간 tool call이 없으면 스스로 종료 | 새 프로세스/소켓 불필요, 가장 가벼움. 재연결 시 콜드 spawn은 여전히 발생하지만 xcelium-mcp보다 훨씬 가벼운 프로세스라 비용이 낮음 |
| **B — xcelium-mcp의 안C+ 재사용** | ForkingMixIn 수퍼바이저 + `/proc` 기반 idle-culler | 아키텍처 일관성은 좋으나 ssh-mcp의 가벼운 상태 대비 과한 복잡도일 수 있음(사용자가 "더 가벼운 대안 검토" 선택) |
| **C — cron 기반 외부 reaper** | `ssh_agent.py`는 무변경, cron이 `/proc`으로 idle/고아 프로세스를 주기적으로 정리 | xcelium-mcp Design §5.3(`idle_culler.py`)의 `/proc` 관찰 로직을 재사용 가능 |

**사용자 방향 확인(Checkpoint, 2026-07-07)**: 안C+ 그대로 재사용보다 **더 가벼운 대안(A 또는 C)을 우선 검토**하기로 함 — Design 단계에서 위 3안을 정식으로 비교·선택.

### 7.2 `ssh_bg_run` reap 방식 후보(Design에서 구체화)

- `SIGCHLD` 핸들러로 즉시 reap
- 또는 주기적 self-check(예: tool call마다 살아있는 `_jobs`의 `.poll()`을 한 번씩 훑기)
- 부모 종료 시 `atexit`으로 `_jobs`의 살아있는 프로세스에 `SIGTERM` 전파

---

## 8. Convention Prerequisites

### 8.1 Existing Project Conventions

- [x] 별도 CLAUDE.md/컨벤션 문서 없음 — 233줄 단일 파일, 최소 구조
- [ ] 테스트 인프라 없음(§5 리스크 항목) — Do 단계에서 최소 pytest 골격 추가 권장

### 8.2 Conventions to Define/Verify

이 프로젝트는 신규 컨벤션을 별도로 정의하지 않고, xcelium-mcp의 기존 스타일(비동기 함수, 명확한 tool 단위 분리)을 참고 수준으로만 따른다. 웹앱 전용 항목(환경변수 prefix, import order 등)은 해당 없음(N/A).

---

## 9. Next Steps

1. [ ] `/pdca design ssh-mcp-process-lifecycle` — 안 A/B/C 비교, Checkpoint로 최종 선택
2. [ ] Do 단계 진입 전 최소 pytest 골격 추가(7개 tool 스모크)
3. [ ] cloud0 실측(systemd/cron/권한) — xcelium-mcp Plan §1.3와 동일 방법론으로 재확인(같은 호스트이므로 대부분 재사용 가능하나, 검증은 다시 수행)

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-07-07 | 초안 — xcelium-mcp server-process-lifecycle PDCA 진행 중 발견한 ssh-mcp(`ssh_agent.py`)의 동일 유형 문제(콜드 spawn 누적) + 추가 문제(`ssh_bg_run` 좀비/고아)를 정리. 사용자 확인: 스코프는 둘 다 포함, 아키텍처는 안C+ 그대로 재사용 대신 더 가벼운 대안(A/C)을 Design에서 우선 검토. | hoseung.lee |
| 0.2 | 2026-07-07 | Design 단계(옵션 A 선택, idle self-exit + job reaper) 진행 중 사용자 요청으로 F-C'/FR-05(`ssh_bg_kill` tool 신규 추가) 반영. 계기: "SSH 재연결 시 부모가 바뀌면 이전 job을 job_id로 못 죽이는 것 아니냐"는 질문 → PID 사이드카 파일(`/tmp/mcp_job_{job_id}.pid`) 영속화를 전제로 한 취소 기능 필요성 확인. Kill 동작은 atexit_handler와 동일 패턴(SIGTERM → 5초 → SIGKILL 자동 승격)으로 확정. §2.2 Out of Scope에서 "tool 추가"는 제외(기존 tool 시그니처 불변 원칙은 유지). | hoseung.lee |
