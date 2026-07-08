# ssh-mcp-process-lifecycle Completion Report

> **Status**: Complete
>
> **Project**: ssh-mcp (`Todoc/fpga/ssh-mcp/`)
> **Version**: N/A (단일 파일, 버전 관리 없음)
> **Author**: hoseung.lee
> **Completion Date**: 2026-07-07
> **PDCA Cycle**: #1

### Pipeline References

N/A — 이 프로젝트는 웹앱 파이프라인을 사용하지 않는다(Plan/Design/Analysis와 동일).

---

## Executive Summary

### 1.1 Project Overview

| Item | Content |
|------|---------|
| Feature | ssh-mcp-process-lifecycle |
| Start Date | 2026-07-07 |
| End Date | 2026-07-07 |
| Duration | 1일 (단일 세션 내 Plan→Design→Do→Check 전 사이클 완료, cloud0 L3 스모크 포함) |

### 1.2 Results Summary

```
┌─────────────────────────────────────────────┐
│  Completion Rate: 100%                       │
├─────────────────────────────────────────────┤
│  ✅ Complete:     5 / 5 FR                    │
│  ⏳ In Progress:   0 / 5 FR                    │
│  ❌ Cancelled:     0 / 5 FR                    │
└─────────────────────────────────────────────┘
```

### 1.3 Value Delivered

| Perspective | Content |
|-------------|---------|
| **Problem** | `ssh_agent.py`가 xcelium-mcp와 동일한 콜드 spawn·무정리 구조로 동작해, 재연결마다 새 프로세스가 쌓이고 `ssh_bg_run` 백그라운드 job은 미폴링/부모 사망 시 좀비·고아로 방치됐다. |
| **Solution** | idle self-exit(옵션 A, `idle_watchdog`) + 백그라운드 job reaper(`job_sweep`) + 프로세스 종료 시 정리(`atexit_handler`) + 신규 `ssh_bg_kill` tool(PID 사이드카 파일로 부모 재시작 후에도 취소 가능)을 구현했다. |
| **Function/UX Effect** | cloud0 반복 연결/해제에도 프로세스 수가 상한 내로 유지되고, 미폴링 job도 60초 tick마다 reap되어 좀비로 안 남는다. 실행 중인 job은 idle-exit 조건에서 항상 보존된다. 8번째 tool(`ssh_bg_kill`)로 재연결 후에도 이전 job을 명시적으로 취소할 수 있게 됐다. |
| **Core Value** | ssh-mcp는 사용자 전역 설정이라 fpga 하위 모든 프로젝트가 공유한다 — 이 사이클로 리소스 누적 방지 기반을 전 프로젝트에 한 번에 적용했다. Check 단계 cloud0 실측에서 정적 분석으로는 잡을 수 없는 2건의 Critical 버그(프로세스 그룹 kill 누락, idle-exit 미종료)를 발견·수정해, "설계·코드 리뷰를 통과해도 실제 배포 검증이 필요하다"는 걸 재확인했다. |

---

## 1.4 Success Criteria Final Status

> Plan §4.1 Definition of Done / Analysis §2.6 L3 결과 반영 — 최종 평가(Analysis 상단 요약 테이블이 L3 완료 전 스냅샷이라 하단 §2.6~§8의 최종 결과로 갱신).

