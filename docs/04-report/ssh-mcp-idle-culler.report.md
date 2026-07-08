# ssh-mcp-idle-culler Completion Report

> **Status**: Complete
>
> **Project**: ssh-mcp (`Todoc/fpga/ssh-mcp/`)
> **Version**: N/A (단일 파일, 버전 관리 없음)
> **Author**: hoseung.lee
> **Completion Date**: 2026-07-08
> **PDCA Cycle**: #1

### Pipeline References

N/A — 이 프로젝트는 웹앱 파이프라인을 사용하지 않는다(Plan/Design/Analysis와 동일).

---

## Executive Summary

### 1.1 Project Overview

| Item | Content |
|------|---------|
| Feature | ssh-mcp-idle-culler |
| Start Date | 2026-07-08 |
| End Date | 2026-07-08 |
| Duration | 1일 (단일 세션 내 Plan→Design→Do→Check 전 사이클 완료) |

### 1.2 Results Summary

```
┌─────────────────────────────────────────────┐
│  Completion Rate: 100%                       │
├─────────────────────────────────────────────┤
│  ✅ Complete:     4 / 4 FR                    │
│  ⏳ In Progress:   0 / 4 FR                    │
│  ❌ Cancelled:     0 / 4 FR                    │
└─────────────────────────────────────────────┘
```

### 1.3 Value Delivered

| Perspective | Content |
|-------------|---------|
| **Problem** | `idle_watchdog`(고정 30분 활동 타이머)이 "앱 레벨 활동 없음"을 "연결 끊김"과 동일시해, 사용자가 조용히 코드를 읽거나 생각만 해도 30분 뒤 프로세스가 자체 종료되어 매번 `/mcp` 재연결이 필요했다. |
| **Solution** | 종료 기준을 `stdin EOF`(연결이 실제로 끊겼을 때만 발생)로 전환하고, 죽은 연결은 클라이언트 SSH keepalive로 조기 감지(~90초)하며, 방치된 프로세스는 xcelium-mcp에서 검증된 패턴을 이식한 `idle_culler.py`(orphan+age 휴리스틱, cloud0 cron)가 최종 안전망으로 정리한다. |
| **Function/UX Effect** | 연결이 살아있는 한 idle 시간과 무관하게 재연결이 불필요해졌다(cloud0 실측: 라이브 세션이 `age=1876s` 동안 무손상 생존 확인). 죽은 연결/방치 프로세스는 기존 30분보다 훨씬 빠르게(고아는 즉시, 백스톱은 최대 6시간이지만 keepalive 경로가 먼저 ~90초 내 처리) 정리된다. |
| **Core Value** | "활동 감지" 대신 "연결 생사"를 기준으로 프로세스 lifecycle을 관리해, 리소스 누적 방지라는 FR-01 원래 목적은 유지하면서 정상 사용 세션의 불필요한 재연결 마찰을 제거했다. |

---

## 1.4 Success Criteria Final Status

> Plan §4.1/§4.2 — 최종 평가.

| # | Criteria | Status | Evidence |
|---|---------|:------:|----------|
| DoD-1 | `idle_watchdog`/`IDLE_TIMEOUT_SEC`/`WATCHDOG_INTERVAL_SEC`/`_last_activity` 완전 제거, job_sweep 등과 결합 없이 동작 | ✅ Met | `ssh_agent.py` grep 결과 참조 주석 외 코드 참조 0건, `job_sweep()`은 `call_tool()` 진입부(`:304`)로 이동, 46개 테스트 전부 pass |
| DoD-2 | idle-culler 스크립트 작성 및 단위 테스트(로컬, `/proc` mock) | ✅ Met | `idle_culler.py` 신규 160줄, `tests/test_idle_culler.py` L1 9개 + L2 9개(Windows: skip, cloud0: 실행 pass) |
| DoD-3 | cloud0 cron 배포 완료 | ✅ Met | `git push`→cloud0 `git pull`(`270b001`, fast-forward)→crontab 병합 등록(`*/5 * * * * .../idle_culler.py`, 기존 xcelium-mcp 항목 보존) |
| DoD-4 | 문서화(Design/Do 문서, README 갱신 필요 시) | ✅ Met | `skill/ssh-mcp/SKILL.md`의 "30분 idle timeout" 서술을 EOF+keepalive+culler 모델로 갱신 |
| QC-1 | 기존 `ssh-mcp-process-lifecycle` 테스트(SC-2~SC-8) 회귀 없음 | ✅ Met | job reap/kill/atexit 테스트 전부 pass, 신규 `test_call_tool_triggers_job_sweep` 추가 |
| QC-2 | `idle_watchdog` 제거로 인한 미사용 import/전역 변수 없음(lint 클린) | ✅ Met | 제거 대상 심볼 grep 결과 코드 참조 0건 |
| QC-3 | cloud0 L3 실측: 정상 세션 생존 + 죽은 연결 조기 정리 + 오탐 없음 3가지 모두 확인 | ✅ Met | Analysis §2.6 L3 — 5/5 PASS |

