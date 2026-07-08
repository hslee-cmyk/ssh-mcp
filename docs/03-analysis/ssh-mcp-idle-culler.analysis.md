# ssh-mcp-idle-culler Analysis Report

> **Analysis Type**: Gap Analysis (Design vs Implementation)
>
> **Project**: ssh-mcp (`Todoc/fpga/ssh-mcp/`)
> **Version**: N/A
> **Analyst**: gap-detector (via hoseung.lee)
> **Date**: 2026-07-08
> **Design Doc**: [ssh-mcp-idle-culler.design.md](../02-design/features/ssh-mcp-idle-culler.design.md)

### Pipeline References

N/A — 이 프로젝트는 웹앱 파이프라인을 사용하지 않는다(Plan/Design과 동일).

---

## Context Anchor

| Key | Value |
|-----|-------|
| **WHY** | `idle_watchdog`이 활동 없음=연결 끊김으로 잘못 가정해, 정상적으로 조용한 세션까지 강제 종료시켜 재연결 마찰을 유발함 |
| **WHO** | ssh-mcp로 cloud0에 접속해 장시간 작업하는 Claude Code 사용자 |
| **RISK** | EOF 기반 종료로 전환 시 keepalive 미설정 클라이언트는 죽은 연결이 방치될 수 있음 — 외부 idle-culler가 최종 안전망 |
| **SUCCESS** | (1) 정상 세션은 idle 시간과 무관하게 재연결 불필요 (2) 죽은 연결은 ~90초 내 정리 (3) 방치 프로세스는 idle-culler로 무한 누적 방지 |
| **SCOPE** | FR-01 재정의(watchdog 완전 제거) + cloud0 전용 idle-culler cron 신규 배포 |

---

## Scope of This Analysis

> **Update (2026-07-08, 같은 날 후속 세션)**: module-3(cloud0 배포 + L3 스모크)까지 완료됨. 아래 초안(v0.1) 본문은 static-only 스냅샷을 그대로 보존하고, module-3 결과는 §2.6 L3와 Match Rate Summary/Overall Score에 반영했다 — Plan §4.1 Definition of Done 전 항목 충족.
>
> (v0.1 원문) Design §11.3 Session Guide는 Do를 Session 2(module-1+2, 코드)와 Session 3(module-3, cloud0 배포)로 분리했고, 사용자는 Session 2에서 module-3(배포)를 명시적으로 다음 세션으로 미뤘다(Do 단계 Checkpoint 4 응답).

---

## Strategic Alignment Check

### PRD Alignment

N/A — 이 사이클은 `/pdca pm`을 거치지 않았다(PRD 문서 없음, `ssh-mcp-process-lifecycle` 사이클과 동일한 패턴).

### Plan Success Criteria Status (Plan §4.1 Definition of Done)

| # | Criteria | Status | Evidence |
|---|---|:---:|---|
| DoD-1 | `idle_watchdog`/`IDLE_TIMEOUT_SEC`/`WATCHDOG_INTERVAL_SEC`/`_last_activity` 코드 완전 제거, job_sweep 등과 결합 없이 동작 | ✅ | grep 결과 주석 외 참조 없음(`ssh_agent.py`). `job_sweep()`은 `call_tool()` 진입부(`:304`)에서 호출 — 정상 동작(37 tests pass) |
| DoD-2 | idle-culler 스크립트 작성 및 단위 테스트(로컬, `/proc` mock) | ✅ | `idle_culler.py` 신규(§4.1 pseudocode 전부 구현), `tests/test_idle_culler.py` L1 9개 + L2 9개(POSIX 전용, Windows skip) |
| DoD-3 | cloud0 cron 배포 완료 | ✅ **완료(2026-07-08)** | `git push` → cloud0 `git pull`(fast-forward, `270b001`) → crontab 병합 등록(`*/5 * * * * /opt/mcp-env/bin/python3 /opt/ssh-mcp/idle_culler.py`, 기존 xcelium-mcp 항목 보존) |
| DoD-4 | 문서화(Design/Do 문서, README 갱신 필요 시) | ✅ | `skill/ssh-mcp/SKILL.md`의 "30분 idle timeout" 서술을 EOF+keepalive+culler 모델로 갱신 |

### Quality Criteria Status (Plan §4.2)

