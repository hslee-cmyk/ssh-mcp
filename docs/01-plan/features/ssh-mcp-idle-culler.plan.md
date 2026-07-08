---
template: plan
version: 1.3
description: PDCA Plan phase document template with Context Anchor and Architecture considerations
variables:
  - feature: ssh-mcp-idle-culler
  - date: 2026-07-08
  - author: hoseung.lee
  - project: ssh-mcp
  - version: 0.1
---

# ssh-mcp-idle-culler Planning Document

> **Summary**: `idle_watchdog`의 고정 30분 타이머(FR-01)를 EOF 기반 정상 종료 + SSH keepalive + 외부 idle-culler 안전망으로 대체해, "활동 없음"과 "연결 끊김"을 혼동하지 않도록 한다.
>
> **Project**: ssh-mcp
> **Version**: 0.1
> **Author**: hoseung.lee
> **Date**: 2026-07-08
> **Status**: Draft

---

## Executive Summary

| Perspective | Content |
|-------------|---------|
| **Problem** | `idle_watchdog`(FR-01, 고정 30분 타이머)이 "앱 레벨 활동 없음"을 "연결 끊김"과 동일시해, 사용자가 조용히 코드를 읽거나 생각만 해도 30분 뒤 프로세스가 자체 종료되어 매번 `/mcp` 재연결이 필요하다. |
| **Solution** | 프로세스 종료 기준을 `stdin EOF`(연결이 실제로 끊겼을 때만 발생, 기존 `stdio_server()` 정상 종료 경로 재사용)로 바꾸고, 죽은 연결 자체는 클라이언트 SSH `ServerAliveInterval`/`ServerAliveCountMax` keepalive로 조기 감지시켜 EOF를 빠르게 유도한다. 방치된 프로세스에 대한 안전망은 앱 내부 타이머 대신 xcelium-mcp에서 이미 검증된 `/proc` 기반 외부 idle-culler를 cloud0 cron으로 이식해 담당한다. |
| **Function/UX Effect** | 연결이 살아있는 한 idle 시간과 무관하게 재연결이 필요 없어지고, 진짜 끊긴 연결은 오히려 지금(30분)보다 훨씬 빠르게(~90초) 정리된다. |
| **Core Value** | "활동 감지"가 아니라 "연결 생사"를 기준으로 프로세스 lifecycle을 관리해, 리소스 누적 방지라는 FR-01의 원래 목적은 유지하면서 정상 사용 세션의 불필요한 재연결을 제거한다. |

---

## Context Anchor

> Auto-generated from Executive Summary. Propagated to Design/Do documents for context continuity.

| Key | Value |
|-----|-------|
| **WHY** | idle_watchdog이 활동 없음=연결 끊김으로 잘못 가정해, 정상적으로 조용한 세션까지 강제 종료시켜 재연결 마찰을 유발함 |
| **WHO** | ssh-mcp를 통해 cloud0에 접속해 장시간 작업(코드 리뷰, 사고, 긴 응답 대기 등)하는 Claude Code 사용자 |
| **RISK** | EOF 기반 종료로 전환 시 keepalive 설정이 없는 클라이언트 환경에서는 죽은 연결이 감지되지 않고 방치될 수 있음 — 외부 idle-culler가 최종 안전망 |
| **SUCCESS** | (1) 정상 세션은 idle 시간과 무관하게 재연결 불필요 (2) 죽은 연결은 ~90초 내 정리 (3) 방치 프로세스는 idle-culler로 무한 누적 방지 |
| **SCOPE** | FR-01 재정의(고정 타이머 제거, idle_watchdog 완전 제거) + cloud0 전용 idle-culler cron 신규 배포. SC-2~SC-8(job reap, `ssh_bg_kill` 등)은 무영향 |

---

## 1. Overview

### 1.1 Purpose

`ssh-mcp-process-lifecycle` 사이클(§2.2.1)에서 검증 완료된 `idle_watchdog`(FR-01)이 "설계대로 정확히 동작"하면서도, 그 설계 기준 자체가 실사용과 맞지 않는 문제를 해결한다. 목표는 두 가지 서로 다른 상황 — "연결은 살아있는데 조용한 세션"과 "연결이 실제로 끊긴 세션" — 을 구분해서 처리하는 것이다.

### 1.2 Background

