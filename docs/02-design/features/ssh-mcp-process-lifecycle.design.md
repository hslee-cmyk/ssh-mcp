# ssh-mcp-process-lifecycle Design Document

> **Summary**: `ssh_agent.py`에 idle self-exit(옵션 A) + 백그라운드 job reaper를 추가해 프로세스/좀비 누적을 방지한다.
>
> **Project**: ssh-mcp (`Todoc/fpga/ssh-mcp/`)
> **Version**: N/A (단일 파일, 버전 관리 없음)
> **Author**: hoseung.lee
> **Date**: 2026-07-07
> **Status**: Draft
> **Planning Doc**: [ssh-mcp-process-lifecycle.plan.md](../../01-plan/features/ssh-mcp-process-lifecycle.plan.md)

### Pipeline References

N/A — 이 프로젝트는 웹앱 파이프라인(Phase 1~4 스키마/컨벤션/목업/API)을 사용하지 않는 단일 Python MCP stdio 서버.

---

## Context Anchor

> Plan 문서에서 그대로 복사.

| Key | Value |
|-----|-------|
| **WHY** | `ssh_agent.py`가 xcelium-mcp와 동일한 콜드 spawn·무정리 구조이면서, 사용자 전역 설정이라 사용 빈도가 더 높고, `ssh_bg_run`은 임의 명령을 배경 실행하므로 방치 시 위험이 더 큼 |
| **WHO** | ssh-mcp를 사용하는 hoseung.lee 계정의 모든 Claude Code 세션(프로젝트 무관, 전역 설정) |
| **RISK** | 프로세스/좀비 누적으로 cloud0 리소스(메모리·PID) 고갈. 특히 `ssh_bg_run` 좀비는 사용자가 시작한 임의 백그라운드 작업이 방치될 수 있어 "무슨 작업이 아직 돌고 있는지 아무도 모르는" 상태로 이어질 수 있음 |
| **SUCCESS** | (1) 반복 연결/해제해도 cloud0 상주 ssh-mcp 프로세스 수가 상한 내 유지, (2) `ssh_bg_run` job이 `ssh_bg_poll` 없이 방치되거나 부모가 죽어도 좀비/고아로 남지 않음 |
| **SCOPE** | F-A'(본 프로세스 lifecycle, 옵션 A 선택) + F-B'(`ssh_bg_run` 백그라운드 job 명시적 reap) |

---

## 1. Overview

### 1.1 Design Goals

- `ssh_agent.py` 프로세스가 유휴 상태(마지막 tool call 이후 일정 시간 경과)에서 스스로 종료해 무한 누적을 방지한다(FR-01).
- `ssh_bg_run`으로 시작한 백그라운드 프로세스가 `ssh_bg_poll` 호출 여부와 무관하게 OS 레벨에서 주기적으로 reap되어 좀비로 남지 않는다(FR-02).
- 부모 프로세스 종료 시 아직 실행 중인 job을 명시적으로 처리(SIGTERM + 로그)한다(FR-03).
- `/tmp/mcp_job_*.txt` 출력 파일이 무기한 누적되지 않는다(FR-04).
- 신규 `ssh_bg_kill(job_id)` tool로 실행 중인 백그라운드 job을 명시적으로 취소할 수 있다 — 부모 프로세스가 재시작(SSH 재연결로 인한 콜드 spawn 포함)되어도 PID 사이드카 파일로 취소 가능하다(FR-05).
- 기존 7개 tool의 시그니처·응답 포맷·outfile 기반 폴링 동작은 변경하지 않는다(회귀 없음).
- **보장(명시)**: idle self-exit(FR-01)과 24시간 정리(FR-04)는 모두 "완료된" 상태에만 적용된다 — `_jobs`에 아직 실행 중인(미완료) job이 하나라도 있으면 idle timeout이 지나도 프로세스는 종료되지 않고, 정리 대상에도 포함되지 않는다. 즉 며칠씩 걸리는 장기 시뮬레이션이라도 `ssh_bg_run`으로 시작한 뒤에는 자체 lifecycle 로직에 의해 중도에 강제 종료되지 않는다.

### 1.2 Design Principles

- **단일 파일 유지**: 233줄 규모 프로젝트에 별도 모듈/프로세스를 도입하지 않는다(Clean Architecture, 프리포크 수퍼바이저 등은 과함 — Plan §7.1 옵션 B 기각 사유와 동일).
- **관찰 기반 판단, 외부 개입 없음**: idle 판단은 프로세스 자신의 tool-call 활동 기록만으로 하고, cron 등 외부 프로세스가 공유 호스트의 다른 프로세스를 오탐·오살할 위험을 만들지 않는다(Plan §7.1 옵션 C 기각 사유와 동일).
- **실행 중인 job은 보존**: idle-exit 조건에 "미완료 job 없음"을 포함시켜, 사용자가 의도적으로 띄운 장기 백그라운드 작업이 유휴 타임아웃으로 중도 종료되지 않게 한다.

---

## 2. Architecture Options

