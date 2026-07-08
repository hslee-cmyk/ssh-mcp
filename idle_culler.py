"""idle_culler.py — ssh-mcp용 orphan+age 휴리스틱 (cloud0 cron 전용).

xcelium-mcp의 idle_culler.py가 쓰는 has_established_tcp() 기반 판정을 그대로
이식하지 않는다 — ssh_agent.py는 자신의 TCP 소켓을 갖지 않고(stdio 통신), TCP
소켓을 쥔 sshd 조상 프로세스의 fd/TCP 상태는 root 없이는 읽을 수 없음이 cloud0
실측으로 확인됨(design.md §2.0). 대신 (a) 부모 프로세스 생존 여부(orphan 감지)와
(b) age 임계값(백스톱)을 사용한다.

Design Ref: docs/02-design/features/ssh-mcp-idle-culler.design.md §4.1
"""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

AGENT_CMDLINE_MARKER = b"ssh_agent.py"
IDLE_THRESHOLD_SEC = int(os.environ.get("SSH_MCP_CULLER_IDLE_THRESHOLD_SEC", 6 * 3600))
KILL_GRACE_SEC = 5
ORPHAN_PPID = 1


# ---------------------------------------------------------------------------
# Pure parsing helpers — no /proc access, unit-testable on any platform.
# ---------------------------------------------------------------------------


def parse_cmdline_argv(cmdline_bytes: bytes) -> list[bytes]:
    """Split raw /proc/<pid>/cmdline content (NUL-separated) into argv tokens."""
    return [a for a in cmdline_bytes.split(b"\x00") if a]


def is_ssh_agent_argv(argv: list[bytes]) -> bool:
    """Whether argv is really `python3 .../ssh_agent.py`, not just a process whose
    cmdline happens to contain that string as a substring (e.g. `grep ssh_agent.py
    /var/log/x`). Matches on argv[-1]'s basename exactly, mirroring xcelium-mcp's
    find_supervisor_pid() flock-wrapper lesson (design.md §4.1)."""
    return bool(argv) and Path(argv[-1].decode(errors="replace")).name == "ssh_agent.py"


def parse_stat_ppid(stat_text: str) -> int:
    """Extract ppid from /proc/<pid>/stat content.

    Format: "pid (comm) state ppid ...". comm can itself contain spaces/parens,
    so skip past the *last* ')' rather than splitting naively (same technique as
    xcelium-mcp's parse_stat_starttime()). ppid is the 2nd field after ')'.
    """
    _, _, rest = stat_text.rpartition(")")
    fields = rest.split()
    return int(fields[1])


def parse_uptime_seconds(uptime_text: str) -> float:
    """Extract system uptime (seconds) from /proc/uptime content."""
    return float(uptime_text.split()[0])


def parse_stat_starttime(stat_text: str) -> int:
    """Extract starttime (clock ticks since boot) from /proc/<pid>/stat content.
    Reused verbatim from xcelium-mcp idle_culler.py (design.md §4.1)."""
    _, _, rest = stat_text.rpartition(")")
    fields = rest.split()
    return int(fields[19])


# ---------------------------------------------------------------------------
# /proc-backed lookups — Linux only, exercised on cloud0.
# ---------------------------------------------------------------------------


def find_ssh_agent_pids() -> list[int]:
    """Scan /proc/*/cmdline for running ssh_agent.py processes."""
    pids: list[int] = []
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = parse_cmdline_argv((entry / "cmdline").read_bytes())
        except OSError:
            continue  # pid exited between listdir() and read — not fatal
        if is_ssh_agent_argv(argv):
            pids.append(int(entry.name))
    return pids


def has_live_children(pid: int) -> bool:
    """Whether pid has any live child process (i.e. a ssh_bg_run job is running)."""
    children_path = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        text = children_path.read_text().strip()
    except OSError:
        return False
    return bool(text)


def is_orphaned(pid: int) -> bool:
    """Whether pid's parent has died (reparented to ORPHAN_PPID, default 1)."""
    stat_text = Path(f"/proc/{pid}/stat").read_text()
    return parse_stat_ppid(stat_text) == ORPHAN_PPID


def process_age_seconds(pid: int) -> float:
    """Reused verbatim from xcelium-mcp idle_culler.py (design.md §4.1)."""
    stat_text = Path(f"/proc/{pid}/stat").read_text()
    uptime_text = Path("/proc/uptime").read_text()
    starttime_ticks = parse_stat_starttime(stat_text)
    clk_tck = os.sysconf("SC_CLK_TCK")
    return parse_uptime_seconds(uptime_text) - (starttime_ticks / clk_tck)


# ---------------------------------------------------------------------------
# Cull decision + entry point
# ---------------------------------------------------------------------------


def _cull_if_eligible(pid: int) -> None:
    if has_live_children(pid):
        return  # a ssh_bg_run job is running — always preserved

    orphaned = is_orphaned(pid)
    if not orphaned and process_age_seconds(pid) <= IDLE_THRESHOLD_SEC:
        return  # live session, not yet past the age backstop

    reason = "orphaned" if orphaned else f"age-backstop age={int(process_age_seconds(pid))}s"

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    time.sleep(KILL_GRACE_SEC)
    try:
        os.kill(pid, 0)  # still alive?
    except ProcessLookupError:
        print(f"[idle-culler] {reason} pid={pid} — killed", file=sys.stderr)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    print(f"[idle-culler] {reason} pid={pid} — killed (forced)", file=sys.stderr)


def main() -> int:
    if sys.platform == "win32":
        print("ssh-mcp-idle-culler requires /proc — Linux/cloud0 only.", file=sys.stderr)
        return 1
    for pid in find_ssh_agent_pids():
        try:
            _cull_if_eligible(pid)
        except (OSError, ValueError, IndexError):
            continue  # scan/kill race (pid exited in between) — not fatal
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