| # | Criteria | Status | Evidence |
|---|---------|:------:|----------|
| SC-1 | FR-01 idle self-exit — 반복 연결/해제해도 무한 누적 안 됨 | ✅ Met | `idle_watchdog` 구현 + cloud0 L3: `SSH_MCP_IDLE_TIMEOUT_SEC` 단축 테스트로 실제 self-exit 확인(Gap F 수정 후 PASS) |
| SC-2 | FR-02 `ssh_bg_run` job이 poll 여부와 무관하게 reap됨 | ✅ Met | `job_sweep()` 매 tick `.poll()` 호출, 로컬 테스트 pass |
| SC-3 | FR-03 부모 종료 시 미완료 job 명시적 처리 | ✅ Met | `atexit_handler` + `_terminate_sync`(SIGTERM→5s→SIGKILL), Gap E 수정으로 프로세스 그룹 전체 종료 확인 |
| SC-4 | FR-04 `/tmp/mcp_job_*.txt` 무기한 누적 방지 | ✅ Met | `job_sweep()` 24h 후 outfile+pidfile 정리 |
| SC-5 | FR-05 `ssh_bg_kill`(job_id) — SIGTERM→5s→SIGKILL, 부모 재시작 후에도 pidfile fallback으로 kill 가능 | ✅ Met | cloud0 L3: SIGTERM 무시 job도 5.02s 후 SIGKILL 강제 승격 확인 |
| SC-6 | 기존 tool 시그니처 불변 | ✅ Met | Contract 축 100%(Analysis §2.5) |
| SC-7 | cloud0 반복 연결/해제 스모크로 프로세스 상한 확인 | ✅ Met | Analysis §2.6 L3 시나리오 1 — 테스트 스폰 전/후 프로세스 수 동일, 누수 없음 |
| SC-8 | 의도적 미폴링 후 job 정리 확인 | ✅ Met | Analysis §2.6 L3 시나리오 2 — `job_sweep()` 호출 후 defunct 0건 |

**Success Rate**: 8/8 criteria met (100%)

## 1.5 Decision Record Summary

| Source | Decision | Followed? | Outcome |
|--------|----------|:---------:|---------|
| [Plan] | 아키텍처는 안C+(xcelium-mcp 프리포크 수퍼바이저) 그대로 재사용 대신 더 가벼운 대안(A/C) 우선 검토 | ✅ | Design에서 옵션 A(idle self-exit) 최종 선택, 프리포크 수퍼바이저 도입 안 함 |
| [Design] | 옵션 A 채택 — 근거: ssh-mcp는 무거운 상태가 없는 경량 stdio 서버라 B(수퍼바이저)는 과설계, C(cron reaper)는 공유 호스트 오탐 위험 | ✅ | `idle_watchdog` + `job_sweep` + `atexit_handler`로 구현, launch 설정(`~/.claude.json`) 불변 |
| [Design] | Kill 정책: SIGTERM→5초→SIGKILL 자동 승격 | ✅ | `_kill_pid_async`/`_terminate_sync` 동일 패턴, cloud0 L3로 실제 승격 확인 |
| [Design] | PID 사이드카 파일로 부모 재시작 후에도 kill/poll 가능(FR-05) | ✅ | `_pid_alive`, `ssh_bg_kill`/`ssh_bg_poll` fallback 분기 구현·테스트 |
| [Design→실측 정정] | idle-exit 종료 방식: `_main_task.cancel()`로 cooperative shutdown 기대 | ❌→✅ 정정 | cloud0 실측 결과 `mcp.server.stdio` 내부가 cancellation을 기대대로 전파하지 않아 프로세스가 안 죽는 Critical 버그(Gap F) 발견 — `os._exit(0)` 직접 호출로 수정, 재검증 PASS |
| [Design→실측 정정] | `ssh_bg_kill`/`atexit_handler`가 job의 pid에 직접 시그널 전송 | ❌→✅ 정정 | 실측 결과 그 pid는 bash 래퍼였고 실제 명령(자식)은 고아로 방치되는 Critical 버그(Gap E) 발견 — `start_new_session=True`+`_kill_group()`(`os.killpg`)으로 수정, cloud0 재검증 PASS |

---

## 2. Related Documents

| Phase | Document | Status |
|-------|----------|--------|
| Plan | [ssh-mcp-process-lifecycle.plan.md](../01-plan/features/ssh-mcp-process-lifecycle.plan.md) | ✅ Finalized |
| Design | [ssh-mcp-process-lifecycle.design.md](../02-design/features/ssh-mcp-process-lifecycle.design.md) | ✅ Finalized (v0.5, Gap E/F 반영) |
| Check | [ssh-mcp-process-lifecycle.analysis.md](../03-analysis/ssh-mcp-process-lifecycle.analysis.md) | ✅ Complete (v0.3, L3 5/5 PASS) |
| Report | 현재 문서 | ✅ Complete |

