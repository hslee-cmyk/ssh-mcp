"""
ssh_agent.py — MCP server for remote shell access via SSH stdio transport.
Run on the remote server: python3 ssh_agent.py
Register in ~/.claude.json:
  "ssh": { "command": "ssh", "args": ["-o","BatchMode=yes","cloud0",
           "/opt/mcp-env/bin/python3", "/opt/mcp-servers/ssh_agent.py"] }

Process lifecycle (idle self-exit, job reap, ssh_bg_kill):
  Design Ref: docs/02-design/features/ssh-mcp-process-lifecycle.design.md
"""

import asyncio
import atexit
import os
import subprocess
import sys
import time
import uuid

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

app = Server("ssh-agent")

# Background job registry: job_id -> {process, outfile, pidfile, finished_at}
_jobs: dict = {}


# ─── Lifecycle ──────────────────────────────────────────────────────────────
# Design Ref: §2 (Option A — self idle-exit), §3.2 (globals), §4.1 (ssh_bg_kill),
#             §6.1.2 (ssh_bg_poll fallback / _pid_alive)

try:
    IDLE_TIMEOUT_SEC = int(os.environ.get("SSH_MCP_IDLE_TIMEOUT_SEC", "1800"))
except ValueError:
    IDLE_TIMEOUT_SEC = 1800

WATCHDOG_INTERVAL_SEC = 60
JOB_RETENTION_SEC = 86400  # 24h — Design §3.2, FR-04
JOB_OUTFILE_TMPL = "/tmp/mcp_job_{job_id}.txt"
JOB_PIDFILE_TMPL = "/tmp/mcp_job_{job_id}.pid"

_last_activity = time.monotonic()
_main_task: asyncio.Task | None = None


def _pid_alive(job_id: str) -> bool:
    """Check OS-level process liveness via the PID sidecar file (no signal sent).
    Design Ref: §6.1.2 — shared by ssh_bg_poll fallback and ssh_bg_kill fallback.
    """
    pidfile = JOB_PIDFILE_TMPL.format(job_id=job_id)
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        # POSIX raises ProcessLookupError (an OSError subclass) for a dead pid;
        # Windows raises a plain OSError (WinError 87) instead — both mean "not alive".
        return False


def _kill_group(pid: int, sig: int) -> None:
    """Signal the whole process GROUP, not just the tracked pid.

    ssh_bg_run's Popen is a `bash -c "...; cmd; ..."` wrapper — the actual
    workload (e.g. a long simulation) runs as bash's CHILD. Signaling only the
    wrapper pid kills bash but leaves that child running, reparented to init —
    verified empirically (not caught by static Design review): killing the
    wrapper alone left a `sleep` child alive and still running afterward.
    `os.killpg` signals every process in the group at once. Requires
    `start_new_session=True` at Popen time (see ssh_bg_run) so the group is
    isolated to just this job's own tree.

    POSIX-only (`os.killpg`/process groups don't exist on Windows) — this
    project targets cloud0/Linux exclusively (Design §3.2), so on Windows
    (local dev only) this falls back to single-process kill.
    """
    if hasattr(os, "killpg"):
        os.killpg(pid, sig)
    else:
        os.kill(pid, sig)


def _terminate_sync(proc: subprocess.Popen, grace_sec: float = 5.0) -> str:
    """Blocking SIGTERM->SIGKILL escalation for atexit_handler (sync context).
    Design Ref: §7, FR-03.
    """
    _kill_group(proc.pid, 15)  # SIGTERM
    try:
        proc.wait(timeout=grace_sec)
        return "graceful"
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid, 9)  # SIGKILL
        proc.wait(timeout=grace_sec)
        return "forced"


async def _kill_pid_async(pid: int, proc: subprocess.Popen | None, grace_sec: float = 5.0) -> str:
    """Async SIGTERM->SIGKILL escalation for the ssh_bg_kill tool. Works with or
    without a Popen handle — the fallback path only has a PID recovered from the
    sidecar file. Design Ref: §4.1, §6.1.2.
    """

    def _alive() -> bool:
        if proc is not None:
            return proc.poll() is None
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            # See _pid_alive() — POSIX: ProcessLookupError, Windows: plain OSError.
            return False

    try:
        _kill_group(pid, 15)  # SIGTERM
    except OSError:
        return "graceful"

    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        if not _alive():
            return "graceful"
        await asyncio.sleep(0.2)

    try:
        _kill_group(pid, 9)  # SIGKILL
    except OSError:
        pass
    if proc is not None:
        try:
            proc.wait(timeout=grace_sec)
        except subprocess.TimeoutExpired:
            pass
    return "forced"