| # | Criteria | Status | Evidence |
|---|---|:---:|---|
| QC-1 | 기존 `ssh-mcp-process-lifecycle` 테스트(SC-2~SC-8) 회귀 없음 | ✅ | job reap/kill/atexit 관련 테스트 전부 pass, 신규 `test_call_tool_triggers_job_sweep` 포함 |
| QC-2 | `idle_watchdog` 제거로 인한 미사용 import/전역 변수 없음(lint 클린) | ✅ | 제거 대상 심볼 grep 결과 코드 참조 0건 |
| QC-3 | cloud0 L3 실측: 정상 세션 생존 + 죽은 연결 조기 정리 + 오탐 없음 3가지 모두 확인 | ✅ **완료(2026-07-08)** | §2.6 L3 — 5/5 PASS |

**Success Rate**: DoD 4/4 충족, QC 3/3 충족. Plan §4.1/§4.2 전 항목 완료.

### Decision Record Verification

| Source | Decision | Followed? | Deviation |
|--------|----------|:---:|-----------|
| [Plan] | 프로세스 종료 트리거: EOF 전용 + 외부 culler (B안) | ✅ | `main()`이 `stdio_server()` EOF 경로만 남음 |
| [Design] | Architecture Option A(orphan+age 휴리스틱) 채택, TCP 정밀 확인(Option B)은 root 제약으로 배제 | ✅ | `idle_culler.py`가 `has_established_tcp()` 없이 `is_orphaned`+`process_age_seconds`만 사용 |
| [Design §3.1] | `_has_running_job()`은 job_sweep 등에서 계속 사용되므로 함수 자체 유지 | ✅ (유지됨) / ⚠️ (근거 서술 오차) | 함수는 살아있고 3개 테스트가 호출하지만, **`job_sweep()` 자체는 `_has_running_job()`을 호출하지 않는다** — 문서상 "job_sweep 등에서 사용"이라는 근거 서술이 부정확(Gap 1 참고). 요구사항(유지·정상동작)은 충족 |
| (승인된 이탈, Design 미명시) | `job_sweep()` 호출 시점을 watchdog 루프 → `call_tool()` 진입부로 이동 | ✅ | 이전 대화에서 사용자 승인 — `ssh_agent.py:304`, 회귀 테스트 포함 |

---

## 1. Analysis Overview

### 1.1 Analysis Purpose

Design(§3/§4.1/§6.2/§11.1)과 실제 구현(`ssh_agent.py`, `idle_culler.py`, `tests/`)이 일치하는지 정적으로 대조하고, module-1+module-2 범위 내 Gap을 확인한다. module-3(cloud0 배포)은 범위 밖.

### 1.2 Analysis Scope

- **Design Document**: `docs/02-design/features/ssh-mcp-idle-culler.design.md` (v0.1)
- **Implementation Path**: `ssh_agent.py`, `idle_culler.py`(신규), `tests/test_lifecycle.py`, `tests/test_idle_culler.py`(신규), `skill/ssh-mcp/SKILL.md`
- **Analysis Date**: 2026-07-08
- **Analysis Mode**: 정적 분석(gap-detector) + 로컬 pytest 46개 실행 결과(37 pass, 9 skip — POSIX 전용 항목, Windows 환경이므로 정상). cloud0 L3는 module-3 이후로 연기.

---

## 2. Gap Analysis (Design vs Implementation)

> 웹앱 axis(API route/DB/UI)는 해당 없음 — 선행 사이클과 동일하게 Structural/Functional/Contract로 재해석. Contract 축은 이번 사이클에서 MCP tool 시그니처(`list_tools()`)를 전혀 건드리지 않으므로 자명하게 100%.

### 2.1 Structural Match — 100% (in-scope)

| Design 요소 | 구현 위치 | 상태 |
|---|---|:---:|
| `idle_culler.py`의 §4.1 함수 7종(`is_ssh_agent_argv`, `find_ssh_agent_pids`, `has_live_children`, `is_orphaned`, `process_age_seconds`, `_cull_if_eligible`, `main`) | `idle_culler.py` 전역 | ✅ |
| §3.2 신규 상수 4종(`AGENT_CMDLINE_MARKER`, `IDLE_THRESHOLD_SEC`, `KILL_GRACE_SEC`, `ORPHAN_PPID`) | `idle_culler.py:19-22` | ✅ |
| §3.1 제거 대상 4종(`idle_watchdog`/`IDLE_TIMEOUT_SEC`/`WATCHDOG_INTERVAL_SEC`/`_last_activity`) | (부재 확인) | ✅ |
| `main()` 단순화 — watchdog task 생성 제거, `stdio_server()` 경로만 유지 | `ssh_agent.py` `main()` | ✅ |
| §11.1 파일 구조 — `idle_culler.py`, `tests/test_idle_culler.py` 신규 | 존재 | ✅ |
| §11.1 `deploy/crontab.example` | **부재** | ⏳ 의도적 연기(module-3), Gap 아님 |

