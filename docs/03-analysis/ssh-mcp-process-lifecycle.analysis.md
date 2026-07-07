# ssh-mcp-process-lifecycle Analysis Report

> **Analysis Type**: Gap Analysis (Design vs Implementation)
>
> **Project**: ssh-mcp (`Todoc/fpga/ssh-mcp/`)
> **Version**: N/A
> **Analyst**: gap-detector (via hoseung.lee)
> **Date**: 2026-07-07
> **Design Doc**: [ssh-mcp-process-lifecycle.design.md](../02-design/features/ssh-mcp-process-lifecycle.design.md)

### Pipeline References

N/A — 이 프로젝트는 웹앱 파이프라인을 사용하지 않는다(Plan/Design과 동일).

---

## Context Anchor

| Key | Value |
|-----|-------|
| **WHY** | `ssh_agent.py`가 콜드 spawn·무정리 구조이면서 사용자 전역 설정이라 사용 빈도가 높고, `ssh_bg_run`은 임의 워크로드를 배경 실행하므로 방치 시 위험이 큼 |
| **WHO** | ssh-mcp를 쓰는 hoseung.lee 계정의 모든 Claude Code 세션(프로젝트 무관) |
| **RISK** | 프로세스/좀비 누적으로 cloud0 리소스 고갈, 방치된 백그라운드 작업 추적 불가 |
| **SUCCESS** | 반복 연결/해제해도 프로세스 상한 유지, bg job이 미폴링/부모사망에도 좀비·고아로 안 남음 |
| **SCOPE** | F-A' + F-B' + F-C'(`ssh_bg_kill`, FR-05) |

---

## Strategic Alignment Check

### PRD Alignment

N/A — 이 사이클은 `/pdca pm`을 거치지 않았다(PRD 문서 없음). Plan §Found in에 기록된 대로 xcelium-mcp PDCA 사이클 진행 중 발견한 후속 조치로 시작됨.

### Success Criteria Status

| # | Criteria (Plan §4) | Status | Evidence |
|---|---|:---:|---|
| SC-1 | FR-01 idle self-exit — 반복 연결/해제해도 무한 누적 안 됨 | ✅ (정적) / ⚠️ L3 미수행 | `ssh_agent.py:157-171` idle_watchdog; cloud0 실측은 Check 단계 수동 항목 |
| SC-2 | FR-02 `ssh_bg_run` job이 poll 여부와 무관하게 reap됨 | ✅ | `job_sweep:133` — 매 tick `.poll()` 호출 |
| SC-3 | FR-03 부모 종료 시 미완료 job 명시적 처리 | ✅ | `atexit_handler` + `_terminate_sync`(SIGTERM→5s→SIGKILL); SIGKILL 자체는 우회 불가(§6.1 알려진 한계, 잔존 리스크로 명시됨) |
| SC-4 | FR-04 `/tmp/mcp_job_*.txt` 무기한 누적 방지 | ✅ | `job_sweep:139-150` — outfile+pidfile 24h 후 정리 |
| SC-5 | FR-05 `ssh_bg_kill`(job_id) — SIGTERM→5s→SIGKILL, 부모 재시작 후에도 pidfile fallback으로 kill 가능 | ✅ (SIGKILL 강제승격 분기 테스트 없음 — Gap A) | `:387-421` |
| SC-6 | 기존 tool 시그니처 불변 | ✅ | Contract 축 100% — 아래 §2.6 |
| SC-7 | cloud0 반복 연결/해제 스모크로 프로세스 상한 확인 | ⏳ 미수행 | Check 단계 사용자 수동 항목(L3) |
| SC-8 | 의도적 미폴링 후 job 정리 확인 | ✅ (로컬) / ⏳ cloud0 미확인 | `test_job_sweep_cleans_up_after_retention` 등 |

**Success Rate**: 6/8 완전 충족, 2건은 L3(cloud0 실배포) 수동 검증 대기 — 코드 레벨 구현은 100% 완료.