2026-07-08 venezia-fpga 세션에서 "ssh-mcp가 일정 시간 후 자꾸 끊겨 매번 재연결해야 한다"는 실사용 불만이 접수되었다. 근본원인 분석 결과 `idle_watchdog`은 앱 레벨 활동 타이머(`_last_activity`)만 보고 실제 SSH 연결(stdin 파이프) 생사는 전혀 확인하지 않는 것으로 확인됨(`ssh-mcp-process-lifecycle.design.md` §2.2.2 참고). 같은 프로젝트의 `xcelium-mcp-server-process-lifecycle` 사이클은 이미 "SSH keepalive로 죽은 연결 감지 + `/proc` 기반 외부 idle-culler" 패턴을 cloud0에서 실전 검증해뒀다 — 이번 사이클은 그 패턴을 ssh-mcp에 이식한다.

### 1.3 Related Documents

- Requirements: `docs/02-design/features/ssh-mcp-process-lifecycle.design.md` §2.2.1(기존 idle_watchdog 검증 기록), §2.2.2(이번 제안의 최초 발의)
- References: xcelium-mcp `idle_culler.py`(참조 구현, 이식 대상) — 경로는 Design 단계에서 실제 파일 확인 후 명시

---

## 2. Scope

### 2.1 In Scope

- [ ] `ssh_agent.py`에서 `idle_watchdog` 코루틴, `IDLE_TIMEOUT_SEC`, `WATCHDOG_INTERVAL_SEC`, `_last_activity` 전역 및 관련 갱신 코드 완전 제거
- [ ] `main()`의 종료 경로를 `stdio_server()`의 정상 EOF 종료 경로 하나로 단순화(watchdog task 생성/취소 로직 제거)
- [ ] xcelium-mcp `idle_culler.py`를 ssh-mcp용으로 이식한 신규 cron 스크립트 작성 (`/proc`에서 ESTABLISHED TCP 없이 오래된 `ssh_agent.py` 프로세스 탐지·정리)
- [ ] cloud0에 idle-culler cron 등록 (배포 절차 문서화)
- [ ] Design 문서에 클라이언트 측 `ServerAliveInterval=30`/`ServerAliveCountMax=3` 설정을 전제조건으로 명시(이미 로컬 적용 완료 — 이 저장소 범위 밖이지만 근거로 문서화)

### 2.2 Out of Scope

- `ssh_bg_run`/`ssh_bg_poll`/`ssh_bg_kill`/job_sweep 등 job lifecycle 로직 변경 (SC-2~SC-8, 기존 사이클에서 이미 100% 검증됨 — 무영향)
- 클라이언트 `~/.claude.json`의 SSH 설정 자체를 이 사이클의 산출물로 관리하는 것 (이미 적용 완료된 로컬 설정, 이 저장소 코드 변경 대상 아님)
- Windows 환경에서의 idle-culler 배포 (cloud0=Linux 전용, §2.1 결정)

---

## 3. Requirements

### 3.1 Functional Requirements

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| FR-01 (재정의) | `ssh_agent.py`는 `idle_watchdog` 없이 `stdin` EOF 시에만 정상 종료한다 (기존 `stdio_server()` 종료 경로 그대로 사용, 신규 구현 불필요) | High | Pending |
| FR-02 | 죽은 연결(네트워크 블랙홀)은 클라이언트 SSH keepalive(`ServerAliveInterval=30`, `ServerAliveCountMax=3`)로 ~90초 내 감지되어 로컬 ssh가 종료되고, 이것이 원격 sshd → `ssh_agent.py`에 EOF로 전파된다 | High | Pending (클라이언트 설정은 적용 완료, 코드 측 검증만 남음) |
| FR-03 | 방치된(오래되고 ESTABLISHED TCP 연결이 없는) `ssh_agent.py` 프로세스는 cloud0의 외부 idle-culler cron이 주기적으로 탐지해 정리한다 | High | Pending |
| FR-04 | idle-culler는 `/proc` 순수 관찰만 수행하며 `ssh_agent.py` 프로세스 내부에 신규 스레드/파일을 추가하지 않는다(xcelium-mcp 패턴 준수) | Medium | Pending |

### 3.2 Non-Functional Requirements