설계에 없던 추가: 없음(gap-detector 확인, §4.1 pseudocode에 충실).

### 2.2 In-Memory / Process State Model (Design §3, DB 대체)

| Design | 구현 | 상태 |
|---|---|:---:|
| `idle_culler.py`는 stateless — 매 실행마다 `/proc` 새로 스캔 | `main()`이 매 호출 `find_ssh_agent_pids()` 재실행, 전역 캐시 없음 | ✅ |
| `ssh_agent.py` 쪽 `_jobs` in-memory 상태는 이번 사이클에서 미변경 | `_jobs` 구조체/필드 불변 확인 | ✅ |

### 2.3 해당 없음 섹션 (Design 원본과 동일하게 N/A)

REST API, DB Schema, UI/UX, Clean Architecture 레이어 — Design과 마찬가지로 N/A.

### 2.4 Functional Depth — 98%

Placeholder/TODO/stub 없음(gap-detector 확인). §4.1 pseudocode의 설계된 동작을 모두 수행:

| Design 동작 | 구현 | 상태 |
|---|---|:---:|
| ppid/starttime 파싱 시 `rpartition(")")`로 comm 내부 공백/괄호 안전 처리 | `parse_stat_ppid`, `parse_stat_starttime` | ✅ |
| cmdline 오매칭 방지 — `argv[-1]` basename 정확 비교(substring 아님) | `is_ssh_agent_argv` | ✅, 테스트로 grep-substring 오매칭 방지 확인 |
| 실행 중 job(살아있는 자식) 있으면 항상 보존 | `_cull_if_eligible`의 `has_live_children` 가드 | ✅ |
| 고아(ppid==1)면 즉시, 아니면 age 임계값 초과 시에만 정리 | `_cull_if_eligible` 게이팅 로직 | ✅ pseudocode와 정확히 일치 |
| SIGTERM → `KILL_GRACE_SEC`(5초) 대기 → SIGKILL 승격 | `_cull_if_eligible` kill 시퀀스 | ✅ |
| Windows 가드 + 스캔-킬 레이스 방어(`try/except (OSError, ValueError, IndexError): continue`) | `main()` | ✅ |
| `job_sweep()`을 `call_tool()` 진입부로 이동(승인된 이탈, §Decision Record 참고) | `ssh_agent.py:304` | ✅ |

**−2% 사유**: Gap 1, Gap 2(아래) — 둘 다 코스메틱, 기능적 결함 아님.

### 2.5 Contract — 100%

이번 사이클은 MCP tool(`list_tools()`)을 전혀 건드리지 않는다 — `idle_culler.py`는 MCP tool이 아닌 독립 cron 스크립트(Design §4 명시). 기존 7개 tool 시그니처 불변, 응답 포맷 불변.

### 2.6 Runtime Verification — Design §8 vs tests/

#### L1 (§8.2, 4개 시나리오) — 4/4 커버

| # | 시나리오 | 테스트 | 상태 |
|---|---|---|:---:|
| 1 | `is_ssh_agent_argv` 정상 매칭 | `test_is_ssh_agent_argv_true_for_real_invocation` | ✅ |
| 2 | `is_ssh_agent_argv` grep 오매칭 방지 | `test_is_ssh_agent_argv_false_for_grep_substring_match` | ✅ |
| 3 | ppid 파싱(comm 공백/괄호 포함) | `test_parse_stat_ppid_handles_comm_with_spaces_and_parens` | ✅ |
| 4 | `process_age_seconds` mock 계산 | `test_process_age_seconds_computes_elapsed_from_mocked_proc` | ✅ |

**L1 Score**: 4/4 = 100% (+2 bonus: `test_is_ssh_agent_argv_false_for_empty_argv`, `test_parse_stat_ppid_nonzero_ppid`)

#### L2 (§8.3, 3개 시나리오) — 3/3 커버 + 6개 추가