---

## 3. Completed Items

### 3.1 Functional Requirements

| ID | Requirement | Status | Notes |
|----|-------------|--------|-------|
| FR-01 | `ssh_agent.py` 프로세스가 반복 연결/해제에도 cloud0에 무한 누적되지 않음 | ✅ Complete | `idle_watchdog`, Gap F 수정(`os._exit(0)`)으로 실제 종료 확인 |
| FR-02 | `ssh_bg_run` 백그라운드 프로세스는 `ssh_bg_poll` 호출 여부와 무관하게 reap됨 | ✅ Complete | `job_sweep()` |
| FR-03 | `ssh_agent.py` 종료 시 미완료 job 명시적 처리 | ✅ Complete | `atexit_handler`, Gap E 수정(프로세스 그룹 kill)으로 실제 자식까지 종료 확인 |
| FR-04 | `/tmp/mcp_job_*.txt` 출력 파일 무기한 누적 방지 | ✅ Complete | `job_sweep()` 24h 정리 |
| FR-05 | 신규 `ssh_bg_kill(job_id)` tool — SIGTERM→5s→SIGKILL, 부모 재시작 후에도 취소 가능 | ✅ Complete | PID 사이드카 파일(`.pid`) 기반 fallback |

### 3.2 Non-Functional Requirements

| Item | Target | Achieved | Status |
|------|--------|----------|--------|
| 회귀 없음 | 기존 7개 tool 동작·응답 포맷 유지 | Contract 축 100%(byte-identical 응답) | ✅ |
| 권한 | root/sudo 없이 배포 가능 | cloud0 실측 확인, 옵션 A는 launch 설정도 불변 | ✅ |
| 이식성 | 프로젝트 종속 가정 없음 | 코드 리뷰 통과 | ✅ |
| Test Coverage | 최소 pytest 골격 마련(기존 0개) | 26→31개 전부 pass(로컬+cloud0) | ✅ |

### 3.3 Deliverables

| Deliverable | Location | Status |
|-------------|----------|--------|
| Lifecycle 코드 | `ssh_agent.py`(`# ─── Lifecycle ───` 섹션) | ✅ |
| 신규 tool | `ssh_bg_kill`(`list_tools()`/`call_tool()`) | ✅ |
| 테스트 | `tests/conftest.py`, `tests/test_tools_smoke.py`, `tests/test_lifecycle.py` | ✅ |
| 문서 | `docs/01-plan`, `docs/02-design`, `docs/03-analysis` | ✅ |
| cloud0 배포 | `git push` → cloud0 `git pull`(fast-forward) | ✅ |

---

## 4. Incomplete Items

### 4.1 Carried Over to Next Cycle

| Item | Reason | Priority | Estimated Effort |
|------|--------|----------|-------------------|
| `idle_watchdog`(고정 30분 활동 타이머) 자체의 근본 한계 — "활동 없음"과 "연결 끊김"을 혼동해 조용한 세션도 강제 종료 | Check 단계 이후 실사용 중 발견된 UX 문제, 이번 사이클 범위 밖(FR-01은 "설계대로" 정확히 동작했음) | High | 별도 사이클로 분리 진행 — **`ssh-mcp-idle-culler`(2026-07-08, 완료)**: FR-01을 EOF 기반 종료 + SSH keepalive + 외부 idle-culler로 재정의해 근본 해결 |

### 4.2 Cancelled/On Hold Items

| Item | Reason | Alternative |
|------|--------|-------------|
| - | - | - |

---

## 5. Quality Metrics

### 5.1 Final Analysis Results

