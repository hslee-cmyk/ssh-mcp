# L2 lifecycle unit tests — Design §8.3. Uses mock Popen objects for pure logic
# (idle condition, job sweep, atexit) and real bash subprocesses for ssh_bg_kill
# (Design §8.2 items 7-10), since kill behavior depends on real OS signal delivery.
import asyncio
import os
import subprocess
import time

import pytest

import ssh_agent
from ssh_agent import call_tool


def run(coro):
    return asyncio.run(coro)


class FakeProc:
    """Minimal subprocess.Popen stand-in for pure-logic lifecycle tests."""

    def __init__(self, running=True, pid=4242):
        self._running = running
        self.pid = pid
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._running else 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        if self._running:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return 0

    def finish(self):
        self._running = False


# ─── idle condition / job bookkeeping ──────────────────────────────────────

def test_has_running_job_true_when_unfinished():
    ssh_agent._jobs["j1"] = {"process": FakeProc(), "outfile": "x", "pidfile": "y", "finished_at": None}
    assert ssh_agent._has_running_job() is True


def test_has_running_job_false_when_all_finished():
    ssh_agent._jobs["j1"] = {
        "process": FakeProc(running=False), "outfile": "x", "pidfile": "y",
        "finished_at": time.monotonic(),
    }
    assert ssh_agent._has_running_job() is False


def test_has_running_job_false_when_empty():
    assert ssh_agent._has_running_job() is False


# ─── idle_watchdog exit trigger ────────────────────────────────────────────
#
# Regression test for a bug found on cloud0: an earlier version called
# _main_task.cancel() here, expecting CancelledError to unwind cleanly through
# stdio_server()'s `async with` — verified live that the process never
# actually terminated even though this exact log line printed on schedule.
# Switched to os._exit(0) (safe here because the trigger condition already
# guarantees no job is running). Mock os._exit so the test process itself
# doesn't die, and use it to break out of idle_watchdog's `while True` loop.