### 2.0 Architecture Comparison (Checkpoint 3 — 완료)

| Criteria | Option A: Minimal (선택됨) | Option B: Clean | Option C: Pragmatic |
|----------|:-:|:-:|:-:|
| **Approach** | 자체 idle self-exit (asyncio 태스크) | xcelium-mcp 안C+ 재사용(프리포크 수퍼바이저 + `/proc` idle-culler) | cron 외부 reaper |
| **New Files** | 0 | 2~3 | 1 + crontab 등록 |
| **Modified Files** | 1 (`ssh_agent.py`) | 1 + `~/.claude.json` | 0~1 |
| **Complexity** | Low | High | Medium |
| **Maintainability** | Medium | High(과함) | Medium |
| **Effort** | Low (~1-2h) | High (~1일+) | Medium |
| **Risk** | Low — launch 설정 불변, 로컬 검증 가능 | Medium — 전역 launch 변경 리스크 | Medium — 공유 호스트 오탐 위험 |

**Selected**: **Option A** — **Rationale**: ssh-mcp는 하드웨어 연결 등 무거운 상태가 없는 경량 stdio 서버라, 프리포크 수퍼바이저(B)는 과설계다. cron 외부 reaper(C)는 `~/.claude.json`을 건드리지 않는 장점은 있으나 cloud0가 여러 프로젝트가 공유하는 호스트라 외부 프로세스 매칭 오탐 위험이 있고, "idle" 판단 근거(활동 로그)를 결국 `ssh_agent.py`에 추가해야 해 이점이 줄어든다. Option A는 launch 설정 변경 없이 파일 하나만 수정하면 되고, 로컬(cloud0 배포 없이)에서 idle-exit 동작을 즉시 검증할 수 있다.

> 사용자 확인(Checkpoint 3, 2026-07-07): 옵션 A 선택.

### 2.1 Component Diagram

```
┌────────────────────────────────────────────────────────────────┐
│ ssh_agent.py (single process, asyncio event loop)               │
│                                                                  │
│  ┌──────────────┐   updates    ┌────────────────────┐          │
│  │ call_tool()   │─────────────▶│ _last_activity (ts) │          │
│  │ (7개 tool 핸들러)│             └──────────┬─────────┘          │
│  └──────────────┘                          │ read                │
│                                             ▼                     │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ idle_watchdog() — asyncio background task (60s interval)   │  │
│  │  1. now - _last_activity > IDLE_TIMEOUT_SEC?                │  │
│  │  2. 모든 _jobs 미완료 없음(no running job)?                  │  │
│  │  3. 둘 다 참이면 → graceful shutdown (asyncio 루프 stop)      │  │
│  │  (매 tick마다) _jobs 전체 .poll() 스윕 → OS 레벨 reap         │  │
│  │  (매 tick마다) 완료 후 24h 지난 job → dict/outfile 정리        │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌──────────────┐        atexit         ┌────────────────────┐ │
│  │ _jobs: dict   │◀───────등록──────────│ ssh_bg_run           │ │
│  │ job_id→{proc, │                       └────────────────────┘ │
│  │  outfile,     │       종료 시 살아있는 proc에 SIGTERM         │
│  │  finished_at} │◀──────────────────────atexit_handler()       │
│  └──────────────┘                                                │
└────────────────────────────────────────────────────────────────┘
```

### 2.2 Process Lifecycle Flow

```
Claude Code 세션 시작 → ssh (client) → sshd → python3 ssh_agent.py (cold spawn)
    │
    ├─ tool call 발생마다 _last_activity 갱신
    │
    ├─ idle_watchdog 60s tick:
    │     - job 실행 중 → 유지
    │     - idle > 30min AND job 없음 → stdio 루프 정상 종료(exit 0)
    │
    └─ 세션 종료(client 연결 끊김) → stdin EOF → stdio_server 컨텍스트 정상 종료
          (네트워크 비정상 종료 시에도 idle_watchdog이 결국 회수)
```

### 2.3 Dependencies

| Component | Depends On | Purpose |
|-----------|-----------|---------|
| `idle_watchdog()` | `_last_activity`, `_jobs` (모듈 전역 상태) | idle 판단 + job 존재 여부 확인 |
| `call_tool()` | `_last_activity` 갱신 | 활동 시각 기록(모든 tool 공통) |
| `atexit_handler()` | `_jobs` | 프로세스 종료 시 잔여 job 정리(FR-03) |
| `job_sweep()` (watchdog 내부) | `_jobs`, `os.remove` | 좀비 reap(FR-02) + outfile/dict 정리(FR-04) |

---

## 3. In-Memory State Model

> DB/영속 저장소 없음 — 모든 상태는 프로세스 메모리에 존재하며 프로세스 종료 시 소멸(기존 설계와 동일, 변경 없음).

### 3.1 `_jobs` Entry (확장)