### Decision Record Verification

| Source | Decision | Followed? | Deviation |
|--------|----------|:---:|-----------|
| [Plan] | 아키텍처는 안C+ 대신 더 가벼운 A/C 우선 검토 | ✅ | Design에서 옵션 A(idle self-exit) 최종 선택 |
| [Design] | 옵션 A(자체 idle self-exit) 채택 | ✅ | `idle_watchdog` + `_main_task.cancel()`로 구현, launch 설정 불변 |
| [Design] | Kill 정책: SIGTERM→5초→SIGKILL 자동 승격 | ✅ | `_kill_pid_async`, `_terminate_sync` 모두 동일 패턴 |
| [Design] | PID 사이드카 파일로 부모 재시작 후에도 kill/poll 가능 | ✅ | `_pid_alive`, `ssh_bg_kill`/`ssh_bg_poll` fallback 분기 |

---

## 1. Analysis Overview

### 1.1 Analysis Purpose

Design v0.3(§2/§3/§4.1/§6.1.2/§8/§11)과 실제 구현(`ssh_agent.py`, `tests/`)이 일치하는지 정적으로 대조하고, Design §8 Test Plan 대비 테스트 커버리지 갭을 확인한다.

### 1.2 Analysis Scope

- **Design Document**: `docs/02-design/features/ssh-mcp-process-lifecycle.design.md` (v0.3)
- **Implementation Path**: `ssh_agent.py` (488줄), `tests/conftest.py`, `tests/test_tools_smoke.py`, `tests/test_lifecycle.py`
- **Analysis Date**: 2026-07-07
- **Analysis Mode**: 정적 분석 + 로컬 pytest 26개 실행 결과(전부 PASS, Windows/MSYS2 환경). cloud0 실배포 Runtime 검증(L3)은 미포함(Check 단계 수동 항목).

---

## 2. Gap Analysis (Design vs Implementation)

> 웹앱 axis(API route/DB/UI)는 해당 없음 — 이 프로젝트에 맞게 Structural/Functional/Contract로 재해석.

### 2.1 Structural Match — 100%

| Design §3.2 요소 | 구현 위치 | 상태 |
|---|---|:---:|
| `IDLE_TIMEOUT_SEC`(env override, ValueError 폴백) | `ssh_agent.py:35-37` | ✅ |
| `WATCHDOG_INTERVAL_SEC=60` | `:39` | ✅ |
| `JOB_RETENTION_SEC=86400` | `:40` | ✅ |
| `JOB_OUTFILE_TMPL` / `JOB_PIDFILE_TMPL` | `:41-42` | ✅ |
| `_last_activity`(monotonic) | `:44` | ✅ |
| `_pid_alive` / `_terminate_sync` / `_kill_pid_async` | `:48`, `:69`, `:83` | ✅ |
| `job_sweep` / `idle_watchdog` / `atexit_handler` | `:124`, `:157`, `:174` | ✅ |
| `JobEntry`(process/outfile/pidfile/finished_at) | `:346-351` | ✅ |
| `list_tools()`에 8개 tool(기존 7 + `ssh_bg_kill`) | `:192-296` | ✅ |

설계에 없던 추가: `_main_task` 전역 + `.cancel()`(`:45`, `:170`) — Design §2.1의 "asyncio 루프 stop" 추상 서술을 구체화한 것으로, 정상 종료 경로를 통해 `atexit`이 확실히 실행되도록 함(설계 의도와 일치, 이탈 아님).

### 2.2 In-Memory State Model (Design §3, DB 대체)

| Design | 구현 | 상태 |
|---|---|:---:|
| `JobEntry.pidfile` 필드 | `:349` | ✅ |
| PID 사이드카 파일 기록(FR-05) | `ssh_bg_run` 내 `:342-345` | ✅ |

### 2.3 해당 없음 섹션 (Design 원본과 동일하게 N/A)