**Success Rate**: 7/7 criteria met (100%)

## 1.5 Decision Record Summary

| Source | Decision | Followed? | Outcome |
|--------|----------|:---------:|---------|
| [Plan] | 프로세스 종료 트리거: (A) 앱 내부 타이머 유지 vs (B) EOF 전용 + 외부 culler | ✅ (B) | `main()`이 `stdio_server()` EOF 경로만 남김 — cloud0 실측으로 정상 세션 생존 확인 |
| [Plan] | 죽은 연결 탐지 위치: 서버 측 자체 탐지 vs 클라이언트 SSH keepalive | ✅ 클라이언트 keepalive | 코드 변경 없음(이미 로컬 적용 완료), 설계 전제로만 문서화 |
| [Plan] | 방치 프로세스 안전망: 앱 자체 타이머 vs 외부 `/proc` 관찰 cron | ✅ 외부 cron | xcelium-mcp `idle_culler.py` 이식 |
| [Design] | Architecture Option A(orphan+age 휴리스틱) vs Option B(sshd TCP 상태 정밀 확인) | ✅ Option A | cloud0 실측으로 Option B가 root 없이는 근본적으로 불가능함을 Design 단계에서 사전 확인, 이식 비용 가정 정정 |
| [Design] | `ORPHAN_PPID=1` 가정 | ✅ | L3 스모크 시나리오 1/4에서 실제 고아 프로세스가 `ppid=1`로 재부모됨을 실측 확인 — 가정 그대로 유효 |
| (Do 단계 승인된 이탈, Design 미명시) | `job_sweep()` 호출 시점을 watchdog 루프 → `call_tool()` 진입부로 이동 | ✅ | Design이 watchdog 제거 후 job_sweep의 트리거 공백을 언급하지 않아 사용자에게 직접 확인 후 결정, 회귀 테스트로 검증 |

---

## 2. Related Documents

| Phase | Document | Status |
|-------|----------|--------|
| Plan | [ssh-mcp-idle-culler.plan.md](../01-plan/features/ssh-mcp-idle-culler.plan.md) | ✅ Finalized |
| Design | [ssh-mcp-idle-culler.design.md](../02-design/features/ssh-mcp-idle-culler.design.md) | ✅ Finalized |
| Check | [ssh-mcp-idle-culler.analysis.md](../03-analysis/ssh-mcp-idle-culler.analysis.md) | ✅ Complete (v0.2, module-3 반영) |
| Report | 현재 문서 | ✅ Complete |

---

## 3. Completed Items

### 3.1 Functional Requirements

| ID | Requirement | Status | Notes |
|----|-------------|--------|-------|
| FR-01 (재정의) | `ssh_agent.py`는 `idle_watchdog` 없이 `stdin` EOF 시에만 정상 종료 | ✅ Complete | `main()` 단순화, watchdog task 생성 제거 |
| FR-02 | 죽은 연결은 클라이언트 SSH keepalive로 ~90초 내 감지되어 EOF로 전파 | ✅ Complete | 클라이언트 설정은 사전 적용 완료(이 저장소 범위 밖), 코드 측은 EOF 경로 존재로 충족 |
| FR-03 | 방치된 `ssh_agent.py` 프로세스는 cloud0 외부 idle-culler cron이 주기적으로 탐지·정리 | ✅ Complete | `idle_culler.py` 배포 + crontab 등록, L3 스모크로 실동작 검증 |
| FR-04 | idle-culler는 `/proc` 순수 관찰만 수행, `ssh_agent.py` 내부에 신규 스레드/파일 미추가 | ✅ Complete | `idle_culler.py`는 독립 프로세스, `ssh_agent.py` 코드는 idle-culler 관련 추가 없음(watchdog 제거만) |