```python
# job_id -> JobEntry
JobEntry = {
    "process": subprocess.Popen,   # 기존
    "outfile": str,                # 기존, 예: /tmp/mcp_job_{job_id}.txt
    "finished_at": float | None,   # 신규 — watchdog이 .poll() 완료를 처음 감지한 monotonic 시각
    "pidfile": str,                # 신규(FR-05) — /tmp/mcp_job_{job_id}.pid, ssh_bg_run 시작 시 즉시 기록
}
```

**PID 사이드카 파일 (`{outfile 경로에서 확장자만 .pid}`, FR-05)**: `ssh_bg_run`이 `Popen` 생성 직후 `proc.pid`를 이 파일에 기록한다. `ssh_bg_kill`은 `_jobs`(in-memory)에서 먼저 조회하고, 없으면(부모 재시작으로 초기화된 경우) 이 파일에서 PID를 읽어 kill을 시도한다 — `ssh_bg_poll`의 outfile fallback(§6.1.1)과 동일한 "파일시스템을 진실의 원천으로 쓰는" 패턴. `job_sweep()`의 24h 정리(FR-04) 시 outfile과 함께 `.pid` 파일도 함께 삭제한다.

### 3.2 Module-level Globals (신규)

| Name | Type | Default | Purpose |
|------|------|---------|---------|
| `_last_activity` | `float` (monotonic) | 시작 시각 | 마지막 tool call 시각 |
| `IDLE_TIMEOUT_SEC` | `int` | `1800` (30분) | 환경변수 `SSH_MCP_IDLE_TIMEOUT_SEC`로 override 가능 |
| `WATCHDOG_INTERVAL_SEC` | `int` | `60` | idle/reap 점검 주기 |
| `JOB_RETENTION_SEC` | `int` | `86400` (24h) | 완료된 job의 dict/outfile 보존 기간(FR-04) |
| `JOB_OUTFILE_TMPL` | `str` | `"/tmp/mcp_job_{job_id}.txt"` | outfile 경로 포맷(신규 — `ssh_bg_run`/`ssh_bg_poll`/`ssh_bg_kill`/`job_sweep` 4곳이 공유) |
| `JOB_PIDFILE_TMPL` | `str` | `"/tmp/mcp_job_{job_id}.pid"` | PID 사이드카 파일 경로 포맷(신규, FR-05) |

### 3.3~3.4 DB Schema / BaaS Collection

N/A — 영속 저장소 없음.

---

## 4. API Specification

REST API 없음(MCP tool만 존재). 기존 7개 tool(`ssh_run`/`ssh_bg_run`/`ssh_bg_poll`/`file_read`/`file_write`/`file_ls`/`file_grep`) 시그니처는 **변경하지 않는다**(Plan §2.2). `ssh_bg_poll`의 "Unknown job" 응답 조건만 실질적으로 넓어짐(24h 경과 후 정리된 job도 동일 메시지 반환).

### 4.1 신규 MCP Tool: `ssh_bg_kill` (FR-05, 2026-07-07 추가)

**Input Schema**:
```json
{
  "type": "object",
  "properties": {
    "job_id": {"type": "string", "description": "Job ID returned by ssh_bg_run"}
  },
  "required": ["job_id"]
}
```

**동작**:
1. `_jobs.get(job_id)` 조회 → 있으면 해당 `process` 사용
2. 없으면(부모 재시작 등) `_pid_alive(job_id)`(§6.1.2 — `ssh_bg_poll` fallback과 공유하는 헬퍼)로 `/tmp/mcp_job_{job_id}.pid`를 읽어 생존 확인 후 사용
3. 둘 다 실패하면 `Unknown job: {job_id}` 반환(기존 `ssh_bg_poll`과 동일한 에러 메시지 패턴)
4. 이미 완료된 job이면 `[no-op] job_id: {job_id} already finished` 반환(kill 시도하지 않음, no-op — 아래 Response 예시와 동일 포맷)
5. 살아있으면 `SIGTERM` 전송 → 5초 대기(비동기 폴링) → 여전히 살아있으면 `SIGKILL` 승격(atexit_handler §7과 동일 패턴, Checkpoint 확정)
6. outfile에 `MCP_KILLED_{job_id}` 마커 append(기존 `MCP_DONE_{job_id}` 마커와 대칭 — `ssh_bg_poll`이 이 마커로 "killed" 상태를 "done"과 구분해 표시 가능)

**Response 예시**:
```
[killed] job_id: a1b2c3d4 (SIGTERM, graceful)
[killed] job_id: a1b2c3d4 (SIGKILL, forced after 5s timeout)
[no-op] job_id: a1b2c3d4 already finished
Unknown job: a1b2c3d4
```

### 4.1.1 프로세스 그룹 기반 kill (2026-07-07 실측으로 발견·수정)

> Check 단계에서 실제로 프로세스 트리를 띄워 검증하던 중 발견 — 정적 코드 리뷰(gap-detector)로는 잡히지 않는 OS 레벨 이슈였다.