| # | 시나리오 | 테스트 | 상태 |
|---|---|---|:---:|
| 1 | `find_ssh_agent_pids` 정확 매칭 | `test_find_ssh_agent_pids_matches_only_real_name` | ✅ (POSIX, Windows skip) |
| 2 | `has_live_children` True→False | `test_has_live_children_true_then_false_after_child_exits` | ✅ (POSIX, Windows skip) |
| 3 | `is_orphaned` False(정상 부모) | `test_is_orphaned_false_for_normal_child` | ✅ (POSIX, Windows skip) |

추가 커버: `_cull_if_eligible` 게이팅 3종(job 보존/age 미달 스킵/고아 즉시킬/age 백스톱킬) + `main()` 3종(win32 가드, pid 스캔, 레이스 방어) — 설계 계획보다 초과.

**L2 Score**: 3/3 = 100% (+6 bonus, 로컬 실행 시 POSIX만 — 이번 세션은 Windows이므로 9개 skip, CI/cloud0에서 실행 시 활성화)

#### L3 (§8.4, 5개 시나리오) — 5/5 PASS (2026-07-08, cloud0 실측)

| # | 시나리오 | 방법 | 결과 |
|---|---|---|:---:|
| 1 | 고아 프로세스 즉시 정리 | 픽스처 `ssh_agent.py`(sleep 스크립트)를 `nohup ... &`로 spawn 후 부모 bash -c 종료로 자연 고아화(`ppid=1` 실측 확인) → `_cull_if_eligible(pid)` 직접 호출 | ✅ PASS — SIGTERM 즉시 종료, 로그 `[idle-culler] orphaned pid=27158 — killed` |
| 2 | 정상 세션 보존 | **현재 라이브 세션 자체**(`pid=24099`, 이 Check를 수행 중인 실제 `ssh_agent.py`)에 `_cull_if_eligible()` 직접 호출 | ✅ PASS — `is_orphaned=False`, `age=1876s`(≪6h) → no-op, 세션 계속 생존(이 문서 자체가 그 증거) |
| 3 | age 백스톱 | `ssh_bg_run`으로 살아있는 부모(job wrapper) 밑에 픽스처 spawn(비고아 상태 유지) → `IDLE_THRESHOLD_SEC=0`으로 일시 오버라이드 → `_cull_if_eligible()` | ✅ PASS — 고아 아님에도 age 초과로 정리, 로그 `[idle-culler] age-backstop age=10s pid=27283 — killed` |
| 4 | 실행 중 job 보존 | 픽스처가 자체적으로 `subprocess.Popen(["sleep","300"])`로 자식을 낳게 한 뒤 고아화(`ppid=1` **and** `has_live_children=True` 동시 충족 — 최악의 케이스) → `_cull_if_eligible()` | ✅ PASS — 고아 상태임에도 살아있는 자식이 있어 정리 건너뜀, 프로세스 생존 확인 |
| 5 | cmdline 오매칭 방지 | `ssh_bg_run`으로 `echo ssh_agent.py dummy && sleep 120` 데코이(cmdline에 문자열만 포함, 실제 파일 아님) spawn 후 `find_ssh_agent_pids()` 결과 확인 | ✅ PASS — 데코이 pid가 매칭 목록에 없음 |

**방법론 비고**: 시나리오 1/3/4는 `main()`(전체 `/proc` 스캔)이 아니라 `_cull_if_eligible(특정pid)`를 직접 호출해 라이브 세션(24099)이 우발적으로 스캔·정리되지 않도록 격리했다. 시나리오 2는 반대로 라이브 세션 자체를 안전하게(정리 조건 미충족 확인 후) 대상으로 직접 검증했다. 픽스처 생성 시 `ssh_run`(동기, `capture_output=True`)으로 백그라운드 job을 얼레벌레 띄우면 자식이 stdout/stderr pipe fd를 물려받아 부모가 종료돼도 pipe가 닫히지 않아 타임아웃되는 함정을 실측으로 발견 — `ssh_bg_run`(Popen 기반, 별도 outfile 리다이렉트) 또는 `nohup ... > file 2>&1 &`(명시적 리다이렉트)로 우회했다.