def job_sweep() -> None:
    """Reap finished children (zombie prevention, FR-02) and clean up entries that
    finished more than JOB_RETENTION_SEC ago (FR-04). Runs every WATCHDOG_INTERVAL_SEC
    from idle_watchdog(). Design Ref: §2.1, §3.2.
    """
    now = time.monotonic()
    stale = []
    for job_id, job in _jobs.items():
        try:
            if job["finished_at"] is None and job["process"].poll() is not None:
                job["finished_at"] = now
        except Exception as e:
            print(f"[ssh-mcp] job_sweep: error polling {job_id}: {e}", file=sys.stderr)
            job["finished_at"] = now

        if job["finished_at"] is not None and (now - job["finished_at"]) > JOB_RETENTION_SEC:
            stale.append(job_id)

    for job_id in stale:
        job = _jobs.pop(job_id)
        for path in (job["outfile"], job["pidfile"]):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                print(f"[ssh-mcp] job_sweep: cleanup failed for {path}: {e}", file=sys.stderr)
        print(f"[ssh-mcp] reaped stale job {job_id} (retained {JOB_RETENTION_SEC}s past completion)", file=sys.stderr)


def _has_running_job() -> bool:
    return any(job["finished_at"] is None for job in _jobs.values())


async def idle_watchdog() -> None:
    """Self-exit when idle and no job is running. Design Ref: §1.1, §2.1 (Option A).
    Cancels the main task (running inside stdio_server's async-with) so that the
    stdio transport closes cleanly and the interpreter reaches a normal shutdown
    (atexit fires — unlike a signal-based kill).
    """
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL_SEC)
        job_sweep()
        idle_for = time.monotonic() - _last_activity
        if idle_for > IDLE_TIMEOUT_SEC and not _has_running_job():
            print(f"[ssh-mcp] idle timeout ({IDLE_TIMEOUT_SEC}s) — exiting", file=sys.stderr)
            if _main_task is not None:
                _main_task.cancel()
            return


def atexit_handler() -> None:
    """On process exit, terminate any job still tracked as running. Design Ref: §7, FR-03.
    Known limitation: SIGKILL (and SIGTERM without a custom handler) bypass atexit —
    see Design §6.1/§6.1.1 for the accepted residual risk.
    """
    for job_id, job in list(_jobs.items()):
        if job["finished_at"] is None and job["process"].poll() is None:
            try:
                result = _terminate_sync(job["process"])
                print(f"[ssh-mcp] atexit: terminated job {job_id} ({result})", file=sys.stderr)
            except Exception as e:
                print(f"[ssh-mcp] atexit: failed to terminate {job_id}: {e}", file=sys.stderr)