**문제**: `ssh_bg_run`의 `Popen(["bash", "-c", cmd])`은 `bash -c "cd ~ && { 실제명령; } >& outfile; echo MCP_DONE... >> outfile"` 형태의 **래퍼** 프로세스다. `_jobs`에 기록되는 pid는 이 래퍼(bash)의 pid이지, 그 안에서 실행되는 실제 명령(예: 장기 시뮬레이션)의 pid가 아니다. 래퍼 pid에만 `SIGTERM`/`SIGKILL`을 보내면 **래퍼(bash)만 죽고 실제 명령은 고아 프로세스로 계속 실행된다** — 다음과 같이 실측 확인됨:

```
# sleep 30을 감싼 bash 래퍼(pid 309)에 SIGTERM 전송 후
$ ps -ef | grep sleep
HSLEE  310  309  sleep 30      ← SIGTERM 이후에도 여전히 살아있음(부모 309는 이미 종료)
```

이는 FR-05(`ssh_bg_kill`)와 FR-03(`atexit_handler`) 둘 다의 목적을 무력화하는 심각한 문제다 — "취소했다"고 응답하지만 실제 워크로드는 안 멈춘 것이므로.

**수정**: `ssh_bg_run`에서 `Popen(..., start_new_session=True)`로 새 프로세스 그룹(pgid == 래퍼의 pid)을 만들고, kill 시 `os.kill(pid, sig)` 대신 `os.killpg(pid, sig)`(그룹 전체에 시그널 전파)를 사용하는 `_kill_group()` 헬퍼로 통일한다. `_terminate_sync`(atexit)와 `_kill_pid_async`(`ssh_bg_kill`) 모두 이 헬퍼를 공유한다.

**플랫폼 제약**: `os.killpg`/프로세스 그룹은 POSIX 전용이라 Windows에는 없다 — 배포 대상은 cloud0(Linux) 전용이므로(§3.2) 이 프로젝트에는 영향 없지만, `_kill_group()`은 Windows(로컬 개발 환경)에서 `hasattr(os, "killpg")`가 거짓이면 단일 프로세스 kill로 폴백한다. **로컬(Windows/MSYS2) pytest는 이 그룹-kill 자체를 검증할 수 없다** — MSYS2의 자체 PID 네임스페이스가 네이티브 Windows PID와 일치하지 않아 `kill -TERM -{pid}` 형태의 그룹 시그널이 애초에 의미가 없기 때문(실측 확인됨). 따라서 이 항목은 §8.4 L3(cloud0 수동 스모크)에 검증 항목으로 추가한다.

---

## 5. UI/UX Design

N/A — CLI/stdio 기반 MCP 서버, UI 없음.

---

## 6. Error Handling

### 6.1 Lifecycle Failure Modes

| Case | Cause | Handling |
|------|-------|----------|
| idle-exit 중 job이 새로 시작됨(race) | watchdog이 job 없음을 확인한 직후 `ssh_bg_run` 호출 발생 | watchdog은 매 tick마다 조건을 새로 평가하므로, 종료 결정과 실제 `sys.exit`/루프 정지 사이 구간을 최소화(조건 확인 직후 즉시 종료 절차 진입). 완전한 원자성이 필요한 수준의 트래픽이 아니므로 best-effort로 충분(허용된 리스크로 §7 리스크에 기록) |
| watchdog이 죽어있는 job의 `.poll()` 예외 | 이미 reap된 pid 재조회 등 | `try/except` 후 해당 entry를 `_jobs`에서 제거, 로그만 남김(프로세스 자체는 계속 동작) |
| 프로세스 비정상 종료(예: OOM kill, `kill -9`) | idle_watchdog/atexit이 실행되지 못함 | **알려진 한계**: `atexit`은 `SIGKILL`을 가로챌 수 없음(그리고 별도 핸들러 없이는 `SIGTERM`도 기본 동작상 atexit을 거치지 않음) — 이 경우 남은 job은 orphan(init에 reparent)되어 outfile 작성은 계속되지만 poll 불가. 완전 방지는 Option A 범위 밖(Option B의 수퍼바이저 도입이 필요한 영역) → Plan §5 리스크에 "잔존 리스크"로 명시 |
| idle-exit 시점에 클라이언트가 실제로는 재연결 대기 중 | 타임아웃 값이 너무 짧음 | 기본값 30분(일반적인 세션 간격보다 충분히 김), 환경변수로 조정 가능하게 설계 |

### 6.1.1 비정상 종료 리스크 분석 (2026-07-07 추가 논의)

> Checkpoint 3 이후 사용자 질문 — "긴 시뮬레이션 도중 부모(`ssh_agent.py`)가 비정상 종료되면 job이 강제로 죽는가?"에 대한 분석.

**결론: job(자식 프로세스) 자체는 죽지 않는다.** `subprocess.Popen`은 기본적으로 부모와 별도 프로세스이므로, 부모 pid에만 가해지는 `kill -9`/OOM kill은 자식(예: 장기 시뮬레이션)에 전파되지 않는다(자식이 별도 process group으로 setsid되지 않는 한). 다만 **부모 메모리에 있던 `_jobs` 딕셔너리가 사라지므로, `ssh_bg_poll`로 더 이상 추적할 수 없게 된다** — job은 계속 돌지만 "보이지 않는" 상태가 된다.