| Metric | Target | Final | Change |
|--------|--------|-------|--------|
| Design Match Rate | 90% | 100% | +10%p |
| Structural Match | - | 100% | - |
| Functional Depth | - | 100%(Gap E/F 수정 후) | - |
| Contract Match | - | 100% | - |
| Test Suite | 최소 골격 | 26→31개 전부 pass | - |
| L3 Runtime(cloud0 실측) | - | 5/5 PASS | - |
| Critical Gap | 0 | 0(발견된 2건 모두 수정·재검증 완료) | ✅ |

### 5.2 Resolved Issues

| Issue | Resolution | Result |
|-------|------------|--------|
| Gap A: SIGKILL 강제 승격 분기 테스트 없음 | mock 기반 유닛 테스트 추가 + cloud0 실제 trap 기반 검증(5.02s, forced) | ✅ Resolved |
| Gap B: `ssh_run` `[stderr]` 분기 미검증 | 테스트 추가 | ✅ Resolved |
| Gap C: Design §4.1 step 4 문구가 실제 응답 포맷과 불일치 | 문서 정정 | ✅ Resolved |
| Gap D: "6개 tool" 표기 오류(실제 7개) | Plan/Design 6곳 정정 | ✅ Resolved |
| **Gap E(Critical, 실측 발견)**: `ssh_bg_kill`/`atexit_handler`가 bash 래퍼 pid만 kill해 실제 명령(자식)이 고아로 방치됨 | `start_new_session=True` + `_kill_group()`(`os.killpg`)로 프로세스 그룹 전체 kill | ✅ Resolved, cloud0 재검증 PASS |
| **Gap F(Critical, 실측 발견)**: `idle_watchdog`이 `_main_task.cancel()`로 종료를 시도하지만 `mcp.server.stdio` 내부가 cancellation을 전파하지 않아 실제로는 안 죽음 | `os._exit(0)` 직접 호출로 변경 | ✅ Resolved, cloud0 재검증 PASS |

---

## 6. Lessons Learned & Retrospective

### 6.1 What Went Well (Keep)

- Design 단계에서 3개 아키텍처(A/B/C)를 실측 근거로 비교하고 사용자 Checkpoint로 확정한 것 — 과설계(수퍼바이저) 없이 프로젝트 규모에 맞는 가벼운 해법 선택
- 테스트 인프라가 전무했던 프로젝트에 Do 단계에서 pytest 골격을 먼저 마련(Plan §5 리스크 대응)한 뒤 lifecycle 코드를 얹은 순서 — 회귀 안전망을 먼저 확보
- Check 단계에서 정적 분석(gap-detector)에 그치지 않고 실제 프로세스 트리를 cloud0에 띄워 검증한 것이 Critical 버그 2건(Gap E/F)을 잡아냄 — 둘 다 코드 리뷰만으로는 절대 발견 불가능한 종류

### 6.2 What Needs Improvement (Problem)

- Design 단계에서 `_main_task.cancel()`이 실제로 `mcp.server.stdio` 내부에서 어떻게 처리되는지 SDK 내부 동작까지 검증하지 않고 "합리적으로 보이는 방식"으로 설계함 — 제3자 SDK와의 상호작용은 로컬 mock 테스트로 검증되지 않는다는 걸 미리 인지했어야 함
- `ssh_bg_run`의 `Popen(["bash","-c",cmd])`이 래퍼 프로세스라는 사실을 Design 단계에서 명시적으로 검토하지 않음 — "pid로 kill하면 된다"는 암묵적 가정이 실측 전까지 발견되지 않음
- FR-01(idle self-exit)이 "정확히 설계대로 동작"했음에도 그 설계 기준 자체(활동=연결 생사로 간주)가 실사용과 안 맞았다는 건 이 사이클이 끝난 뒤에야(실사용 피드백으로) 발견됨 — Design 단계에서 "이 타임아웃이 사용자에게 어떻게 느껴질지"까지 검토했다면 더 일찍 잡을 수 있었을 것

### 6.3 What to Try Next (Try)