# ─── Tool 목록 ────────────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools():
    return [
        Tool(
            name="ssh_run",
            description=(
                "Run a bash command on this server and return stdout+stderr. "
                "Use for short commands (< timeout seconds)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Bash command to execute"},
                    "cwd":     {"type": "string",  "description": "Working directory (default: ~)"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default: 30)"},
                },
                "required": ["command"],
            },
        ),
        Tool(
            name="ssh_bg_run",
            description=(
                "Start a long-running command in the background. "
                "Returns a job_id. Use ssh_bg_poll to check progress."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Bash command to run in background"},
                    "cwd":     {"type": "string",  "description": "Working directory (default: ~)"},
                },
                "required": ["command"],
            },
        ),
        Tool(
            name="ssh_bg_poll",
            description="Poll a background job started by ssh_bg_run. Returns status and output so far.",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Job ID returned by ssh_bg_run"},
                },
                "required": ["job_id"],
            },
        ),
        Tool(
            name="ssh_bg_kill",
            description=(
                "Cancel a background job started by ssh_bg_run, by job_id. "
                "Sends SIGTERM, escalates to SIGKILL after 5s if still alive. "
                "Works even after this server process has restarted (e.g. after an SSH reconnect)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Job ID returned by ssh_bg_run"},
                },
                "required": ["job_id"],
            },
        ),
        Tool(
            name="file_read",
            description="Read the contents of a file on this server.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or ~-relative file path"},
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="file_write",
            description="Write (overwrite) content to a file on this server.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "Absolute or ~-relative file path"},
                    "content": {"type": "string", "description": "Text content to write"},
                },
                "required": ["path", "content"],
            },
        ),
        Tool(
            name="file_ls",
            description="List contents of a directory on this server.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (default: current dir)"},
                },
            },
        ),
        Tool(
            name="file_grep",
            description="Search for a pattern in files on this server.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Grep pattern"},
                    "path":    {"type": "string", "description": "File or directory to search"},
                    "flags":   {"type": "string", "description": "Grep flags (default: -rn)"},
                },
                "required": ["pattern", "path"],
            },
        ),
    ]