### 3.2 Non-Functional Requirements

| Item | Target | Achieved | Status |
|------|--------|----------|--------|
| Reliability(정상 세션 유지) | idle 시간과 무관하게 유지 | cloud0 L3: age 1876s 세션 무손상 생존 확인(시나리오 2) | ✅ |
| Responsiveness(죽은 연결 정리) | 기존(30분)보다 빠르게(~90초) | 코드 경로 존재(FR-02), 고아 프로세스는 즉시(L3 시나리오 1) | ✅ |
| Safety(오탐 방지) | 정상 동작 중인 프로세스를 오탐으로 죽이지 않음 | L3 시나리오 2/4에서 라이브 세션 및 job-실행-중 프로세스 보존 확인, 실제 대화 세션 무손상 | ✅ |
| Test Coverage | 로컬+cloud0 전체 pass | 46/46 pass(cloud0), 37 pass + 9 skip(Windows) | ✅ |

### 3.3 Deliverables

| Deliverable | Location | Status |
|-------------|----------|--------|
| idle-culler 스크립트 | `idle_culler.py` | ✅ |
| ssh_agent.py 정리 | `ssh_agent.py` | ✅ |
| 배포 설정 | `deploy/crontab.example` + cloud0 crontab | ✅ |
| 테스트 | `tests/test_idle_culler.py`, `tests/test_lifecycle.py` | ✅ |
| 문서 | `docs/01-plan`, `docs/02-design`, `docs/03-analysis`, `skill/ssh-mcp/SKILL.md` | ✅ |

---

## 4. Incomplete Items

### 4.1 Carried Over to Next Cycle

없음 — 이번 사이클 범위(module-1+2+3) 전 항목 완료.

### 4.2 Cancelled/On Hold Items

| Item | Reason | Alternative |
|------|--------|-------------|
| - | - | - |

---

## 5. Quality Metrics

### 5.1 Final Analysis Results

| Metric | Target | Final | Change |
|--------|--------|-------|--------|
| Design Match Rate | 90% | 99.2~99.5% | +9.2~9.5%p |
| Structural Match | - | 100% | - |
| Functional Depth | - | 98% | - |
| Contract Match | - | 100% | - |
| Test Suite (cloud0) | 전부 pass | 46/46 pass | - |
| L3 Runtime (cloud0 실측) | 5/5 | 5/5 PASS | - |
| Critical/Important Gap | 0 | 0 | ✅ |

### 5.2 Resolved Issues

| Issue | Resolution | Result |
|-------|------------|--------|
| Design이 watchdog 제거 후 `job_sweep()` 트리거 공백을 다루지 않음(Do 단계 발견) | 사용자 확인 후 `call_tool()` 진입부로 이동, 회귀 테스트 추가 | ✅ Resolved |
| `ssh_run`으로 백그라운드 job을 nohup 없이 띄우면 stdout/stderr pipe 상속으로 타임아웃(L3 스모크 중 실측 발견) | `ssh_bg_run` 또는 `nohup ... > file 2>&1 &` 명시적 리다이렉트로 우회 | ✅ Resolved(작업 방법론 교훈, 코드 결함 아님) |
| `find_ssh_agent_pids()`가 SSH `tcsh -c` 로그인 셸 래퍼도 함께 매칭(L3 스모크 중 발견, Gap 3) | 실측 확인 결과 `has_live_children` 가드로 항상 보호됨 — 코드 변경 불필요, Analysis에 기록만 | ✅ 정보성 관찰, 조치 불요 |

---

## 6. Lessons Learned & Retrospective

### 6.1 What Went Well (Keep)

- Design 단계에서 실측(cloud0)으로 Plan의 가정(Option B "이식 비용 낮음")을 미리 검증·정정한 것 — 잘못된 아키텍처로 Do에 진입하는 것을 사전에 방지
- xcelium-mcp의 검증된 `idle_culler.py` 패턴(파싱 함수, kill 승격, 방어적 코딩)을 그대로 재사용해 새로 설계할 필요 없이 이식만으로 완성도 확보
- L3 스모크를 "라이브 세션(현재 대화 자체)을 대상으로 안전 검증 + 별도 픽스처로 위험 시나리오 격리"라는 원칙으로 설계해, 실제 원격 프로세스를 죽이면서도 이 세션 자체는 무손상으로 검증 완료