def test_idle_watchdog_calls_os_exit_when_idle_and_no_job(monkeypatch):
    monkeypatch.setattr(ssh_agent, "WATCHDOG_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(ssh_agent, "IDLE_TIMEOUT_SEC", 0)
    ssh_agent._last_activity = time.monotonic() - 100  # already idle

    calls = []

    class _StopLoop(Exception):
        pass

    def fake_exit(code):
        calls.append(code)
        raise _StopLoop()

    monkeypatch.setattr(os, "_exit", fake_exit)

    with pytest.raises(_StopLoop):
        run(ssh_agent.idle_watchdog())

    assert calls == [0]


def test_idle_watchdog_does_not_exit_while_job_running(monkeypatch):
    monkeypatch.setattr(ssh_agent, "WATCHDOG_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(ssh_agent, "IDLE_TIMEOUT_SEC", 0)
    ssh_agent._last_activity = time.monotonic() - 100
    ssh_agent._jobs["j1"] = {"process": FakeProc(running=True), "outfile": "x", "pidfile": "y", "finished_at": None}

    calls = []
    monkeypatch.setattr(os, "_exit", lambda code: calls.append(code))

    async def run_briefly():
        task = asyncio.ensure_future(ssh_agent.idle_watchdog())
        await asyncio.sleep(0.05)  # a few ticks
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    run(run_briefly())
    assert calls == []


# ─── job_sweep: zombie reap (FR-02) + retention cleanup (FR-04) ───────────

def test_job_sweep_marks_finished_and_reaps_zombie():
    proc = FakeProc(running=False)
    ssh_agent._jobs["j1"] = {"process": proc, "outfile": "nonexistent.txt", "pidfile": "nonexistent.pid", "finished_at": None}
    ssh_agent.job_sweep()
    assert ssh_agent._jobs["j1"]["finished_at"] is not None


def test_job_sweep_keeps_running_job():
    proc = FakeProc(running=True)
    ssh_agent._jobs["j1"] = {"process": proc, "outfile": "x", "pidfile": "y", "finished_at": None}
    ssh_agent.job_sweep()
    assert ssh_agent._jobs["j1"]["finished_at"] is None
    assert "j1" in ssh_agent._jobs


def test_job_sweep_cleans_up_after_retention(tmp_path, monkeypatch):
    outfile = tmp_path / "out.txt"
    pidfile = tmp_path / "out.pid"
    outfile.write_text("done")
    pidfile.write_text("123")
    monkeypatch.setattr(ssh_agent, "JOB_RETENTION_SEC", 0)
    ssh_agent._jobs["j1"] = {
        "process": FakeProc(running=False),
        "outfile": str(outfile),
        "pidfile": str(pidfile),
        "finished_at": time.monotonic() - 10,
    }
    ssh_agent.job_sweep()
    assert "j1" not in ssh_agent._jobs
    assert not outfile.exists()
    assert not pidfile.exists()


def test_job_sweep_retains_recently_finished_job(tmp_path):
    outfile = tmp_path / "out.txt"
    outfile.write_text("done")
    ssh_agent._jobs["j1"] = {
        "process": FakeProc(running=False),
        "outfile": str(outfile),
        "pidfile": str(tmp_path / "out.pid"),
        "finished_at": time.monotonic(),  # just finished
    }
    ssh_agent.job_sweep()
    assert "j1" in ssh_agent._jobs
    assert outfile.exists()


# ─── atexit_handler (FR-03) ─────────────────────────────────────────────

def test_atexit_handler_terminates_running_job(monkeypatch):
    # _terminate_sync now signals the process GROUP via _kill_group() (Design
    # discovery: signaling only the tracked pid orphans its children — e.g. the
    # 'sleep' inside a bash wrapper survives a plain os.kill on the wrapper).
    # Stub _kill_group and have it mark the fake process finished, so we can
    # assert atexit_handler invokes it with the right pid/signal without
    # depending on a real OS process tree.
    calls = []

    def fake_kill_group(pid, sig):
        calls.append((pid, sig))
        proc.finish()

    monkeypatch.setattr(ssh_agent, "_kill_group", fake_kill_group)
    proc = FakeProc(running=True, pid=4242)
    ssh_agent._jobs["j1"] = {"process": proc, "outfile": "x", "pidfile": "y", "finished_at": None}
    ssh_agent.atexit_handler()
    assert calls == [(4242, 15)]  # SIGTERM


def test_atexit_handler_skips_finished_job():
    proc = FakeProc(running=False)
    ssh_agent._jobs["j1"] = {"process": proc, "outfile": "x", "pidfile": "y", "finished_at": time.monotonic()}
    ssh_agent.atexit_handler()
    assert proc.terminated is False


# ─── _pid_alive (§6.1.2) ────────────────────────────────────────────────

def test_pid_alive_true_for_current_process(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh_agent, "JOB_PIDFILE_TMPL", str(tmp_path / "mcp_job_{job_id}.pid"))
    (tmp_path / "mcp_job_j1.pid").write_text(str(os.getpid()))
    assert ssh_agent._pid_alive("j1") is True


def test_pid_alive_false_for_nonexistent_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh_agent, "JOB_PIDFILE_TMPL", str(tmp_path / "mcp_job_{job_id}.pid"))
    (tmp_path / "mcp_job_j1.pid").write_text("999999")
    assert ssh_agent._pid_alive("j1") is False


def test_pid_alive_false_when_pidfile_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh_agent, "JOB_PIDFILE_TMPL", str(tmp_path / "mcp_job_{job_id}.pid"))
    assert ssh_agent._pid_alive("nope") is False


# ─── ssh_bg_kill: SIGKILL escalation (Design §8.2 #10) ─────────────────────
#
# A real end-to-end test would start `bash -c "trap '' TERM; sleep 60"` and
# confirm SIGTERM is ignored so the code must escalate to SIGKILL. That is
# untestable on this Windows/MSYS2 dev machine: Python's os.kill(pid, 15) on
# Windows calls TerminateProcess() directly, a forceful native termination
# that bypasses MSYS2 bash's userspace trap handling entirely (verified
# empirically — trap '' TERM did not delay termination at all). This gap only
# exists locally; cloud0 (real Linux, real signals) is unaffected. So instead
# we test the escalation *control flow* itself — grace period elapses with no
# death observed -> SIGKILL is sent -> "forced" is returned — with a process
# double that never reports itself dead, independent of real OS signals.
# Real end-to-end trap verification is deferred to the Design §8.4 cloud0
# manual smoke.

