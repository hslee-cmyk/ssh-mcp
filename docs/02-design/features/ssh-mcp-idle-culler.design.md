# ssh-mcp-idle-culler Design Document

> **Summary**: `ssh_agent.py`의 `idle_watchdog`(고정 30분 활동 타이머)을 완전히 제거하고, EOF 기반 정상 종료 + 클라이언트 SSH keepalive(이미 적용) + cloud0 전용 외부 `idle_culler.py`(orphan+age 휴리스틱, cron)로 대체한다.
>
> **Project**: ssh-mcp (`Todoc/fpga/ssh-mcp/`)
> **Version**: N/A (단일 파일, 버전 관리 없음)
> **Author**: hoseung.lee
> **Date**: 2026-07-08
> **Status**: Draft
> **Planning Doc**: [ssh-mcp-idle-culler.plan.md](../../01-plan/features/ssh-mcp-idle-culler.plan.md)

### Pipeline References

N/A — 웹앱 파이프라인(Phase 1~4) 미사용. `ssh-mcp-process-lifecycle.design.md`(선행 사이클)와 동일한 컨벤션을 따른다.

---

## Context Anchor

> Plan 문서에서 그대로 복사.

| Key | Value |
|-----|-------|
| **WHY** | idle_watchdog이 활동 없음=연결 끊김으로 잘못 가정해, 정상적으로 조용한 세션까지 강제 종료시켜 재연결 마찰을 유발함 |
| **WHO** | ssh-mcp를 통해 cloud0에 접속해 장시간 작업(코드 리뷰, 사고, 긴 응답 대기 등)하는 Claude Code 사용자 |
| **RISK** | EOF 기반 종료로 전환 시 keepalive 설정이 없는 클라이언트 환경에서는 죽은 연결이 감지되지 않고 방치될 수 있음 — 외부 idle-culler가 최종 안전망 |
| **SUCCESS** | (1) 정상 세션은 idle 시간과 무관하게 재연결 불필요 (2) 죽은 연결은 ~90초 내 정리 (3) 방치 프로세스는 idle-culler로 무한 누적 방지 |
| **SCOPE** | FR-01 재정의(고정 타이머 제거, idle_watchdog 완전 제거) + cloud0 전용 idle-culler cron 신규 배포. SC-2~SC-8(job reap, `ssh_bg_kill` 등)은 무영향 |

---

## 1. Overview

### 1.1 Design Goals

- `ssh_agent.py`는 `stdin` EOF에서만 종료한다(기존 `stdio_server()` 종료 경로 그대로, 신규 구현 불필요) — 활동 타이머 완전 제거(FR-01).
- 죽은 연결은 클라이언트 SSH keepalive(이미 적용 완료)로 로컬에서 먼저 감지되어 ~90초 내 EOF로 전파된다(FR-02, 이 저장소 범위 밖).
- 클라이언트 keepalive가 없거나 실패하는 경우에 대비해, cloud0에 배포되는 외부 `idle_culler.py`가 방치된 `ssh_agent.py` 프로세스를 주기적으로 탐지·정리한다(FR-03).
- `idle_culler.py`는 `ssh_agent.py` 프로세스 내부에 스레드/파일 등 아무것도 추가하지 않는다 — 순수 `/proc` 관찰 + 시그널 전송만 수행한다(FR-04).

### 1.2 Design Principles

- **관찰 기반, 비침습적**: culler는 `ssh_agent.py`를 전혀 수정하지 않고 외부에서만 관찰·정리한다(Plan §7.2 결정과 동일).
- **정밀함보다 안전망**: 이 culler는 1차 방어선(EOF+keepalive)이 실패했을 때만 작동하는 백스톱이다 — 아래 §2.0에서 실측으로 확인했듯 "정밀한 연결 생사 판정"은 root 없이는 불가능하므로, 완벽한 판정 대신 두 가지 저비용·고신뢰 신호(고아 프로세스 감지 + 넉넉한 age 임계값)의 조합으로 충분히 낮은 오탐률을 확보한다.
- **실행 중인 job은 항상 보존**: `ssh_agent.py`가 `ssh_bg_run`으로 띄운 job이 하나라도 살아있으면, 고아 상태이든 age가 임계값을 넘었든 절대 kill하지 않는다(기존 `_has_running_job()`과 동일한 원칙을 외부에서 재현).