| Category | Criteria | Measurement Method |
|----------|----------|-------------------|
| Reliability | 정상 세션은 idle 시간과 무관하게 프로세스가 유지되어야 함 | cloud0 L3: 80초+ idle 후에도 프로세스 생존 확인 |
| Responsiveness | 죽은 연결은 기존(30분)보다 빠르게(~90초) 정리되어야 함 | cloud0 L3: 네트워크 차단 시나리오 재현 후 정리 시각 측정 |
| Safety | idle-culler가 정상 동작 중인 프로세스를 오탐으로 죽이지 않아야 함 | 정적 분석 + cloud0 L3: ESTABLISHED TCP 있는 프로세스는 절대 종료 안 됨을 확인 |

---

## 4. Success Criteria

### 4.1 Definition of Done

- [ ] `idle_watchdog`/`IDLE_TIMEOUT_SEC`/`WATCHDOG_INTERVAL_SEC`/`_last_activity` 코드 완전 제거, 기존 job_sweep(FR-02~FR-05, 별도 사이클)과의 결합 없이 동작
- [ ] idle-culler 스크립트 작성 및 단위 테스트(로컬, `/proc` mock)
- [ ] cloud0 cron 배포 완료
- [ ] 문서화(Design/Do 문서, README 갱신 필요 시)

### 4.2 Quality Criteria

- [ ] 기존 `ssh-mcp-process-lifecycle` 테스트(SC-2~SC-8 관련) 회귀 없음
- [ ] `idle_watchdog` 제거로 인한 미사용 import/전역 변수 없음(lint 클린)
- [ ] cloud0 L3 실측: 정상 세션 생존 + 죽은 연결 조기 정리 + 오탐 없음 3가지 모두 확인

---

## 5. Risks and Mitigation

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| keepalive 미설정 클라이언트에서는 죽은 연결이 EOF로 전환되지 않고 방치될 수 있음 | Medium | Low (현재 알려진 클라이언트는 이미 keepalive 적용됨) | idle-culler가 최종 안전망으로 남아 무한 누적은 방지됨 |
| idle-culler가 `/proc` 파싱 오류 등으로 정상 프로세스를 오탐 종료 | High | Low | ESTABLISHED TCP 존재 여부를 확인 조건에 반드시 포함, cloud0 L3에서 오탐 없음 직접 검증 |
| idle_watchdog 제거 시 `_has_running_job()`을 참조하던 다른 코드 경로에 영향 | Low | Low | `_has_running_job()`은 job_sweep 등 다른 곳에서도 쓰이므로 함수 자체는 유지, watchdog 호출부만 제거 — Do 단계에서 실제 참조처 재확인 |

---

## 6. Impact Analysis

### 6.1 Changed Resources

| Resource | Type | Change Description |
|----------|------|--------------------|
| `ssh_agent.py` — `idle_watchdog`, `IDLE_TIMEOUT_SEC`, `WATCHDOG_INTERVAL_SEC`, `_last_activity` | Python module-level code | 제거 |
| `ssh_agent.py` — `main()` | Python function | watchdog task 생성/관리 로직 제거, `stdio_server()` 종료 경로만 남김 |
| (신규) idle-culler cron 스크립트 | 신규 파일 (cloud0 배포) | xcelium-mcp `idle_culler.py` 이식 |

### 6.2 Current Consumers

| Resource | Operation | Code Path | Impact |
|----------|-----------|-----------|--------|
| `idle_watchdog` | 호출 | `main()`의 `asyncio.create_task(idle_watchdog())` | Breaking(의도적 제거) — `main()` 수정 필요 |
| `_last_activity` | 갱신 | 각 tool 핸들러 진입부(활동 기록용) | Needs verification — 갱신 코드도 함께 제거해야 dead code 안 남음 |
| `_has_running_job()` | 참조 | `idle_watchdog` 내부 + job_sweep 등 | Needs verification — watchdog 제거 후에도 다른 참조처는 그대로 유지되어야 함 |

### 6.3 Verification

- [ ] `idle_watchdog` 제거 후 `_last_activity` 갱신 코드까지 모두 제거되어 dead code 없음
- [ ] `_has_running_job()`은 job_sweep 등 다른 경로에서 계속 정상 동작
- [ ] 기존 SC-2~SC-8(job reap/kill) 관련 테스트 전부 통과(회귀 없음)

---

## 7. Architecture Considerations

### 7.1 Project Level Selection