발생 가능성 평가:

| 원인 | 실제 위험도 | 근거 |
|------|:-:|------|
| OOM killer가 부모(`ssh_agent.py`)를 죽임 | 낮음 | OOM killer는 RSS가 큰 프로세스를 우선 타겟팅 — 무거운 시뮬레이션 자식 프로세스가 부모보다 메모리를 훨씬 많이 쓰므로, 메모리 압박 시 자식이 먼저 죽을 가능성이 높음(이 경우는 job이 실제로 실패한 것이므로 별개 이슈) |
| 사용자/운영자가 수동으로 `kill -9`·`pkill python3` | 중간 | cloud0가 여러 프로젝트가 공유하는 호스트라, 무관한 정리 작업 중 실수로 걸릴 수 있음(예: `pkill -9 -f python3` 같은 광범위 명령) |
| 호스트 재부팅/크래시 | 낮음(빈도) / 영향 큼 | 발생 시 부모·자식 모두 종료 — 어떤 lifecycle 설계로도 막을 수 없는 영역 |

**완화책(권장, Do 단계 반영)**: `ssh_bg_poll`이 `_jobs`에서 `job_id`를 못 찾을 경우, 그대로 "Unknown job"을 반환하지 말고 `/tmp/mcp_job_{job_id}.txt` 파일 존재 여부를 **파일시스템에서 직접 재확인**하는 fallback을 추가한다. outfile은 자식 프로세스가 직접 쓰므로 부모의 생사와 무관하게 존재한다 — 이렇게 하면 부모가 재시작되어 `_jobs`가 초기화되더라도, 원래 응답에서 받은 `job_id`/`outfile` 경로만 있으면 결과를 복구할 수 있다. (이 fallback은 §11.2 구현 순서에 추가 항목으로 반영 권장)

### 6.1.2 `ssh_bg_poll` Fallback 구현 상세 (2026-07-07 추가)

두 경로로 나뉜다 — **정상 경로**(`_jobs`에 `Popen` 핸들이 있음, `.poll()`로 정확한 판정)와 **fallback 경로**(핸들 소실, outfile/pidfile로 간접 판정).

**공유 헬퍼 `_pid_alive()`** (§4.1의 `ssh_bg_kill`도 동일 헬퍼를 재사용 — PID 사이드카 파일 생존 확인 로직을 중복 구현하지 않는다):

```python
def _pid_alive(job_id: str) -> bool:
    """pidfile을 읽어 실제 OS 프로세스 생존 여부를 확인 (시그널 전송 없음)"""
    pidfile = JOB_PIDFILE_TMPL.format(job_id=job_id)
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)          # signal 0 = "죽이지 않고 존재만 확인"하는 표준 트릭
        return True
    except ProcessLookupError:
        return False              # pid 없음 = 확실히 죽음
    except PermissionError:
        return True                # 존재는 함(다른 유저 소유일 리는 없지만 방어적으로)
    except (FileNotFoundError, ValueError):
        return False               # pidfile 없거나 손상 = 판단 불가 → 죽었다고 간주
```

**`ssh_bg_poll` 분기**:

```python
elif name == "ssh_bg_poll":
    job_id = arguments["job_id"]
    job = _jobs.get(job_id)

    if job:
        # ── 정상 경로: 기존 코드와 동일 ──────────────────────────
        done    = job["process"].poll() is not None
        outfile = job["outfile"]
    else:
        # ── Fallback 경로: 부모 재시작 등으로 in-memory 기록 소실 ──
        outfile = JOB_OUTFILE_TMPL.format(job_id=job_id)
        if not os.path.exists(outfile):
            return [TextContent(type="text", text=f"Unknown job: {job_id}")]

        with open(outfile) as f:
            content = f.read()

        # Popen 핸들이 없어 .poll()을 못 쓰므로, outfile에 적힌 완료 마커로 상태 추론
        if f"MCP_KILLED_{job_id}" in content:
            return [TextContent(type="text", text=f"[killed] (recovered from disk)\n{content}")]
        done = f"MCP_DONE_{job_id}" in content or not _pid_alive(job_id)
        return [TextContent(
            type="text",
            text=f"[{'done' if done else 'running'}] (recovered from disk)\n{content}",
        )]

    output = ""
    if os.path.exists(outfile):
        with open(outfile) as f:
            output = f.read()
    status = "done" if done else "running"
    return [TextContent(type="text", text=f"[{status}]\n{output}")]
```

**판정 기준 차이**: 정상 경로는 `Popen.poll()`(OS가 직접 알려주는 정확한 exit 상태)을 쓰지만, fallback은 그게 없으므로 **outfile의 완료 마커**(`MCP_DONE_{job_id}`/`MCP_KILLED_{job_id}`)로 1차 판정하고, 마커가 아직 없으면 `_pid_alive()`로 pidfile 기반 OS 레벨 생존 확인을 한 번 더 해 "running"과 "마커 없이 비정상 종료"를 구분한다.