---

## 2. Architecture Options

### 2.0 Architecture Comparison (Checkpoint 3 — 완료)

> Plan §7.2에서는 xcelium-mcp의 `idle_culler.py`를 "이식 비용 낮음"으로 가정했으나, cloud0에서 직접 실측한 결과 그 가정이 틀렸다는 것이 확인되었다 — 아래 실측 근거를 architecture 선택의 전제로 삼는다.

**실측 근거** (2026-07-08, cloud0):
1. xcelium-mcp의 worker는 자기 자신이 TCP 소켓(xmsim bridge)을 들고 있어 `has_established_tcp(pid)`가 그 프로세스의 `/proc/<pid>/fd`만 보면 판정 가능하다. 반면 `ssh_agent.py`는 stdio(pipe)로만 통신하며, 실측 결과 자기 자신의 열린 소켓은 `AF_UNIX`(asyncio 내부용, `/proc/net/unix`로 확인됨) 뿐이었다 — TCP 소켓은 조상 프로세스인 `sshd: {user}@notty`가 들고 있다.
2. `sshd` 조상 프로세스의 fd/TCP 상태는 `netstat -tnp`(root 없이 실행 시 해당 프로세스만 `PID/Program name`이 `-`로 표시됨, 실측 확인) 및 `/proc/<sshd_pid>/fd`(`Permission denied`, 실측 확인 — `ptrace_scope=0`임에도 거부되어 sshd의 non-dumpable 속성으로 추정) 양쪽 다 root 없이는 읽을 수 없다.
3. 따라서 "정밀한 TCP ESTABLISHED 상태 확인"은 root 권한 없이는 근본적으로 불가능 — Option B는 채택 불가.
4. 반면 프로세스 트리 정보는 동일 uid의 일반 프로세스(로그인 셸, `ssh_agent.py` 자신)에 대해서는 문제없이 읽힌다 — `/proc/<pid>/stat`의 ppid 필드, `/proc/<pid>/task/<pid>/children` 모두 실측으로 정상 동작 확인됨.

| Criteria | Option A: orphan+age 휴리스틱 (선택됨) | Option B: sshd TCP 상태 정밀 확인 | Option C: idle_watchdog 유지 + 타이머만 단축 |
|----------|:-:|:-:|:-:|
| **Approach** | 부모 프로세스 생존(ppid==1 감지) + age 임계값 + 실행 중 job 없음, `/proc` 전용 | sshd 조상의 TCP 소켓 상태를 직접 확인 | 외부 스크립트 도입 없이 앱 내부 타이머만 존치 |
| **root 필요 여부** | 불필요 | **필수**(실측으로 불가능 확인 — cloud0 계정은 root 아님) | 불필요 |
| **New Files** | 1 (`idle_culler.py`) + crontab 항목 | 1 + sudoers/setuid 설정 | 0 |
| **Modified Files** | 1 (`ssh_agent.py`, idle_watchdog 제거) | 1 | 0 (또는 타임아웃 상수만) |
| **정밀도** | 중간(고아 판정은 즉시·정확, age 백스톱은 근사치) | 높음(불가능하므로 의미 없음) | 낮음(활동=연결이라는 근본 가정 그대로) |
| **Feasibility (cloud0)** | ✅ 실측 검증됨 | ❌ root 없어 불가능 | ✅ 가능하나 Plan에서 이미 기각(근본 문제 미해결) |
| **Risk** | Low — 오탐 시나리오는 §6.1에서 분석, 실행 중 job 보존으로 완화 | N/A(채택 불가) | 재연결 마찰 문제 그대로 잔존 |