> 본 프로젝트(ssh-mcp)는 Next.js/웹앱이 아닌 Python asyncio 기반 MCP 서버 + 원격(cloud0) 배포 스크립트다. 템플릿의 Starter/Dynamic/Enterprise 웹 레벨 구분은 해당 없음(N/A) — 아래는 이 프로젝트 실제 구조에 맞게 대체.

| 구성 요소 | 위치 | 비고 |
|-----------|------|------|
| MCP 서버 본체 | `ssh_agent.py` (단일 파일) | 기존 구조 유지 |
| 신규 idle-culler | cloud0 전용 cron 스크립트 (신규 파일) | xcelium-mcp 이식, 저장소 내 위치는 Design 단계에서 확정 |

### 7.2 Key Architectural Decisions

| Decision | Options | Selected | Rationale |
|----------|---------|----------|-----------|
| 프로세스 종료 트리거 | (A) 앱 내부 활동 타이머 유지 (B) EOF 전용 + 외부 culler | **(B)** | 활동 없음≠연결 끊김 문제의 근본 해결; xcelium-mcp에서 이미 검증된 패턴 재사용 |
| 죽은 연결 탐지 위치 | (A) 서버 측 자체 탐지 (B) 클라이언트 SSH keepalive | **(B)** | 서버가 알 수 없는 클라이언트 측 네트워크 단절을 클라이언트 keepalive가 가장 빠르게 감지 |
| 방치 프로세스 안전망 | (A) 앱 자체 타이머(자기 참조라 반증 곤란) (B) 외부 `/proc` 관찰 cron | **(B)** | 외부에서 독립적으로 판정해야 신뢰 가능, xcelium-mcp `idle_culler.py` 재사용으로 이식 비용 낮음 |

### 7.3 Clean Architecture Approach

```
ssh-mcp/
├── ssh_agent.py            # idle_watchdog 제거, EOF 종료 경로만 유지
└── (신규) deploy 또는 scripts/
    └── idle_culler.py      # cloud0 cron 전용, xcelium-mcp 이식본
```

---

## 8. Convention Prerequisites

### 8.1 Existing Project Conventions

- [x] `CLAUDE.md`에 코딩 컨벤션 섹션 있음(상위 fpga 프로젝트 공통 CLAUDE.md — 이 저장소 자체는 별도 CLAUDE.md 없음)
- [ ] `docs/01-plan/conventions.md` — N/A (웹앱 파이프라인 전용, 이 프로젝트 미해당)
- [x] 기존 사이클(`ssh-mcp-process-lifecycle`)의 Design 문서 컨벤션(§ 번호 체계, Design Ref 주석 등)을 그대로 따름

### 8.2 Conventions to Define/Verify

| Category | Current State | To Define | Priority |
|----------|---------------|-----------|:--------:|
| Design Ref 주석 | 기존 사이클에 이미 사용 중 | `// Design Ref: §{section}` 패턴을 신규 코드에도 동일 적용 | High |
| cron 배포 절차 | 없음(신규) | cloud0 cron 등록 스크립트/절차 문서화 | High |

### 8.3 Environment Variables Needed

| Variable | Purpose | Scope | To Be Created |
|----------|---------|-------|:-------------:|
| `SSH_MCP_IDLE_TIMEOUT_SEC` | (기존, 제거 대상) idle_watchdog 타임아웃 | Server | 제거 |
| (신규, 이름 TBD) idle-culler 판정 임계값 | 방치 프로세스로 간주할 최소 경과 시간 | cloud0 cron | ☐ |

### 8.4 Pipeline Integration

N/A — 이 프로젝트는 9-phase 웹앱 Development Pipeline을 사용하지 않음(Python MCP 서버, 별도 PDCA 사이클로 관리).

---

## 9. Next Steps

1. [ ] Design 문서 작성 (`ssh-mcp-idle-culler.design.md`) — xcelium-mcp `idle_culler.py` 실제 소스 확인 후 이식 설계 구체화
2. [ ] `idle_watchdog` 제거 범위와 `_has_running_job()` 등 공유 헬퍼 영향 범위 재확인
3. [ ] cloud0 cron 배포 절차 확정

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-07-08 | Initial draft — `ssh-mcp-process-lifecycle.design.md` §2.2.2 후속 제안을 정식 Plan으로 승격 | hoseung.lee |