**알려진 미세 리스크**: pidfile의 PID가 그 사이 재활용(recycle)되어 무관한 새 프로세스를 "생존 중"으로 오판할 이론적 가능성이 있다(8자리 `job_id` + 실제 PID 재사용 주기를 고려하면 실무적으로는 무시 가능한 수준 — 잔존 리스크로만 기록).

### 6.2 로깅 (신규, 최소)

기존 코드는 stderr 로깅이 전혀 없음(stdout은 MCP 프로토콜 전용이라 사용 불가). idle-exit/reap 이벤트는 `sys.stderr`에 한 줄 로그만 남긴다(예: `[ssh-mcp] idle timeout ({N}s) — exiting`, `[ssh-mcp] reaped job {job_id} (exit={code})`). 별도 로깅 프레임워크 도입 없음(과설계 방지).

---

## 7. Security Considerations

- 기존 `subprocess.run(["bash", "-c", cmd])` 기반 명령 실행의 보안 모델(사용자가 자기 자신의 셸 접근 권한 내에서 명령 실행)은 이번 변경으로 **넓어지지 않는다** — lifecycle 코드는 새로운 입력 경로를 추가하지 않음.
- `atexit`에서 `proc.terminate()`(SIGTERM) 사용, `kill -9` 강제 종료는 사용하지 않음(job이 정리 작업을 할 기회를 줌). 일정 시간(예: 5초) 내 종료하지 않으면 `proc.kill()`로 승격.
- 환경변수 `SSH_MCP_IDLE_TIMEOUT_SEC`는 정수 파싱 실패 시 기본값(1800)으로 폴백 — 잘못된 값으로 인한 크래시 방지.

---

## 8. Test Plan

> 이 프로젝트는 테스트 인프라가 전무하다(Plan §5 리스크). Do 단계에서 pytest 골격을 신규로 마련한다.

### 8.1 Test Scope

| Type | Target | Tool | Phase |
|------|--------|------|-------|
| L1: Tool Smoke Tests | 기존 7개 tool의 happy-path 회귀 방지 | `pytest` (직접 함수 호출, MCP 프로토콜 레이어 우회) | Do |
| L2: Lifecycle Unit Tests | idle_watchdog 조건 로직, job sweep 로직 | `pytest` + `unittest.mock`(시간/프로세스 mock) | Do |
| L3: Manual Smoke | cloud0 실배포 후 반복 연결/해제 | 수동 (SSH 직접 접속, `ps`로 프로세스 수 관찰) | Check |

### 8.2 L1: Tool Smoke Test Scenarios

| # | Tool | Test Description | Expected |
|---|------|-------------------|----------|
| 1 | `ssh_run` | 정상 명령 실행 | stdout 반환, `[exit N]` 없음(returncode 0) |
| 2 | `ssh_run` | 존재하지 않는 명령 | `[stderr]` 포함 + `[exit N]` (N≠0) |
| 3 | `ssh_run` | timeout 초과 | `[timeout after Ns]` 반환 |
| 4 | `ssh_bg_run` → `ssh_bg_poll` | 짧은 명령 백그라운드 실행 후 폴링 | `job_id` 반환 → poll 시 `[done]` + outfile 내용 |
| 5 | `file_read`/`file_write` | 쓰기 후 읽기 라운드트립 | 동일 content 반환 |
| 6 | `file_ls`/`file_grep` | 디렉토리 목록/패턴 검색 | 정렬된 목록 / grep 결과 |
| 7 | `ssh_bg_run` → `ssh_bg_kill` | 장기 명령(`sleep 60`) 백그라운드 실행 후 즉시 kill | `[killed] ... (SIGTERM, graceful)` 반환, 이후 `ssh_bg_poll`이 `[done]` 또는 `[killed]` 상태 표시 |
| 8 | `ssh_bg_kill` (이미 종료) | 짧은 명령 완료 후 kill 시도 | `[no-op] ... already finished` 반환 |
| 9 | `ssh_bg_kill` (미상 job_id) | 존재하지 않는 job_id로 호출 | `Unknown job: {job_id}` 반환 |
| 10 | `ssh_bg_kill` (SIGTERM 무시) | SIGTERM을 무시하도록 만든 프로세스(`trap '' TERM; sleep 60`) kill | 5초 후 `[killed] ... (SIGKILL, forced after 5s timeout)` 반환 |

### 8.3 L2: Lifecycle Unit Test Scenarios

| # | Target | Test Description | Expected |
|---|--------|-------------------|----------|
| 1 | idle 조건 | `_last_activity`를 과거로 mock, `_jobs` 비어있음 | watchdog이 종료 조건 True 판정 |
| 2 | idle 조건 (job 존재) | `_last_activity` 과거 + 미완료 job 1개 | 종료 조건 False (job 보존) |
| 3 | job sweep | 완료된 mock process를 `_jobs`에 등록 | `.poll()` 호출로 `finished_at` 설정 확인 |
| 4 | job 정리(FR-04) | `finished_at`이 `JOB_RETENTION_SEC` 이전 | `_jobs` entry 및 outfile 삭제 확인 |
| 5 | atexit 정리 | 살아있는 mock process 1개 등록 후 핸들러 호출 | `terminate()` 호출 확인 |