**Selected**: **Option A** — **Rationale**: Option B는 이번 Design 단계 실측으로 root 권한 없이는 구현 자체가 불가능함이 확인되어 배제된다. Option C는 Plan 단계에서 이미 "근본 원인 미해결"로 기각된 방향과 동일하다. Option A는 정밀도는 완벽하지 않지만(연결이 살아있는데 오래+무활동인 극단적 케이스를 오탐할 이론적 가능성), (1) 고아 감지는 SSH 세션 종료의 압도적 다수 케이스를 즉시·정확하게 잡아내고, (2) age 백스톱은 순수 최후 안전망으로만 작동하며 실행 중 job은 항상 보존하므로 실사용 리스크가 낮다.

> 사용자 확인(Checkpoint 3, 2026-07-08): 옵션 A 선택.

### 2.1 Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│ cloud0                                                                    │
│                                                                            │
│  sshd: {user}@notty (TCP 소켓 보유, ssh_agent.py에서는 접근 불가)          │
│    └─ login shell (tcsh/bash 등)         ← ssh_agent.py의 직접 부모        │
│         └─ python3 ssh_agent.py           ← EOF에서만 자체 종료(FR-01)     │
│              └─ (있다면) ssh_bg_run job    ← Popen 자식, culler가 보존      │
│                                                                            │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │ idle_culler.py — cron, */5분, root 아님, ssh_agent.py 코드 무변경   │   │
│  │  1. /proc/*/cmdline 스캔 → "python3 .../ssh_agent.py" 프로세스 탐색 │   │
│  │  2. /proc/<pid>/task/<pid>/children 확인 → 살아있는 자식(job) 있음? │   │
│  │       있으면 → 건너뜀(항상 보존)                                     │   │
│  │  3. /proc/<pid>/stat의 ppid 확인 → ppid == 1(고아)?                 │   │
│  │       예 → 즉시 SIGTERM→(grace)→SIGKILL                            │   │
│  │       아니오 → 4로                                                  │   │
│  │  4. process_age(pid) > IDLE_THRESHOLD_SEC(기본 6h)?                 │   │
│  │       예 → SIGTERM→(grace)→SIGKILL (백스톱)                        │   │
│  │       아니오 → 건너뜀                                               │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Process Lifecycle Flow (재정의 후)

```
Claude Code 세션 시작 → ssh (client, keepalive 적용됨) → sshd → login shell → python3 ssh_agent.py
    │
    ├─ 정상 종료: client 연결 끊김 → stdin EOF → stdio_server 정상 종료 (FR-01, 신규 구현 없음)
    │
    ├─ 네트워크 끊김(clean): 클라이언트 keepalive(ServerAliveInterval=30/CountMax=3)가
    │     ~90초 내 로컬 ssh를 종료 → 서버 측에 정상 전파 → 위와 동일 경로로 EOF 종료
    │
    └─ 위 두 경로 모두 실패한 잔존 케이스(백스톱):
          idle_culler.py(cron, */5분)가 (a) 고아 프로세스는 즉시, (b) 그 외에는
          age > IDLE_THRESHOLD_SEC(기본 6h) + 실행 중 job 없음 조건으로 정리