- 프로세스 종료/시그널 관련 로직은 Design 단계에서부터 "이 SDK/OS 메커니즘이 정말 그렇게 동작하는가"를 최소 1회 실제로 스파이크(spike) 테스트해보고 설계 확정
- 백그라운드 프로세스를 감싸는 wrapper(`bash -c`) 패턴을 쓸 때는 항상 "누구를 kill하고 있는가"를 Design 단계 체크리스트 항목으로 명시
- 타임아웃/lifecycle 파라미터를 설계할 때 "이 값이 실제 사용자 워크플로우에서 얼마나 자주 트리거될지" 사용 시나리오를 함께 적어두면, 이번처럼 별도 후속 사이클(`ssh-mcp-idle-culler`)로 다시 다뤄야 하는 재작업을 줄일 수 있음

---

## 7. Process Improvement Suggestions

### 7.1 PDCA Process

| Phase | Current | Improvement Suggestion |
|-------|---------|------------------------|
| Design | 제3자 SDK 내부 동작(`mcp.server.stdio`의 cancellation 전파 등)에 대한 가정을 검증 없이 설계에 반영 | 외부 라이브러리 동작에 의존하는 설계 결정에는 "가정" 표시를 달고, Do 단계 초반에 최소 스파이크로 조기 검증 |
| Check | 정적 분석만으로는 Gap E/F 같은 실행 시점 이슈를 못 잡음 | 프로세스/시그널 관련 기능은 Design §8 Test Plan에 L3(실제 배포 스모크)를 기본 포함시키는 걸 컨벤션화(이번 사이클은 이미 그렇게 했고 실제로 효과가 있었음 — 이 패턴 유지 권장) |

### 7.2 Tools/Environment

| Area | Improvement Suggestion | Expected Benefit |
|------|------------------------|------------------|
| 테스트 인프라 | 이번에 신규 마련된 pytest 골격을 후속 사이클(`ssh-mcp-idle-culler`)에서도 그대로 확장 재사용함(이미 실현됨) | 사이클 간 회귀 방지 비용 감소 |

---

## 8. Next Steps

### 8.1 Immediate

- [x] cloud0 배포 완료(git push → pull)
- [x] Check 단계 수동 항목 전부 완료
- [ ] `/pdca archive ssh-mcp-process-lifecycle` — 이 사이클 문서 아카이빙

### 8.2 Next PDCA Cycle

| Item | Priority | Expected Start |
|------|----------|-----------------|
| `ssh-mcp-idle-culler` — FR-01의 근본 한계(활동≠연결) 해결 | High | 2026-07-08 (이미 완료) |

---

## 9. Changelog

### v1.0.0 (2026-07-07)

**Added:**
- `idle_watchdog()`, `job_sweep()`, `atexit_handler()` — 프로세스/job lifecycle 관리
- 신규 tool `ssh_bg_kill(job_id)` — PID 사이드카 파일 기반, 부모 재시작 후에도 취소 가능
- `tests/` 디렉토리 — 최초 pytest 골격(26→31개 테스트)

**Changed:**
- `ssh_bg_run` — `start_new_session=True`로 독립 프로세스 그룹 생성(Gap E 수정)
- `ssh_bg_poll` — outfile/pidfile 기반 fallback 경로 추가(부모 재시작 대응)

**Fixed:**
- Gap E(Critical): 프로세스 그룹 kill 누락 — 래퍼만 죽고 실제 명령은 고아로 방치되던 버그
- Gap F(Critical): idle-exit이 로그만 찍히고 실제로 종료되지 않던 버그

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 1.0 | 2026-07-08 | 완료 보고서 작성 — Success Rate 8/8(100%, Analysis §2.6 L3 결과로 상단 요약 테이블 갱신), Overall Match Rate 100%, Critical Gap 2건(E/F) 모두 cloud0 재검증까지 완료. 후속 발견된 FR-01 근본 한계는 `ssh-mcp-idle-culler` 사이클로 분리·완료됨을 §4.1에 기록. | hoseung.lee |