### 8.4 L3: Manual Smoke (Check 단계)

| # | Scenario | Steps | Success Criteria |
|---|----------|-------|-------------------|
| 1 | 반복 연결/해제 | cloud0에 SSH로 접속해 `ps aux \| grep ssh_agent.py`를 세션 시작 전/중/후 여러 번 확인 | 프로세스 수가 활성 세션 수를 초과해 누적되지 않음 |
| 2 | bg job 미폴링 방치 | `ssh_bg_run`으로 짧은 job 시작 후 `ssh_bg_poll` 호출하지 않고 방치 | `ps`에서 해당 자식이 좀비(`<defunct>`)로 오래 남지 않음(다음 watchdog tick 내 reap) |
| 3 | idle 타임아웃 실동작 | `SSH_MCP_IDLE_TIMEOUT_SEC=60`으로 짧게 설정 후 미사용 방치 | 60초+watchdog 주기 내 프로세스 자체 종료 확인 |
| 4 | 프로세스 그룹 kill(§4.1.1) | `ssh_bg_run`으로 장기 명령(`sleep 60`) 시작 → `ssh_bg_kill` 호출 → `ps -ef \| grep sleep` | 래퍼(bash)뿐 아니라 `sleep`도 함께 종료됨(고아로 남지 않음) — 로컬 Windows에서는 검증 불가(§4.1.1) |
| 5 | SIGTERM 무시 job → SIGKILL 승격(§8.2 #10) | `ssh_bg_run`으로 `bash -c "trap '' TERM; sleep 60"` 시작 → `ssh_bg_kill` 호출 | ~5초 후 `[killed] ... (SIGKILL, forced after 5s timeout)` 확인 — 로컬 Windows에서는 trap이 우회되어 검증 불가(§8.2 #10 테스트 주석 참조) |

### 8.5 Seed Data Requirements

N/A — DB 없음. 테스트는 mock 프로세스/타임스탬프로 충분.

---

## 9. Clean Architecture

N/A — 단일 파일(233줄) 프로젝트, 레이어 분리를 도입하지 않는다(Design Principles §1.2 참조). 신규 함수(`idle_watchdog`, `job_sweep`, `atexit_handler`)는 기존 파일 내 별도 섹션(`# ─── Lifecycle ───`)으로 구분해 가독성만 확보한다.

---

## 10. Coding Convention Reference

기존 프로젝트에 별도 컨벤션 문서 없음(Plan §8.1). 기존 파일 스타일을 그대로 따른다:

| Item | Convention Applied |
|------|--------------------|
| 함수 스타일 | 기존과 동일하게 `async def`, tool 핸들러는 `if/elif` 분기 유지 |
| 섹션 구분 주석 | 기존 `# ─── ... ───` 스타일 재사용(예: `# ─── Lifecycle ───`) |
| 에러 처리 | 기존과 동일하게 `try/except Exception as e` 후 `[error] {e}` 텍스트 반환(신규 lifecycle 코드도 예외를 삼키고 로그만 남겨 서버 자체는 죽지 않게 함) |
| 환경변수 | 신규 `SSH_MCP_IDLE_TIMEOUT_SEC` 하나만 추가, 기존 프로젝트에 prefix 컨벤션 없으므로 신규 정의 없음(N/A) |

---

## 11. Implementation Guide

### 11.1 File Structure

```
ssh-mcp/
├── ssh_agent.py          # 기존 파일 수정 (idle_watchdog, job_sweep, atexit_handler 추가)
└── tests/
    ├── test_tools_smoke.py       # L1
    └── test_lifecycle.py         # L2
```

### 11.2 Implementation Order

1. [ ] pytest 골격 + `tests/test_tools_smoke.py` (기존 7개 tool 회귀 방지 먼저 확보 — Plan §5 리스크 대응)
2. [ ] `_last_activity` 전역 변수 + `call_tool()` 진입 시 갱신
3. [ ] `ssh_bg_run`에 PID 사이드카 파일(`/tmp/mcp_job_{job_id}.pid`) 기록 추가 + `JobEntry`에 `pidfile` 필드 추가(§3.1, FR-05 선행 작업)
4. [ ] `job_sweep()`: `_jobs` 순회 `.poll()` 호출(zombie reap) + `finished_at` 기록 + 24h 경과 entry 정리(outfile + pidfile 포함)
5. [ ] `idle_watchdog()`: `WATCHDOG_INTERVAL_SEC` 주기 asyncio 태스크, idle+no-job 조건 시 종료 트리거
6. [ ] `atexit_handler()`: 등록 후 살아있는 job에 SIGTERM(5초 대기 후 SIGKILL 승격)
7. [ ] `ssh_bg_poll` fallback: `_jobs`에 `job_id`가 없으면 `/tmp/mcp_job_{job_id}.txt` 파일 직접 확인(부모 재시작으로 `_jobs`가 초기화돼도 결과 복구 가능하게 — §6.1.1)
8. [ ] 신규 tool `ssh_bg_kill` 구현(§4.1): `_jobs` → pidfile 순으로 PID 조회, SIGTERM→5초→SIGKILL 승격(atexit_handler와 동일 헬퍼 함수 재사용 권장), outfile에 `MCP_KILLED_{job_id}` 마커 append
9. [ ] `list_tools()`에 `ssh_bg_kill` 등록(7번째 tool)
10. [ ] `main()`에 `idle_watchdog` 태스크 시작 로직 연결(`asyncio.create_task`)
11. [ ] `tests/test_lifecycle.py` (L2 유닛 테스트, fallback + kill 포함)
12. [ ] cloud0 배포 전 최종 로컬 스모크(7개 tool 전부 + 신규 lifecycle 동작)

### 11.3 Session Guide

#### Module Map

| Module | Scope Key | Description | Estimated Turns |
|--------|-----------|--------------|:---:|
| pytest 골격 + 기존 tool 스모크 | `module-1` | 회귀 안전망 먼저 구축(Plan §5 리스크) | 15-20 |
| idle self-exit + job reaper | `module-2` | `_last_activity`, `idle_watchdog`, `job_sweep`, `atexit_handler` 구현, PID 사이드카 파일(`.pid`) 기록 | 25-35 |
| `ssh_bg_kill` tool + poll fallback | `module-3` | 신규 tool 추가(§4.1), `ssh_bg_poll`의 outfile fallback(§6.1.1) 동시 구현 | 20-25 |
| 로컬 검증 + lifecycle 유닛 테스트 | `module-4` | L2 테스트 작성, 로컬에서 idle timeout 단축(`SSH_MCP_IDLE_TIMEOUT_SEC=10`)해 동작 확인, `ssh_bg_kill` L1 시나리오 검증 | 20-25 |

#### Recommended Session Plan

| Session | Phase | Scope | Turns |
|---------|-------|-------|:-----:|
| Session 1 | Plan + Design | 전체(FR-05 `ssh_bg_kill` 추가 포함) | 완료 (본 세션) |
| Session 2 | Do | `--scope module-1` | 15-20 |
| Session 3 | Do | `--scope module-2,module-3` | 45-60 |
| Session 4 | Do | `--scope module-4` | 20-25 |
| Session 5 | Check + Report | 전체(+ cloud0 실배포 스모크는 사용자 승인 후 수동 진행 — Plan §5 배포 리스크) | 30-40 |

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-07-07 | 초안 — Checkpoint 3에서 옵션 A(자체 idle self-exit) 선택. idle_watchdog + job reaper(zombie reap, atexit 정리, outfile 정리) 설계. 웹앱 전용 템플릿 섹션(DB/API/UI/Clean Architecture 레이어)은 N/A 처리. | hoseung.lee |
| 0.2 | 2026-07-07 | Plan FR-05 반영 — 신규 tool `ssh_bg_kill` 설계(§4.1). PID 사이드카 파일(`.pid`, §3.1) 도입해 부모 재시작 후에도 kill 가능하게 함. Kill 동작은 atexit_handler와 동일 패턴(SIGTERM→5초→SIGKILL)으로 확정. Test Plan §8.2에 L1 시나리오 4개 추가, Module Map에 `module-3`(ssh_bg_kill) 신설. | hoseung.lee |
| 0.3 | 2026-07-07 | `ssh_bg_poll` fallback(§6.1.2)의 실제 구현 pseudocode 추가 — `_pid_alive()` 공유 헬퍼(`ssh_bg_kill`과 공용), `JOB_OUTFILE_TMPL`/`JOB_PIDFILE_TMPL` 전역 상수(§3.2)로 4개 코드 경로(`ssh_bg_run`/`ssh_bg_poll`/`ssh_bg_kill`/`job_sweep`)의 경로 포맷 중복 제거. | hoseung.lee |
| 0.4 | 2026-07-07 | Do+Check 단계 구현/실측 중 발견한 사항 반영: (1) §4.1.1 신규 — 래퍼(bash) pid만 kill하면 내부 실제 명령이 고아로 남는 문제를 실측으로 발견, `start_new_session=True` + `_kill_group()`(프로세스 그룹 kill)으로 수정, POSIX 전용(Windows는 단일 프로세스 kill로 폴백)임을 명시. (2) §8.4에 L3 항목 2개 추가(그룹 kill 확인, SIGTERM 무시→SIGKILL 승격 확인) — 둘 다 로컬 Windows에서는 검증 불가해 cloud0 전용. (3) §4.1 step 4 문구를 실제 Response 포맷(`[no-op] ...`)과 일치하도록 정정(gap-detector Gap C). (4) "6개 tool" 표기를 실제 개수(7개)로 정정(gap-detector Gap D). | hoseung.lee |