DB Schema/BaaS Collection, UI/UX, Clean Architecture 레이어 — Design v0.3과 마찬가지로 N/A.

### 2.4 Functional Depth — 98%

Placeholder/TODO/stub 없음. 설계된 동작을 모두 수행:

| Design 동작 | 구현 | 상태 |
|---|---|:---:|
| §1.2/§6.1.1 job 실행 중이면 idle-exit 보류 | `idle_watchdog:167` `not _has_running_job()` | ✅ |
| §2.1 job_sweep: zombie reap + 24h 정리(outfile+pidfile) | `:133`, `:139-150` | ✅ |
| §7/FR-03 atexit SIGTERM→5초→SIGKILL | `atexit_handler:182` → `_terminate_sync:73-80` | ✅ |
| §4.1 `ssh_bg_kill`: `_jobs`→pidfile fallback 순서 | `:389`, `:397-412` | ✅ |
| §4.1 SIGTERM→5초 폴링→SIGKILL 승격 | `_kill_pid_async:101-121` | ✅ |
| §6.1.2 `ssh_bg_poll` outfile fallback(마커+`_pid_alive`) | `:371-384` | ✅ |
| 마커 `MCP_DONE_/MCP_KILLED_{job_id}` | `:338`, `:416` | ✅ |
| Kill 응답 라벨(`SIGTERM, graceful` / `SIGKILL, forced after 5s timeout`) | `:420-421` | ✅ Design §4.1 예시와 정확히 일치 |

Windows 로컬 테스트를 위해 Design에 없던 이식성 보강: `_pid_alive`/`_kill_pid_async`가 POSIX `ProcessLookupError`와 Windows의 순수 `OSError`(WinError 87)를 모두 처리(`:63-66`, `:96-99`) — cloud0(Linux) 동작에는 영향 없는 안전한 확장.

**−2% 사유**: Gap C(아래) — Design §4.1 step 4 문구가 자체 예시와 불일치. 코드는 예시·테스트와 일치하므로 기능적 결함은 아니고 문서 정합성 이슈.

### 2.5 Contract — 100%

- `ssh_bg_kill` inputSchema(`{job_id: string, required}`)가 §4.1과 정확히 일치(`:242-248`)
- 기존 7개 tool 시그니처 불변(Plan §2.2 준수) — `call_tool` 진입부에 `_last_activity` 갱신만 추가(`:303-304`), 시그니처/응답 변경 없음
- `ssh_bg_poll`/`ssh_bg_run` 정상 경로 응답 포맷이 레거시와 byte-identical, 신규 동작은 전부 additive(fallback 분기·pidfile 기록만 추가)

### 2.6 Runtime Verification — Design §8 vs tests/

#### L1 (§8.2, 10개 시나리오) — 9/10 커버