**부수 발견(Gap 3, 코드 변경 불필요)**: `find_ssh_agent_pids()`가 실제 `ssh_agent.py` 프로세스(`pid=24099`)뿐 아니라 SSH가 `ForceCommand`로 띄우는 `tcsh -c "/opt/mcp-env/bin/python3 /opt/ssh-mcp/ssh_agent.py"` 로그인 셸 래퍼(`pid=24050`)도 함께 매칭한다 — `argv[-1]`이 통짜 문자열이라 그 안의 마지막 `/`가 우연히 `ssh_agent.py` 앞에 오기 때문(`Path(...).name`이 전체 문자열을 하나의 경로로 파싱). 실측 확인 결과 `has_live_children(24050)`이 항상 `True`(실제 `ssh_agent.py`가 그 자식이므로)라 이 래퍼는 job-보존 가드로 항상 보호되며, 실사용 리스크는 없다 — Design 문서화 가치만 있는 관찰.

**L3 Score**: 5/5 = 100% — Plan §4.1 DoD-3, §4.2 QC-3 완전 충족

### 2.7 Match Rate Summary

```
┌─────────────────────────────────────────────┐
│  Structural Match Rate:   100%                │
│  Functional Match Rate:   98%                 │
│  Contract Match Rate:     100%                │
│  L1 Runtime (proxy):      100%                │
│  L2 Runtime (proxy):      100% (cloud0: 활성) │
│  L3 Runtime (cloud0 실측):100% (5/5 PASS)      │
│  ─────────────────────────────────────────── │
│  Overall Match Rate:      99.2%               │
├─────────────────────────────────────────────┤
│  ✅ Match:  전 항목(module-1+2+3, L3 포함)     │
│  ❌ Not implemented: 0건                       │
└─────────────────────────────────────────────┘
```

**Overall Match Rate (Design §8 Match Rate Formula, runtime 실행됨 준용)**:
`(Structural×0.15)+(Functional×0.25)+(Contract×0.25)+(Runtime×0.35)` = `(100×0.15)+(98×0.25)+(100×0.25)+(100×0.35)` = **99.5%**
(참고: static-only 공식 `(100×0.2)+(98×0.4)+(100×0.4)` = 99.2% — 둘 다 90% 임계값 상회, 차이는 반올림/가중치 수준)

> **Plan §4.1 Definition of Done 전 항목 충족**(2026-07-08) — module-3(cloud0 배포 + crontab 등록) 및 L3 스모크 5건까지 완료. Functional 98%의 −2%는 Gap 1/2(둘 다 Minor, 코드 변경 불필요)에서 기인 — Critical/Important 0건.

---

## Gaps

| # | 심각도 | 내용 | 근거 | 신뢰도 |
|---|:---:|---|---|:---:|
| 1 | Minor(문서) | Design §3.1 "`_has_running_job()`은 `job_sweep()` 등 다른 곳에서 계속 사용되므로 함수 자체는 유지한다"는 서술이 부정확 — `job_sweep()`은 `_has_running_job()`을 호출하지 않는다(테스트에서만 3회 호출) | `ssh_agent.py:145-176`(job_sweep 정의부에 `_has_running_job` 호출 없음) | 95% |
| 2 | Minor(코스메틱) | `AGENT_CMDLINE_MARKER` 상수가 정의됐지만 실제 매칭은 `is_ssh_agent_argv`의 `argv[-1]` basename 비교로 이루어져 미사용 — Design §4.1 pseudocode 자체도 동일하게 미사용으로 선언되어 있어 Design을 충실히 따른 결과 | `idle_culler.py:19` | 90% |
| 3 | Minor(정보성, L3 실측 발견) | `find_ssh_agent_pids()`가 실제 `ssh_agent.py` 프로세스뿐 아니라 SSH `ForceCommand`가 띄우는 `tcsh -c "...ssh_agent.py"` 로그인 셸 래퍼도 함께 매칭(`argv[-1]` 문자열 안의 우연한 `/` 때문). `has_live_children()` 가드가 항상 보호하므로 실사용 리스크 없음 — §2.6 L3 방법론 비고 참고 | cloud0 실측(`pid=24050`) | 95% |

Critical/Important 등급 Gap 없음(신뢰도 80% 이상 기준). 셋 다 코드 변경 불필요.

---

## Checkpoint 5 — Review Decision (2026-07-08, 사용자 결정: "그대로 진행")

세 Gap 모두 Minor이고 코드 변경 불필요 — Gap 2는 Design 자체가 의도한 대로 구현된 것, Gap 3은 실사용 리스크 없는 정보성 관찰. Gap 1(문서 서술)만 다음 문서 갱신 시 정정 대상으로 남겨둠. 사용자 승인에 따라 module-3(cloud0 배포)로 진행, 완료.

