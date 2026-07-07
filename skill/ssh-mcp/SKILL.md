---
name: ssh-mcp
description: |
  `mcp__ssh__*` 툴(ssh_run, ssh_bg_run, ssh_bg_poll, ssh_bg_kill, file_read, file_write, file_ls,
  file_grep)로 원격 서버(cloud0)에 명령을 실행하거나 파일을 다루는 모든 세션에서 사용. FPGA/RTL
  작업에 국한되지 않고, 원격 빌드/배포/로그 확인/장시간 job 관리 등 일반적인 원격 작업에도 적용된다.
  특히 (1) 명령이 몇 초 안에 끝나지 않을 것 같을 때 ssh_run 대신 ssh_bg_run+ssh_bg_poll을 써야 하는
  판단, (2) 백그라운드 job이 세션/서버 재시작을 넘나들며 살아남는 이유, (3) job 정리 시점 판단에
  반드시 참고할 것.
  트리거: ssh-mcp, mcp__ssh__, ssh_run, ssh_bg_run, ssh_bg_poll, ssh_bg_kill, cloud0,
    원격 서버, 원격 명령, 원격 파일, 백그라운드 job, job_id, remote shell, remote command.
argument-hint: ""
user-invocable: false
---

# ssh-mcp

`ssh` MCP 서버(이 저장소의 `ssh_agent.py`, 원격 호스트 `cloud0`에서 SSH stdio transport로 구동)가
제공하는 8개 툴을 언제·어떻게 쓸지 안내한다. 이 서버는 `~/.claude.json`에 사용자 레벨로 등록되어
있어 어떤 프로젝트 세션에서든 붙는다 — RTL/FPGA 전용이 아니다.

소스: `C:\Users\HSLEE\Documents\Todoc\fpga\ssh-mcp\ssh_agent.py`
설계 문서: `docs/02-design/features/ssh-mcp-process-lifecycle.design.md`

## 1. 명령 실행: ssh_run vs ssh_bg_run+poll

| 상황 | 툴 |
|------|-----|
| 몇 초~수십 초 안에 끝나는 명령 (ls, git status, 짧은 빌드 스텝) | `ssh_run` |
| 끝나는 시점을 모르거나 분 단위 이상 걸리는 명령 (긴 빌드, regression, 대용량 전송) | `ssh_bg_run` → `ssh_bg_poll` 반복 |

- `ssh_run`은 `subprocess.run(..., timeout=...)`으로 동기 실행되며 **기본 timeout은 30초**다.
  timeout을 늘려서 오래 기다리게 하지 말 것 — 그동안 세션이 막히고, 중간 진행 상황도 볼 수 없다.
  대신 오래 걸릴 것 같으면 처음부터 `ssh_bg_run`을 써라.
- `ssh_bg_run`은 즉시 `job_id`를 반환한다. 이후 `ssh_bg_poll(job_id)`로 상태(`running`/`done`)와
  누적 출력을 확인한다. 한 번 poll해서 `running`이면, 잠시 후 다시 poll하면 된다 — busy-wait로
  연달아 poll하지 말고 실제로 걸릴 만한 시간만큼 간격을 두고 확인한다.
- 다 쓴 job은 `ssh_bg_kill(job_id)`로 정리한다. 내부적으로 프로세스 그룹 전체(`os.killpg`)에
  SIGTERM → 5초 후 SIGKILL을 보내므로, bash 래퍼 밑에서 실행 중인 실제 워크로드(예: 시뮬레이션
  자식 프로세스)까지 확실히 종료된다 — 이건 서버가 알아서 처리하므로 별도로 pkill 등을 할 필요 없다.

## 2. 세션이 끊기거나 서버가 재시작해도 job은 안전하다

- `ssh` 서버 프로세스는 **30분(`SSH_MCP_IDLE_TIMEOUT_SEC`, 기본 1800초) 동안 툴 호출이 없으면
  스스로 종료**한다. 다음 호출 시 SSH가 새 프로세스를 자동으로 띄워주므로 사용자 입장에서는
  아무 조치도 필요 없다 — 다만 그 프로세스의 메모리 상태(`_jobs` dict)는 사라진다.
- 그래도 백그라운드 job은 안전하다: job의 출력은 `/tmp/mcp_job_{job_id}.txt`, PID는
  `/tmp/mcp_job_{job_id}.pid`에 디스크로 기록된다. `ssh_bg_poll`/`ssh_bg_kill`은 메모리에 job이
  없으면 이 파일들을 읽어 복구한다 — 즉 어제 시작한 job의 `job_id`를 오늘 새 세션에서 poll하거나
  kill해도 정상 동작한다.
- 완료된 job의 sidecar 파일은 완료 후 24시간(`JOB_RETENTION_SEC`) 지나면 자동 정리된다. 그 전이면
  언제든 `ssh_bg_poll`로 지난 출력을 다시 볼 수 있다.

## 3. 파일 작업은 file_* 툴을 우선 사용

`ssh_run`으로 `cat`/`echo >`/`ls`/`grep`을 흉내내지 말고, 전용 툴을 써라 — 셸 이스케이핑 문제를
피하고 의도가 더 명확해진다.

| 목적 | 툴 |
|------|-----|
| 파일 읽기 | `file_read(path)` |
| 파일 쓰기(덮어쓰기) | `file_write(path, content)` |
| 디렉토리 목록 | `file_ls(path)` |
| 패턴 검색 | `file_grep(pattern, path, flags="-rn")` |

`file_grep`은 내부적으로 `grep {flags} {pattern!r} {path}`를 셸에서 그대로 실행한다 — `pattern`에
셸 메타문자가 들어가면 예상과 다르게 동작할 수 있으니, 복잡한 정규식보다는 단순 패턴에 쓰는 것이
안전하다.

## 4. 트리거 판단 기준

- `mcp__ssh__*` 툴이 실제로 화면에 등장하거나, 원격 호스트(cloud0)에서 명령/파일을 다뤄야 하는
  맥락이면 이 skill을 로드한다 — RTL 프로젝트 여부와 무관하다.
- xcelium-mcp(시뮬레이터 제어)와는 다른 서버다 — xcelium 관련 작업은 `xcelium-sim` skill을 따로
  참조한다. 두 skill이 같은 세션에서 동시에 활성화될 수 있다.