```

### 2.3 Dependencies

| Component | Depends On | Purpose |
|-----------|-----------|---------|
| `idle_culler.py` | `/proc/*/cmdline`, `/proc/<pid>/stat`, `/proc/<pid>/task/<pid>/children` | ssh_agent.py 프로세스 탐색, 고아/age 판정, 실행 중 job 확인 |
| `ssh_agent.py` (수정) | (제거) `idle_watchdog`, `IDLE_TIMEOUT_SEC`, `WATCHDOG_INTERVAL_SEC`, `_last_activity` | 이번 사이클에서 완전 삭제 — `stdio_server()`의 기존 EOF 종료 경로만 남김 |
| cron (cloud0) | `idle_culler.py` | 5분 주기 실행 (xcelium-mcp `deploy/crontab.example`과 동일 패턴) |

---

## 3. In-Memory State Model

> `idle_culler.py`는 상태를 저장하지 않는(stateless) 1회성 스크립트다 — 매 cron 실행마다 `/proc`을 새로 스캔한다. `ssh_agent.py` 쪽 in-memory 상태(`_jobs` 등, 선행 사이클에서 설계됨)는 이번 사이클에서 변경하지 않는다.

### 3.1 제거 대상 (`ssh_agent.py`)

| 항목 | 현재 | 조치 |
|------|------|------|
| `idle_watchdog()` (코루틴) | `WATCHDOG_INTERVAL_SEC`마다 idle 조건 확인 | **삭제** |
| `IDLE_TIMEOUT_SEC` | 환경변수 `SSH_MCP_IDLE_TIMEOUT_SEC` (기본 1800) | **삭제** |
| `WATCHDOG_INTERVAL_SEC` | 60 | **삭제** |
| `_last_activity` (전역) + 갱신 코드 | `call_tool()` 진입부에서 매번 갱신 | **삭제** (더 이상 어떤 코드도 참조하지 않아야 함 — §6.2 Impact Analysis 참고) |
| `main()`의 `asyncio.create_task(idle_watchdog())` | watchdog 태스크 생성 | **삭제** — `stdio_server()` 정상 종료 경로만 남김 |

`_has_running_job()`은 `job_sweep()` 등 다른 곳에서 계속 사용되므로 함수 자체는 유지한다(Plan §6.3 확인 필요 항목).

### 3.2 신규 모듈 상수 (`idle_culler.py`)

| Name | Type | Default | Purpose |
|------|------|---------|---------|
| `AGENT_CMDLINE_MARKER` | `bytes` | `b"ssh_agent.py"` | cmdline 매칭 대상 (argv 파싱 후 정확히 매칭, §4.1 참고 — 단순 substring 매칭 아님) |
| `IDLE_THRESHOLD_SEC` | `int` | `6 * 3600` (6시간) | 환경변수 `SSH_MCP_CULLER_IDLE_THRESHOLD_SEC`로 override 가능 |
| `KILL_GRACE_SEC` | `int` | `5` | SIGTERM 후 SIGKILL 승격 전 대기 시간 (기존 `_kill_group()`/`ssh_bg_kill`과 동일한 값) |
| `ORPHAN_PPID` | `int` | `1` | 고아 판정 기준 ppid (Do 단계에서 cloud0 실제 재부모 대상이 pid 1인지 재확인 — §7 리스크) |

---

## 4. API Specification

REST API 없음. `idle_culler.py`는 MCP tool이 아니라 독립 cron 스크립트다. 기존 8개 MCP tool 시그니처는 변경하지 않는다.

### 4.1 `idle_culler.py` 핵심 로직 (pseudocode)

```python
"""idle_culler.py — ssh-mcp용 orphan+age 휴리스틱 (xcelium-mcp idle_culler.py의
has_established_tcp() 기반 판정을 이식하지 않음 — ssh_agent.py는 자신의 TCP 소켓을
갖지 않고, sshd 조상의 fd/TCP 상태는 root 없이는 읽을 수 없음이 cloud0 실측으로
확인됨(design.md §2.0). 대신 (a) 부모 프로세스 생존 여부(orphan 감지)와
(b) age 임계값을 사용한다."""

AGENT_CMDLINE_MARKER = b"ssh_agent.py"
IDLE_THRESHOLD_SEC = int(os.environ.get("SSH_MCP_CULLER_IDLE_THRESHOLD_SEC", 6 * 3600))
KILL_GRACE_SEC = 5
ORPHAN_PPID = 1

def is_ssh_agent_argv(argv: list[bytes]) -> bool:
    """xcelium-mcp의 flock 오탐 사례(module docstring 참고)와 동일한 함정을 피하기
    위해, 전체 cmdline에 대한 substring 매칭이 아니라 argv[-1]이 정확히
    ssh_agent.py로 끝나는지 확인한다 — 'grep ssh_agent.py' 같은 무관한 프로세스가
    cmdline에 같은 문자열을 인자로 담고 있어도 오매칭되지 않는다."""
    return bool(argv) and Path(argv[-1].decode(errors="replace")).name == "ssh_agent.py"

def find_ssh_agent_pids() -> list[int]:
    # /proc/*/cmdline 스캔, is_ssh_agent_argv()로 필터 (xcelium find_supervisor_pid()와 동일 패턴)
    ...