# ─── Tool 구현 ────────────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    global _last_activity
    _last_activity = time.monotonic()

    # ── ssh_run ──────────────────────────────────────────────────────────────
    if name == "ssh_run":
        cwd     = arguments.get("cwd", "~")
        timeout = arguments.get("timeout", 30)
        cmd     = f"cd {cwd} && {arguments['command']}"
        try:
            r = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out = r.stdout
            if r.stderr:
                out += f"\n[stderr]\n{r.stderr}"
            if r.returncode != 0:
                out += f"\n[exit {r.returncode}]"
        except subprocess.TimeoutExpired:
            out = f"[timeout after {timeout}s]"
        except Exception as e:
            out = f"[error] {e}"
        return [TextContent(type="text", text=out or "(no output)")]

    # ── ssh_bg_run ────────────────────────────────────────────────────────────
    elif name == "ssh_bg_run":
        job_id  = str(uuid.uuid4())[:8]
        outfile = JOB_OUTFILE_TMPL.format(job_id=job_id)
        pidfile = JOB_PIDFILE_TMPL.format(job_id=job_id)
        cwd     = arguments.get("cwd", "~")
        cmd     = (
            f"cd {cwd} && "
            f"{{ {arguments['command']}; }} >& {outfile}; "
            f"echo MCP_DONE_{job_id} >> {outfile}"
        )
        # start_new_session=True isolates this job into its own process group
        # (pgid == pid) so _kill_group() can signal the whole tree, not just
        # the bash wrapper — see _kill_group() docstring.
        proc = subprocess.Popen(["bash", "-c", cmd], start_new_session=True)
        try:
            with open(pidfile, "w") as f:
                f.write(str(proc.pid))
        except Exception as e:
            print(f"[ssh-mcp] ssh_bg_run: failed to write pidfile for {job_id}: {e}", file=sys.stderr)
        _jobs[job_id] = {
            "process": proc,
            "outfile": outfile,
            "pidfile": pidfile,
            "finished_at": None,
        }
        return [TextContent(type="text", text=f"job_id: {job_id}\noutput: {outfile}")]

    # ── ssh_bg_poll ───────────────────────────────────────────────────────────
    elif name == "ssh_bg_poll":
        job_id = arguments["job_id"]
        job    = _jobs.get(job_id)

        if job:
            done = job["process"].poll() is not None
            if done and job["finished_at"] is None:
                job["finished_at"] = time.monotonic()
            outfile = job["outfile"]
            output = ""
            if os.path.exists(outfile):
                with open(outfile) as f:
                    output = f.read()
            status = "done" if done else "running"
            return [TextContent(type="text", text=f"[{status}]\n{output}")]

        # Fallback (Design §6.1.2): parent may have restarted, _jobs is empty.
        # outfile survives on disk independent of the parent's memory.
        outfile = JOB_OUTFILE_TMPL.format(job_id=job_id)
        if not os.path.exists(outfile):
            return [TextContent(type="text", text=f"Unknown job: {job_id}")]
        with open(outfile) as f:
            content = f.read()
        if f"MCP_KILLED_{job_id}" in content:
            return [TextContent(type="text", text=f"[killed] (recovered from disk)\n{content}")]
        done = f"MCP_DONE_{job_id}" in content or not _pid_alive(job_id)
        return [TextContent(
            type="text",
            text=f"[{'done' if done else 'running'}] (recovered from disk)\n{content}",
        )]

    # ── ssh_bg_kill ───────────────────────────────────────────────────────────
    elif name == "ssh_bg_kill":
        job_id = arguments["job_id"]
        job    = _jobs.get(job_id)

        if job:
            if job["finished_at"] is not None or job["process"].poll() is not None:
                return [TextContent(type="text", text=f"[no-op] job_id: {job_id} already finished")]
            result  = await _kill_pid_async(job["process"].pid, job["process"])
            job["finished_at"] = time.monotonic()
            outfile = job["outfile"]
        else:
            # Fallback (Design §4.1): parent may have restarted, _jobs is empty.
            outfile = JOB_OUTFILE_TMPL.format(job_id=job_id)
            pidfile = JOB_PIDFILE_TMPL.format(job_id=job_id)
            if not os.path.exists(pidfile):
                if os.path.exists(outfile):
                    return [TextContent(type="text", text=f"[no-op] job_id: {job_id} already finished")]
                return [TextContent(type="text", text=f"Unknown job: {job_id}")]
            if not _pid_alive(job_id):
                return [TextContent(type="text", text=f"[no-op] job_id: {job_id} already finished")]
            try:
                with open(pidfile) as f:
                    pid = int(f.read().strip())
            except (ValueError, OSError) as e:
                return [TextContent(type="text", text=f"[error] could not read pidfile for {job_id}: {e}")]
            result = await _kill_pid_async(pid, None)

        try:
            with open(outfile, "a") as f:
                f.write(f"\nMCP_KILLED_{job_id}\n")
        except Exception as e:
            print(f"[ssh-mcp] ssh_bg_kill: failed to mark outfile for {job_id}: {e}", file=sys.stderr)

        label = "SIGTERM, graceful" if result == "graceful" else "SIGKILL, forced after 5s timeout"
        return [TextContent(type="text", text=f"[killed] job_id: {job_id} ({label})")]

    # ── file_read ─────────────────────────────────────────────────────────────
    elif name == "file_read":
        path = os.path.expanduser(arguments["path"])
        try:
            with open(path) as f:
                return [TextContent(type="text", text=f.read())]
        except Exception as e:
            return [TextContent(type="text", text=f"[error] {e}")]

    # ── file_write ────────────────────────────────────────────────────────────
    elif name == "file_write":
        path = os.path.expanduser(arguments["path"])
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                f.write(arguments["content"])
            return [TextContent(type="text", text=f"Written: {path}")]
        except Exception as e:
            return [TextContent(type="text", text=f"[error] {e}")]

    # ── file_ls ───────────────────────────────────────────────────────────────
    elif name == "file_ls":
        path = os.path.expanduser(arguments.get("path", "."))
        try:
            entries = sorted(os.listdir(path))
            return [TextContent(type="text", text="\n".join(entries))]
        except Exception as e:
            return [TextContent(type="text", text=f"[error] {e}")]

    # ── file_grep ─────────────────────────────────────────────────────────────
    elif name == "file_grep":
        flags   = arguments.get("flags", "-rn")
        pattern = arguments["pattern"]
        path    = arguments["path"]
        cmd     = f"grep {flags} {pattern!r} {path}"
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        result  = r.stdout or "(no match)"
        if r.stderr:
            result += f"\n[stderr]\n{r.stderr}"
        return [TextContent(type="text", text=result)]

    # ── unknown ───────────────────────────────────────────────────────────────
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ─── 엔트리포인트 ─────────────────────────────────────────────────────────────

async def main():
    global _main_task
    _main_task = asyncio.current_task()
    watchdog_task = asyncio.create_task(idle_watchdog())
    try:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )
    except asyncio.CancelledError:
        pass
    finally:
        watchdog_task.cancel()

if __name__ == "__main__":
    atexit.register(atexit_handler)
    asyncio.run(main())