def test_kill_pid_async_escalates_to_sigkill_when_unresponsive(monkeypatch):
    calls = []
    monkeypatch.setattr(ssh_agent, "_kill_group", lambda pid, sig: calls.append(sig))
    proc = FakeProc(running=True, pid=4242)  # never reports itself finished

    result = run(ssh_agent._kill_pid_async(4242, proc, grace_sec=0.3))

    assert result == "forced"
    assert calls == [15, 9]  # SIGTERM, then SIGKILL


def test_kill_pid_async_graceful_when_process_dies(monkeypatch):
    calls = []
    proc = FakeProc(running=True, pid=4242)

    def fake_kill_group(pid, sig):
        calls.append(sig)
        proc.finish()  # simulate the signal actually working

    monkeypatch.setattr(ssh_agent, "_kill_group", fake_kill_group)

    result = run(ssh_agent._kill_pid_async(4242, proc, grace_sec=5.0))

    assert result == "graceful"
    assert calls == [15]  # SIGTERM


# ─── ssh_bg_kill tool (Design §4.1, §8.2 #7-9) — real bash subprocesses ──

def test_ssh_bg_kill_running_job(bash_tmp, monkeypatch):
    win_dir, bash_dir = bash_tmp
    monkeypatch.setattr(ssh_agent, "JOB_OUTFILE_TMPL", bash_dir + "/mcp_job_{job_id}.txt")
    monkeypatch.setattr(ssh_agent, "JOB_PIDFILE_TMPL", bash_dir + "/mcp_job_{job_id}.pid")

    started = run(call_tool("ssh_bg_run", {"command": "sleep 60"}))
    job_id = started[0].text.split("job_id: ")[1].split("\n")[0]

    killed = run(call_tool("ssh_bg_kill", {"job_id": job_id}))
    assert "[killed]" in killed[0].text
    assert ssh_agent._jobs[job_id]["finished_at"] is not None

    outfile = win_dir / f"mcp_job_{job_id}.txt"
    assert f"MCP_KILLED_{job_id}" in outfile.read_text()


def test_ssh_bg_kill_already_finished(bash_tmp, monkeypatch):
    win_dir, bash_dir = bash_tmp
    monkeypatch.setattr(ssh_agent, "JOB_OUTFILE_TMPL", bash_dir + "/mcp_job_{job_id}.txt")
    monkeypatch.setattr(ssh_agent, "JOB_PIDFILE_TMPL", bash_dir + "/mcp_job_{job_id}.pid")

    started = run(call_tool("ssh_bg_run", {"command": "echo done-fast"}))
    job_id = started[0].text.split("job_id: ")[1].split("\n")[0]

    for _ in range(50):
        polled = run(call_tool("ssh_bg_poll", {"job_id": job_id}))
        if polled[0].text.startswith("[done]"):
            break
        time.sleep(0.1)
    else:
        pytest.fail("job did not complete in time")

    result = run(call_tool("ssh_bg_kill", {"job_id": job_id}))
    assert "[no-op]" in result[0].text and "already finished" in result[0].text


def test_ssh_bg_kill_unknown_job_id():
    result = run(call_tool("ssh_bg_kill", {"job_id": "doesnotexist"}))
    assert "Unknown job" in result[0].text


def test_ssh_bg_kill_fallback_after_parent_restart(bash_tmp, monkeypatch):
    """Simulates an SSH reconnect: the job was started, then _jobs is cleared
    (as if the parent restarted), but the pidfile survives on disk — Design §4.1
    fallback path must still be able to kill it."""
    win_dir, bash_dir = bash_tmp
    monkeypatch.setattr(ssh_agent, "JOB_OUTFILE_TMPL", bash_dir + "/mcp_job_{job_id}.txt")
    monkeypatch.setattr(ssh_agent, "JOB_PIDFILE_TMPL", bash_dir + "/mcp_job_{job_id}.pid")

    started = run(call_tool("ssh_bg_run", {"command": "sleep 60"}))
    job_id = started[0].text.split("job_id: ")[1].split("\n")[0]

    ssh_agent._jobs.clear()  # simulate parent restart losing in-memory state

    killed = run(call_tool("ssh_bg_kill", {"job_id": job_id}))
    assert "[killed]" in killed[0].text
    outfile = win_dir / f"mcp_job_{job_id}.txt"
    assert f"MCP_KILLED_{job_id}" in outfile.read_text()