def has_live_children(pid: int) -> bool:
    # /proc/<pid>/task/<pid>/children 읽어 비어있지 않으면 True (job 실행 중)
    ...

def is_orphaned(pid: int) -> bool:
    # /proc/<pid>/stat의 ppid 필드(parse_stat_ppid, xcelium parse_stat_starttime과
    # 동일한 파싱 방식 재사용 — rpartition(")") 후 필드 인덱싱)가 ORPHAN_PPID(1)인지 확인
    ...

def process_age_seconds(pid: int) -> float:
    # xcelium idle_culler.py의 process_age_seconds()를 그대로 재사용 (변경 없음)
    ...

def _cull_if_eligible(pid: int) -> None:
    if has_live_children(pid):
        return  # job 실행 중 — 항상 보존
    if not is_orphaned(pid) and process_age_seconds(pid) <= IDLE_THRESHOLD_SEC:
        return  # 살아있는 세션 + 아직 백스톱 임계값 미달
    # 고아이거나(즉시) age 임계값 초과(백스톱) — 정리
    # xcelium _cull_if_idle()과 동일한 SIGTERM → KILL_GRACE_SEC 대기 → SIGKILL 승격
    ...

def main() -> int:
    if sys.platform == "win32":
        print("ssh-mcp-idle-culler requires /proc — Linux/cloud0 only.", file=sys.stderr)
        return 1
    for pid in find_ssh_agent_pids():
        try:
            _cull_if_eligible(pid)
        except (OSError, ValueError, IndexError):
            continue  # 스캔과 처리 사이 pid 소멸 등 — 치명적이지 않음
    return 0