### 6.2 What Needs Improvement (Problem)

- Design 문서가 `job_sweep()`의 트리거 공백처럼 "제거 대상의 연쇄 영향"을 완전히 다루지 못한 부분이 있었음 — Do 단계에서 코드를 실제로 읽다가 발견됨
- L3 스모크 스크립팅 중 `ssh_run`의 pipe-상속 타임아웃 함정에 두 번 걸림(30초 낭비) — 미리 알았다면 처음부터 `ssh_bg_run`/명시적 리다이렉트로 시작했을 것

### 6.3 What to Try Next (Try)

- 다음에 watchdog류 코드를 제거할 때는 "이 함수를 호출하는 다른 곳이 있는가"뿐 아니라 "이 함수를 호출하던 타이머 자체가 없어지면 그 함수의 새 트리거는 어디인가"까지 Design 단계에서 명시적으로 결정
- 원격 프로세스 스모크 테스트를 짤 때 `ssh_run`으로 백그라운드 job을 절대 직접 `&`로 띄우지 말고 처음부터 `ssh_bg_run` 또는 `nohup ... > file 2>&1 &`을 기본값으로 사용

---

## 7. Process Improvement Suggestions

### 7.1 PDCA Process

| Phase | Current | Improvement Suggestion |
|-------|---------|------------------------|
| Design | 제거 대상 코드의 연쇄 영향(트리거 공백 등)을 Impact Analysis에서 놓칠 수 있음 | Design §6.2 Impact Analysis에 "제거되는 함수/타이머가 다른 함수의 유일한 호출자였는가"를 체크리스트 항목으로 추가 |
| Check | L3(cloud0) 스모크가 Do 세션과 분리되어 Check가 두 단계(static→L3)로 나뉨 | module 분할이 큰 기능은 Design 단계에서 Check도 "static Check"와 "L3 Check"로 미리 분리해 문서화하면 혼선이 줄어듦(이번 사이클에서 이미 자연스럽게 그렇게 됨) |

### 7.2 Tools/Environment

| Area | Improvement Suggestion | Expected Benefit |
|------|------------------------|------------------|
| ssh-mcp 원격 스모크 테스트 | `ssh_run` 사용 가이드에 "백그라운드 job은 절대 쓰지 말 것, ssh_bg_run 사용" 경고를 skill 문서에 추가 | 향후 유사 타임아웃 재발 방지 |

---

## 8. Next Steps

### 8.1 Immediate

- [x] cloud0 배포 완료
- [x] crontab 등록 완료
- [ ] `/pdca archive ssh-mcp-idle-culler` — 이 사이클 문서 아카이빙
- [ ] (선택) Analysis Gap 1 — Design §3.1 `_has_running_job()` 유지 근거 서술 정정

### 8.2 Next PDCA Cycle

| Item | Priority | Expected Start |
|------|----------|----------------|
| `ssh-mcp-process-lifecycle` 사이클 마무리(현재 Check 단계, 미커밋 문서 변경 존재) | Medium | 필요 시 |
| idle-culler 실사용 모니터링(며칠간 cron 로그 관찰, 오탐/미탐 없는지) | Low | 자연 관찰 |

---

## 9. Changelog

### v1.0.0 (2026-07-08)

**Added:**
- `idle_culler.py` — orphan+age 휴리스틱 기반 cloud0 cron 스크립트
- `deploy/crontab.example` — crontab 등록 가이드
- `tests/test_idle_culler.py` — L1/L2 단위 테스트

**Changed:**
- `ssh_agent.py` — `idle_watchdog` 제거, EOF 전용 종료 경로로 단순화, `job_sweep()` 호출 위치 이동
- `skill/ssh-mcp/SKILL.md` — 프로세스 lifecycle 서술을 EOF+keepalive+culler 모델로 갱신

**Fixed:**
- 30분 고정 활동 타이머로 인한 정상 세션 강제 종료/재연결 마찰 (근본 원인 해결)

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 1.0 | 2026-07-08 | 완료 보고서 작성 — module-1+2+3 전 범위 완료, Success Rate 7/7(100%), Match Rate 99.2~99.5% | hoseung.lee |