| # | 시나리오 | 테스트 | 상태 |
|---|---|---|:---:|
| 1 | ssh_run 성공 | `test_ssh_run_success` | ✅ |
| 2 | 존재하지 않는 명령 → `[stderr]`+`[exit N]` | `test_ssh_run_nonzero_exit`(`exit 3`) | ⚠️ `[exit N]`만 검증, `[stderr]` 분기 미검증 |
| 3 | timeout | `test_ssh_run_timeout` | ✅ |
| 4 | bg_run→bg_poll | `test_ssh_bg_run_and_poll` | ✅ |
| 5 | file 쓰기/읽기 라운드트립 | `test_file_read_write_roundtrip` | ✅ |
| 6 | file_ls/grep | `test_file_ls`, `test_file_grep` | ✅ |
| 7 | bg_run→kill(정상 실행 중) | `test_ssh_bg_kill_running_job` | ✅ |
| 8 | kill 이미 종료된 job | `test_ssh_bg_kill_already_finished` | ✅ |
| 9 | kill 미상 job_id | `test_ssh_bg_kill_unknown_job_id` | ✅ |
| 10 | SIGTERM 무시 → SIGKILL 강제 승격 | `test_kill_pid_async_escalates_to_sigkill_when_unresponsive`(mock 기반) | ✅ (제어 흐름만 — 실제 trap 기반은 L3 #5로 이관) |

**L1 Score**: 10/10 = 100% (제어 흐름 기준; #10의 실제 OS trap 검증은 L3로 별도)

#### L2 (§8.3, 5개 시나리오) — 5/5 커버 + 8개 추가

전부 커버, 설계 계획보다 초과 커버(job_sweep 실행중 유지/최근종료 보존, atexit 종료-job 스킵, `_pid_alive` 3종, FR-05 재연결 fallback 시나리오, `_kill_pid_async` graceful/forced 제어흐름 2종).

**L2 Score**: 5/5 = 100% (+8 bonus)

#### L3 (§8.4, 수동) — 완료, 5/5 PASS (2026-07-07, cloud0 실측)

- [x] cloud0 반복 연결/해제 → `ps` 프로세스 수 상한 확인 — PASS (테스트 스폰 전/후 프로세스 수 동일, 누수 없음)
- [x] bg job 미폴링 방치 → 좀비(`<defunct>`) 미잔존 확인 — PASS (`job_sweep()` 호출 후 defunct 0건)
- [x] `SSH_MCP_IDLE_TIMEOUT_SEC` → idle+tick 내 self-exit 확인 — PASS (exit code 0) — **최초 시도는 FAIL, Gap F 발견·수정 후 재검증 PASS**
- [x] **(Gap E)** 프로세스 그룹 kill — `ssh_bg_run`(`sleep 60`) → `ssh_bg_kill` → `ps -ef | grep sleep` — PASS (`sleep_alive_before: true` → `sleep_alive_after: false`)
- [x] **(Gap A 잔여)** `ssh_bg_run`(`trap '' TERM; sleep 60`) → `ssh_bg_kill` → ~5초 후 `SIGKILL, forced` — PASS (elapsed 5.02s, `sleep_alive_after: false`)

**L3 Score**: 5/5 = 100%

### 2.6.1 L3 실측 중 발견한 신규 Gap (Gap F)

정적 분석·mock 유닛 테스트로는 포착 불가능한 두 번째 실측 버그. `idle_watchdog`이 `_main_task.cancel()`로 종료를 시도했으나, cloud0 실측 결과 `[ssh-mcp] idle timeout (10s) — exiting` 로그는 정확히 예정대로 찍히는데 **프로세스는 3분 넘게 계속 살아있었다**(`mcp.server.stdio` 내부의 cancellation 전파 실패로 추정). `os._exit(0)` 직접 호출로 수정(안전 근거: 이 경로는 `not _has_running_job()`이 이미 보장되므로 `atexit` 우회가 무해함) — Design §2.2.1 신규 반영, 재검증 PASS. Gap E와 함께, "Do 단계 정적 검증을 통과해도 Check 단계 실제 배포 검증에서만 드러나는 버그가 있다"는 걸 보여준 사례.

### 2.7 Match Rate Summary

```
┌─────────────────────────────────────────────┐
│  Structural Match Rate:   100%               │
│  Functional Match Rate:   100% (Gap E/F 수정 후)│
│  Contract Match Rate:     100%               │
│  L1 Runtime (proxy):      100%               │
│  L2 Runtime (proxy):      100%               │
│  L3 Runtime (cloud0 실측):100% (5/5 PASS)     │
│  ─────────────────────────────────────────── │
│  Overall Match Rate:     100%                │
├─────────────────────────────────────────────┤
│  ✅ Match:            전 항목 (A/B/C/D/E/F Fixed)│
│  ❌ Not implemented:   0건                    │
└─────────────────────────────────────────────┘
```

> Gap 발견 당시(gap-detector 정적 분석) 스냅샷은 Overall ~99%였다. 이후 실측(Gap E: process-group kill 버그, Gap F: idle_watchdog 미종료 버그)까지 포함해 전부 수정하고 cloud0 L3 스모크(5/5 PASS)까지 완료한 최종 상태가 위 표다.

---

## Gaps — 처리 결과 (2026-07-07, Checkpoint 5 "지금 모두 수정")

| # | 심각도 | 내용 | 근거 | 처리 |
|---|:---:|---|---|---|
| A | Important | Design §8.2 #10(SIGKILL 강제 승격) 테스트 없음 | `ssh_agent.py:112-121`(구 라인) | ✅ **Fixed** — `test_kill_pid_async_escalates_to_sigkill_when_unresponsive` 추가. 단, 실제 bash `trap '' TERM` 기반 end-to-end 검증은 Windows/MSYS2에서 불가능함을 실측 확인(Windows `os.kill(pid,15)`가 `TerminateProcess`로 trap을 우회) — 대신 `_kill_pid_async`의 에스컬레이션 제어 흐름을 `_kill_group` mock으로 검증. 실제 trap 기반 검증은 Design §8.4 L3 #5로 이관 |
| B | Minor | `[stderr]` 분기 미검증 | `test_tools_smoke.py:33-35`(구) | ✅ **Fixed** — `test_ssh_run_nonexistent_command_hits_stderr_branch` 추가 |
| C | Minor(문서) | Design §4.1 step 4 문구 불일치 | Design §4.1 | ✅ **Fixed** — `[no-op] job_id: {id} already finished`로 정정 |
| D | Minor(문서) | "6개 tool" 표기 오류 | Plan §2.2, Design §4 | ✅ **Fixed** — Plan/Design 전체 6곳을 "7개"로 정정 |

**테스트 스위트**: 26개 → **29개, 전부 PASS**(`C:\Python314\python.exe -m pytest tests/ -v`).

### 추가 발견 (Gap E, 정적 분석 범위 밖 — 실제 프로세스 트리 실측으로 발견)

Gap 수정 작업 중 `ssh_bg_kill`/`atexit_handler`가 실제로 자식 프로세스까지 종료시키는지 직접 프로세스 트리를 띄워 검증하다가, **정적 코드 리뷰로는 발견 불가능한 심각한 버그**를 확인함:

| # | 심각도 | 내용 | 근거 |
|---|:---:|---|---|
| E | **Critical** (실측 발견) | `ssh_bg_run`의 `Popen`이 만드는 것은 `bash -c "...; 실제명령; ..."` **래퍼** 프로세스이고, `_jobs`에 기록되는 pid는 이 래퍼의 pid다. `ssh_bg_kill`/`atexit_handler`가 이 래퍼 pid에만 `SIGTERM`/`SIGKILL`을 보내면 **래퍼만 죽고 실제 명령(예: 장기 시뮬레이션)은 고아 프로세스로 계속 실행**된다 — FR-03/FR-05 둘 다의 목적을 무력화 | 실측: `sleep 30`을 감싼 bash 래퍼에 SIGTERM 전송 후 `ps -ef`로 `sleep` 자식이 여전히 살아있음을 직접 확인 |

**처리**: ✅ **Fixed, cloud0 실측 재검증 PASS** — `ssh_bg_run`에 `start_new_session=True` 추가로 job마다 독립 프로세스 그룹 생성, `_kill_group()` 헬퍼(`os.killpg` 사용, POSIX 전용)로 `_terminate_sync`/`_kill_pid_async` 통일. Design §4.1.1(신규)에 상세 기록. 로컬 Windows에서는 검증 불가(MSYS2 PID 네임스페이스 불일치)했으나 cloud0에서 실제로 `sleep 60` 자식까지 종료되는 것을 확인함(§2.6 L3 결과).

이 발견은 gap-detector의 정적 분석(Overall ~99%, Critical 0건)이 **놓친** 이슈였다 — 코드만 읽어서는 `_kill_group`이 무엇을 죽이는지(래퍼 vs 실제 명령)가 실행 시점 프로세스 트리 구조에 의존하기 때문에 드러나지 않는다. Do/Check 단계에서 실제로 프로세스를 띄워보는 것의 가치를 보여주는 사례로 기록.

### 추가 발견 (Gap F, L3 cloud0 스모크 중 발견)

Gap E 수정 검증을 위해 실제 cloud0에 idle timeout 테스트(`SSH_MCP_IDLE_TIMEOUT_SEC=10`)를 돌리던 중, **두 번째 실측 버그**를 발견함:

| # | 심각도 | 내용 | 근거 |
|---|:---:|---|---|
| F | **Critical**(실측 발견) | `idle_watchdog`이 `_main_task.cancel()`로 종료를 시도하지만, `mcp.server.stdio`/`app.run()` 내부(추정 anyio 기반)가 이 cancellation을 기대한 대로 전파하지 않음 — 로그(`idle timeout — exiting`)는 정확히 찍히는데 **프로세스는 3분 넘게 계속 살아있음** | 실측: cloud0에서 idle timeout 10초로 프로세스를 띄우고 90초 대기 후 `poll()` 확인 → `None`(안 죽음). 로그 파일에는 종료 메시지가 정상적으로 있었음 |

**처리**: ✅ **Fixed, cloud0 재검증 PASS(exit code 0)** — `os._exit(0)` 직접 호출로 변경(Design §2.2.1 신규). 이 경로는 `not _has_running_job()`이 이미 보장되므로 `atexit` 우회가 무해함. `_main_task` 전역과 `main()`의 `except CancelledError` 분기 제거. mock 기반 유닛 테스트 2건 추가(`test_idle_watchdog_calls_os_exit_when_idle_and_no_job`, `test_idle_watchdog_does_not_exit_while_job_running`).

**Gap E/F 공통 교훈**: 둘 다 Do 단계 pytest(로컬)와 gap-detector 정적 분석을 통과한 뒤에도 남아있던 버그였고, 오직 cloud0에 실제로 배포해 프로세스를 띄워본 뒤에야 발견됐다 — Design §8.4가 이 항목들을 "Check 단계 수동"으로 분류해둔 게 정확히 맞아떨어진 사례.

---

## 3~7. Code Quality / Performance / Test Coverage 상세 / Clean Architecture / Convention

N/A 또는 위 §2에 통합 — 단일 파일(488줄) 경량 프로젝트로 별도 레이어/네이밍 컨벤션 문서가 없다(Design §9/§10과 동일하게 N/A). 코드 스멜·보안 이슈는 gap-detector 검토에서 발견되지 않음(하드코딩 시크릿 없음, `subprocess.run(["bash","-c",...])` 기반 명령 실행 모델은 Design §7에서 이미 리스크 인지 및 범위 밖으로 명시).

---

## 8. Overall Score

```
┌─────────────────────────────────────────────┐
│  Overall Match Rate: 100%                    │
├─────────────────────────────────────────────┤
│  Structural:  100%                           │
│  Functional:  100%                           │
│  Contract:    100%                           │
│  Test Coverage(L1+L2 proxy): 10/10 + 5/5     │
│  L3(cloud0 실측):            5/5             │
└─────────────────────────────────────────────┘
```

**모든 Gap(A/B/C/D/E/F) 수정 완료, 테스트 26→31개 전부 PASS(로컬+cloud0), cloud0 L3 실배포 스모크 5/5 PASS.** 배포·검증까지 전 과정 완료.

---

## 9. Recommended Actions

### 9.1 완료됨 (Checkpoint 5 "지금 모두 수정" + cloud0 L3 스모크)

| 항목 | 파일 | 결과 |
|---|---|---|
| Gap A: SIGKILL 승격 테스트 추가 | `tests/test_lifecycle.py` | ✅ mock 테스트 + cloud0 실제 trap 검증(5.02s, forced) 둘 다 PASS |
| Gap B: `[stderr]` 분기 테스트 추가 | `tests/test_tools_smoke.py` | ✅ 완료 |
| Gap C: Design §4.1 step 4 문구 정정 | Design §4.1 | ✅ 완료 |
| Gap D: "6개"→"7개" 표기 정정 | Plan §2.2, Design §4 외 6곳 | ✅ 완료 |
| Gap E(실측 발견): process-group kill 버그 | `ssh_agent.py`(`_kill_group`, `start_new_session=True`) | ✅ 완료, cloud0 실측 재검증 PASS |
| Gap F(실측 발견): `idle_watchdog` 미종료 버그 | `ssh_agent.py`(`os._exit(0)`) | ✅ 완료, cloud0 실측 재검증 PASS |

### 9.2 Check 단계 수동 항목 — 완료 (cloud0, 2026-07-07)

- [x] 반복 연결/해제 → 프로세스 수 상한 확인 — PASS
- [x] bg job 미폴링 방치 → 좀비 미잔존 확인 — PASS
- [x] idle timeout 실동작 확인 — PASS(Gap F 수정 후)
- [x] process-group kill — 자식까지 종료되는지 확인 — PASS(Gap E 수정 후)
- [x] SIGTERM 무시 job → 실제 SIGKILL 승격 확인 — PASS

배포: git commit/push → cloud0 `git pull`(fast-forward, `/opt/ssh-mcp`). 기존 활성 세션(pid 5445, 7299)은 파일 교체 영향 없이 계속 구동(설계대로).

---

## 10. Design Document Updates Needed

- [x] Design §4.1 step 4 문구 정정(Gap C) — 완료
- [x] Plan §2.2 / Design §4의 "6개 tool" 표기 정정(Gap D) — 완료
- [x] Design §4.1.1(신규) — process-group kill 설계 및 근거 추가(Gap E) — 완료
- [x] Design §2.2.1(신규) — idle-exit `os._exit(0)` 전환 근거 추가(Gap F) — 완료

---

## 11. Next Steps

- [x] Gap A/B/C/D 수정 완료
- [x] Gap E/F(실측 발견) 수정 완료
- [x] Check 단계 수동 항목(cloud0, 5건) 완료 — 전부 PASS
- [ ] 완료 보고서 작성(`ssh-mcp-process-lifecycle.report.md`)

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-07-07 | 초안 — gap-detector 정적 분석 결과 반영. Overall ~99%, Critical 0건, Important 1건(Gap A: SIGKILL 분기 테스트 부재), Minor 3건(B/C/D) | hoseung.lee |
| 0.3 | 2026-07-07 | cloud0 L3 스모크 5/5 PASS로 완료. 스모크 도중 Gap F(`idle_watchdog`이 로그는 찍히나 실제 종료 안 됨 — `os._exit(0)`으로 수정) 신규 발견·수정·재검증. Gap E(process-group kill)도 cloud0에서 실제 자식 프로세스 종료 확인. 배포: git push → cloud0 pull. Overall Match Rate 100%로 최종 확정. | hoseung.lee |
| 0.2 | 2026-07-07 | Checkpoint 5 "지금 모두 수정" 진행 — Gap A/B/C/D 전부 수정(테스트 26→29개), Design/Plan 문서 정정. 수정 검증 중 **Gap E(신규, Critical급)** 실측 발견: `ssh_bg_kill`/`atexit_handler`가 bash 래퍼만 죽이고 실제 명령(자식)은 고아로 방치되는 프로세스 그룹 버그 — `start_new_session=True`+`_kill_group()`(POSIX `os.killpg`)으로 수정, Design §4.1.1 신규 반영. Overall ~99% → ~100%(L3 cloud0 수동 항목 5건 제외). | hoseung.lee |