```

**xcelium-mcp `idle_culler.py`에서 그대로 재사용하는 것**: `parse_uptime_seconds()`, `process_age_seconds()`의 계산 방식(starttime ticks + `/proc/uptime` + `SC_CLK_TCK`), SIGTERM→grace→SIGKILL 승격 패턴, `try/except (OSError, ValueError, IndexError): continue`로 스캔 중 pid 소멸을 무해하게 처리하는 방어적 코딩 스타일.

**재사용하지 않는 것(§2.0 실측 근거)**: `has_established_tcp()`, `_socket_inodes_for_pid()`, `parse_tcp_table_established_inodes()` — ssh_agent.py 자신은 TCP 소켓을 갖지 않으므로 무의미. `find_worker_pids()`(supervisor의 children 조회) — ssh-mcp에는 supervisor가 없고 `ssh_agent.py` 프로세스 자체가 최상위 대상이므로 대신 `find_ssh_agent_pids()`로 직접 cmdline 스캔.

---

## 5. UI/UX Design

N/A — CLI/cron 기반, UI 없음.

---

## 6. Error Handling

### 6.1 오탐/누락 시나리오 분석

| Case | 원인 | 영향 | 완화 |
|------|------|------|------|
| 살아있는 세션인데 6시간 넘게 무활동 + job도 없음 | age 백스톱이 오탐 | 정상 세션이 강제 종료됨(재연결 필요) | 임계값을 넉넉하게(6h) 잡아 발생 빈도를 낮춤 + 환경변수로 조정 가능 |
| 고아인데 아직 job이 살아있음(드묾) | 세션 종료 직후 job이 미처 안 끝남 | `has_live_children()`이 True → 정리 건너뜀 | 의도된 동작 — 다음 cron tick에 job이 끝나 있으면 그때 정리됨. Job이 무한정 안 끝나는 경우는 §7 잔존 리스크(선행 사이클 문서와 동일한 SIGKILL 불가 케이스 제외하면 이론상 발생 안 함) |
| ppid가 1이 아닌 다른 subreaper로 재부모됨(가정 오류) | 컨테이너/systemd 환경에 따라 orphan 대상이 pid 1이 아닐 수 있음 | 고아 감지가 작동하지 않고 age 백스톱에만 의존하게 됨(정리가 최대 6시간 지연) | Do 단계에서 cloud0 실측으로 실제 재부모 대상 pid를 확인 후 `ORPHAN_PPID` 값 확정(§3.2) — 백스톱이 있으므로 최악의 경우도 무한 누적은 아님 |
| 스캔 시점과 kill 시도 사이 pid 재사용(recycle) | 극히 드묾 | 무관한 새 프로세스를 오탐 kill할 이론적 가능성 | `is_ssh_agent_argv()` 재확인 없이 pid만으로 kill하지 않고, kill 직전 cmdline을 한 번 더 확인하는 재검증 단계를 Do 단계 구현에 추가 권장(잔존 리스크로 기록, 실무적으로는 무시 가능한 수준 — xcelium `idle_culler.py`도 동일 수준의 리스크를 감수함) |

### 6.2 Impact Analysis (Plan §6 확장)

| Resource | Operation | Code Path | Impact |
|----------|-----------|-----------|--------|
| `idle_watchdog` | 삭제 | `main()`의 `asyncio.create_task(idle_watchdog())` | Breaking(의도적) |
| `_last_activity` | 삭제 | 각 tool 핸들러 진입부 갱신 코드 | 갱신 코드까지 모두 제거되어야 dead code 없음 — Do 단계에서 `grep -n _last_activity ssh_agent.py`로 전량 확인 필요 |
| `_has_running_job()` | 유지 | `job_sweep()` 등 | idle_watchdog 삭제와 무관하게 계속 정상 동작해야 함 |

### 6.3 로깅

`idle_culler.py`는 stderr에 최소 로그만 남긴다(예: `[idle-culler] orphaned pid={pid} — killed`, `[idle-culler] age-backstop pid={pid} age={N}s — killed`). cron 표준 동작상 stderr는 대개 시스템 메일/로그로 전달되므로 별도 로그 파일 관리 불필요(xcelium-mcp와 동일한 최소주의).

---

## 7. Security Considerations

- `idle_culler.py`는 root로 실행되지 않는다(§2.0 실측으로 확인된 제약을 오히려 설계 전제로 삼음) — 자기 자신의 uid가 소유한 프로세스만 kill 가능(`os.kill`의 커널 권한 검사가 그대로 보장).
- `SIGTERM`만 우선 사용하고 `KILL_GRACE_SEC`(5초) 대기 후에만 `SIGKILL`로 승격 — 선행 사이클(`ssh-mcp-process-lifecycle`)의 `atexit_handler`/`ssh_bg_kill`과 동일한 패턴, 신규 정책 아님.
- 환경변수 `SSH_MCP_CULLER_IDLE_THRESHOLD_SEC` 파싱 실패 시 기본값(6h)으로 폴백.
- cmdline 매칭을 `argv[-1]` 정확 비교로 제한해(§4.1) 임의의 무관한 프로세스를 오매칭·오살하는 것을 방지.

---

## 8. Test Plan

### 8.1 Test Scope

| Type | Target | Tool | Phase |
|------|--------|------|-------|
| L1: Pure 파싱 함수 유닛 테스트 | `is_ssh_agent_argv`, ppid 파싱, age 계산 | `pytest`(플랫폼 무관, mock 텍스트 입력) | Do |
| L2: `/proc` 연동 유닛 테스트 | `find_ssh_agent_pids`, `has_live_children`, `is_orphaned` | `pytest` + 실제 자식 프로세스 fixture(로컬 POSIX 필요 — Windows는 skip) | Do |
| L3: cloud0 실배포 스모크 | 고아 정리, age 백스톱, job 보존, cmdline 오매칭 방지 | 수동(SSH 직접 접속) | Check |

### 8.2 L1: 파싱 함수 유닛 테스트

| # | Target | Test Description | Expected |
|---|--------|-------------------|----------|
| 1 | `is_ssh_agent_argv` | `[b"python3", b"/opt/ssh-mcp/ssh_agent.py"]` | True |
| 2 | `is_ssh_agent_argv` | `[b"bash", b"-c", b"grep ssh_agent.py /var/log/x"]` (오매칭 방지 확인) | False |
| 3 | ppid 파싱 | `/proc/<pid>/stat` 형식 텍스트, comm에 공백/괄호 포함(`(my proc)`) | 올바른 ppid 필드 추출 (xcelium `parse_stat_starttime`과 동일한 `rpartition(")")` 안전성 확인) |
| 4 | `process_age_seconds` | mock starttime + mock uptime | 예상 경과 초 반환 (xcelium 로직 재사용 검증) |

### 8.3 L2: `/proc` 연동 유닛 테스트 (POSIX 전용, Windows skip)

| # | Target | Test Description | Expected |
|---|--------|-------------------|----------|
| 1 | `find_ssh_agent_pids` | `sleep 100`을 `ssh_agent.py`처럼 이름 붙인 fixture는 매칭 안 됨, 실제 이름 매칭만 True | 정확히 매칭된 pid만 반환 |
| 2 | `has_live_children` | 자식 프로세스 fork 후 확인 | True; 자식 종료 후 재확인 | False |
| 3 | `is_orphaned` | 정상 부모 하에서 실행 중인 프로세스 | False (ppid != 1) |

### 8.4 L3: cloud0 실배포 스모크 (Check 단계)

| # | Scenario | Steps | Success Criteria |
|---|----------|-------|-------------------|
| 1 | 고아 프로세스 즉시 정리 | `ssh_agent.py`를 `nohup`+`setsid`로 부모 없이 실행(고아 시뮬레이션) 후 `idle_culler.py` 수동 실행 | 다음 실행에서 즉시 SIGTERM→SIGKILL로 정리됨 |
| 2 | 정상 세션은 보존 | 정상 SSH 세션으로 연결된 `ssh_agent.py`가 살아있는 상태에서 `idle_culler.py` 수동 실행 | 정리되지 않음(고아 아님 + age 미달) |
| 3 | age 백스톱 | `IDLE_THRESHOLD_SEC`를 짧게(예: 10초) override 후 정상 세션 유지한 채 대기 | age 초과 시 정리됨(백스톱 동작 확인) — 단, 정상 세션이 강제 종료되는 트레이드오프도 함께 확인 |
| 4 | 실행 중 job 보존 | `ssh_bg_run`으로 장기 명령(`sleep 120`) 시작 후, 부모 세션을 강제로 죽여 고아 상태를 만든 뒤 `idle_culler.py` 실행 | job이 살아있는 동안은 `ssh_agent.py`가 정리되지 않음(`has_live_children` True) |
| 5 | cmdline 오매칭 방지 | `bash -c "sleep 30 # ssh_agent.py"`처럼 cmdline에 문자열만 포함된 무관한 프로세스 실행 후 `idle_culler.py` 실행 | 오매칭되어 kill되지 않음 |

### 8.5 Seed Data Requirements

N/A — DB 없음. 실제/fixture 프로세스로 충분.

---

## 9. Clean Architecture

N/A — `ssh_agent.py`와 동일하게 단일 파일 스타일 유지(`idle_culler.py` 자체도 xcelium-mcp 참조 구현과 유사하게 단일 파일).

---

## 10. Coding Convention Reference

| Item | Convention Applied |
|------|--------------------|
| 함수 스타일 | xcelium-mcp `idle_culler.py`와 동일한 순수 함수 + `/proc` I/O 분리 스타일(§4.1 pure vs `/proc`-backed 구분) 재사용 |
| 방어적 코딩 | `try/except (OSError, ValueError, IndexError): continue` — xcelium 패턴 그대로 |
| 로깅 | stderr 한 줄, 별도 프레임워크 없음(선행 사이클과 동일 원칙) |
| 환경변수 | `SSH_MCP_CULLER_IDLE_THRESHOLD_SEC` 하나만 추가(기존 프로젝트에 prefix 컨벤션 없음) |

---

## 11. Implementation Guide

### 11.1 File Structure

```
ssh-mcp/
├── ssh_agent.py              # idle_watchdog/IDLE_TIMEOUT_SEC/WATCHDOG_INTERVAL_SEC/_last_activity 제거
├── idle_culler.py            # 신규 — cloud0 배포용, xcelium-mcp idle_culler.py 참조하되 TCP 판정부는 orphan+age로 대체
├── deploy/
│   └── crontab.example        # 신규 — */5 * * * * python3 /opt/ssh-mcp/idle_culler.py
└── tests/
    ├── test_tools_smoke.py            # 기존 (선행 사이클)
    ├── test_lifecycle.py              # 기존 (선행 사이클) — idle_watchdog 관련 테스트는 이번에 제거
    └── test_idle_culler.py            # 신규 — L1/L2