---

## 3~7. Code Quality / Performance / Test Coverage 상세 / Clean Architecture / Convention

N/A 또는 위 §2에 통합 — 선행 사이클(`ssh-mcp-process-lifecycle`)과 동일하게 단일 파일 경량 프로젝트로 별도 레이어/네이밍 컨벤션 문서 없음. 하드코딩 시크릿·보안 이슈 없음(gap-detector 확인) — `os.kill`은 자기 uid 프로세스만 대상 가능(Design §7 명시).

---

## 8. Overall Score

```
┌─────────────────────────────────────────────┐
│  Overall Match Rate: 99.2~99.5%               │
├─────────────────────────────────────────────┤
│  Structural:  100%                            │
│  Functional:  98%                             │
│  Contract:    100%                            │
│  Test Coverage(L1+L2 proxy): 4/4 + 3/3        │
│  L3(cloud0 실측):            5/5              │
└─────────────────────────────────────────────┘
```

module-1+module-2+module-3 전 범위 완료, cloud0 배포 및 L3 실측까지 완료. Plan Definition of Done 전 항목 충족 — `/pdca report ssh-mcp-idle-culler` 진행 가능.

---

## 9. Recommended Actions

### 9.1 이번 세션 범위 — 완료

| 항목 | 파일 | 결과 |
|---|---|---|
| module-1: ssh_agent.py 정리 | `ssh_agent.py` | ✅ 완료, 37 tests pass |
| module-2: idle_culler.py + 테스트 | `idle_culler.py`, `tests/test_idle_culler.py` | ✅ 완료, L1/L2 전 항목 커버 |
| SKILL.md 갱신 | `skill/ssh-mcp/SKILL.md` | ✅ 완료 |

### 9.2 module-3 — 완료 (cloud0, 2026-07-08)

- [x] `deploy/crontab.example` 작성
- [x] cloud0에 `idle_culler.py` 배포(`git push` → cloud0 `git pull`, `270b001`) + crontab 등록(기존 xcelium-mcp 항목과 병합)
- [x] Design §8.4 L3 스모크 5건 실행 — 전부 PASS(§2.6)
- [x] `ORPHAN_PPID=1` cloud0 실측으로 재확인 — 고아 프로세스가 실제로 `ppid=1`로 재부모됨을 시나리오 1/4에서 직접 확인, 가정값 그대로 유효

### 9.3 선택 사항 — Gap 1 문서 정정

- [ ] Design §3.1의 `_has_running_job()` 유지 근거 서술을 "job_sweep 등에서 사용"에서 "테스트 및 향후 확장을 위해 함수 자체 유지"로 정정(선택, 코드 변경 없음)

---

## 10. Design Document Updates Needed

- [ ] (선택) Design §3.1 — Gap 1 서술 정정

---

## 11. Next Steps

- [x] Checkpoint 5 결정 — "그대로 진행"(Gap 전부 코드 변경 불필요)
- [x] module-3(cloud0 배포) 완료 — 사용자 승인 후 진행
- [x] L3 스모크 5건 완료, 최종 Match Rate 확정(99.2~99.5%)
- [ ] `/pdca report ssh-mcp-idle-culler` 진행 가능

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.2 | 2026-07-08 | module-3(cloud0 배포 + L3 스모크 5건) 완료 반영. `git push`→cloud0 `git pull`(270b001)→crontab 병합 등록, cloud0 pytest 46/46 pass. L3 5/5 PASS(고아/정상세션/age백스톱/job보존/cmdline오매칭 전부 실측 확인) — 라이브 세션 무손상. Gap 3(정보성) 신규 발견: tcsh 로그인셸 래퍼도 cmdline 매칭되나 has_live_children 가드로 항상 보호됨, 코드 변경 불필요. Overall Match Rate 99.2~99.5%로 확정, Plan Definition of Done 전 항목 충족. | hoseung.lee |
| 0.1 | 2026-07-08 | 초안 — gap-detector 정적 분석 결과 반영(module-1+2 범위). Static-only Overall 99.2%, Critical/Important 0건, Minor 2건(문서/코스메틱). L3(cloud0)는 module-3 연기로 범위 밖 — Plan DoD 미완료 상태로 명시. | hoseung.lee |