```

### 11.2 Implementation Order

1. [ ] `ssh_agent.py`에서 `idle_watchdog`/`IDLE_TIMEOUT_SEC`/`WATCHDOG_INTERVAL_SEC`/`_last_activity`(및 갱신 코드) 완전 제거, `main()` 단순화
2. [ ] 기존 `tests/test_lifecycle.py`에서 idle_watchdog 관련 테스트 제거(회귀 없음 확인 — 나머지 job reap/atexit 테스트는 유지)
3. [ ] `idle_culler.py` L1 순수 함수 작성: `is_ssh_agent_argv`, ppid 파싱, `process_age_seconds`(xcelium 로직 재사용)
4. [ ] `idle_culler.py` L2 `/proc` 연동 함수: `find_ssh_agent_pids`, `has_live_children`, `is_orphaned`
5. [ ] `_cull_if_eligible` + `main()` 조립(§4.1 pseudocode 그대로 구현)
6. [ ] `tests/test_idle_culler.py` 작성 (L1 전부, L2는 POSIX에서만)
7. [ ] `deploy/crontab.example` 작성(xcelium-mcp 패턴 참고, `*/5 * * * * /opt/mcp-env/bin/python3 /opt/ssh-mcp/idle_culler.py`)
8. [ ] cloud0에 `idle_culler.py` 배포 + crontab 등록(사용자 승인 후 수동 진행)
9. [ ] cloud0 L3 스모크(§8.4) — 특히 항목 3(age 백스톱 override)에서 실제 `ORPHAN_PPID` 재확인 병행

### 11.3 Session Guide

#### Module Map

| Module | Scope Key | Description | Estimated Turns |
|--------|-----------|--------------|:---:|
| ssh_agent.py 정리 | `module-1` | idle_watchdog 등 제거 + 기존 테스트 정리 | 15-20 |
| idle_culler.py 구현 | `module-2` | L1/L2 함수 + `_cull_if_eligible`/`main()` | 30-40 |
| 배포 준비 | `module-3` | crontab.example + cloud0 배포 절차 문서화 | 10-15 |

#### Recommended Session Plan

| Session | Phase | Scope | Turns |
|---------|-------|-------|:-----:|
| Session 1 | Plan + Design | 전체(cloud0 실측 포함, 본 세션) | 완료 |
| Session 2 | Do | `--scope module-1,module-2` | 45-60 |
| Session 3 | Do | `--scope module-3` + cloud0 배포(사용자 승인 필요) | 15-25 |
| Session 4 | Check + Report | 전체 | 25-35 |

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-07-08 | 초안 — Checkpoint 3에서 옵션 A(orphan+age 휴리스틱) 선택. cloud0 실측으로 xcelium-mcp `idle_culler.py`의 TCP 기반 판정(`has_established_tcp`)을 그대로 이식할 수 없음을 확인(ssh_agent.py는 TCP 소켓을 갖지 않고, sshd 조상의 fd/TCP 상태는 root 없이 읽을 수 없음) — Plan §7.2의 "이식 비용 낮음" 가정을 정정. 대신 부모 프로세스 생존(orphan) + age 백스톱 조합으로 설계. | hoseung.lee |
