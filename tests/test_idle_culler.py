# idle_culler.py unit tests — Design §8.2 (L1, platform-agnostic) / §8.3 (L2,
# POSIX-only /proc integration). Design Ref:
# docs/02-design/features/ssh-mcp-idle-culler.design.md
import subprocess
import sys
import time

import pytest

import idle_culler

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="/proc is Linux-only")


# ─── L1: pure parsing functions (Design §8.2) ──────────────────────────────

def test_is_ssh_agent_argv_true_for_real_invocation():
    argv = [b"python3", b"/opt/ssh-mcp/ssh_agent.py"]
    assert idle_culler.is_ssh_agent_argv(argv) is True


def test_is_ssh_agent_argv_false_for_grep_substring_match():
    argv = [b"bash", b"-c", b"grep ssh_agent.py /var/log/x"]
    assert idle_culler.is_ssh_agent_argv(argv) is False


def test_is_ssh_agent_argv_false_for_empty_argv():
    assert idle_culler.is_ssh_agent_argv([]) is False


def test_parse_stat_ppid_handles_comm_with_spaces_and_parens():
    # pid=100, comm="(my proc)" (space + parens inside comm), state=S, ppid=1
    stat_text = "100 (my proc) S 1 100 100 0 -1 4194304 0 0 0 0 0 0 0 0 20 0 1 0 123456"
    assert idle_culler.parse_stat_ppid(stat_text) == 1


def test_parse_stat_ppid_nonzero_ppid():
    stat_text = "200 (ssh_agent.py) S 999 200 200 0 -1 4194304 0 0 0 0 0 0 0 0 20 0 1 0 500"
    assert idle_culler.parse_stat_ppid(stat_text) == 999


def test_process_age_seconds_computes_elapsed_from_mocked_proc(tmp_path, monkeypatch):
    # starttime=100 ticks, SC_CLK_TCK=100 -> started at uptime=1.0s; current uptime=61.0s
    # -> age = 60.0s
    stat_file = tmp_path / "stat"
    uptime_file = tmp_path / "uptime"
    stat_file.write_text("300 (ssh_agent.py) S 1 300 300 0 -1 4194304 0 0 0 0 0 0 0 0 20 0 1 0 100")
    uptime_file.write_text("61.0 60.0")

    monkeypatch.setattr(idle_culler.os, "sysconf", lambda name: 100, raising=False)

    class FakePath:
        def __init__(self, s):
            self._s = s

        def read_text(self):
            if "stat" in self._s:
                return stat_file.read_text()
            return uptime_file.read_text()

    monkeypatch.setattr(idle_culler, "Path", FakePath)
    assert idle_culler.process_age_seconds(300) == pytest.approx(60.0)


# ─── L2: /proc integration (Design §8.3, POSIX only) ───────────────────────

@posix_only
def test_find_ssh_agent_pids_matches_only_real_name(tmp_path, monkeypatch):
    proc = subprocess.Popen(["sleep", "30"])
    try:
        assert proc.pid not in idle_culler.find_ssh_agent_pids()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@posix_only
def test_has_live_children_true_then_false_after_child_exits():
    parent = subprocess.Popen(["bash", "-c", "sleep 30 & wait"])
    try:
        time.sleep(0.3)
        assert idle_culler.has_live_children(parent.pid) is True
    finally:
        parent.terminate()
        parent.wait(timeout=5)
    time.sleep(0.3)
    assert idle_culler.has_live_children(parent.pid) is False


@posix_only
def test_is_orphaned_false_for_normal_child():
    proc = subprocess.Popen(["sleep", "30"])
    try:
        assert idle_culler.is_orphaned(proc.pid) is False
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# ─── _cull_if_eligible: job preservation + eligibility gating ──────────────

@posix_only
def test_cull_if_eligible_preserves_process_with_live_children(monkeypatch):
    monkeypatch.setattr(idle_culler, "has_live_children", lambda pid: True)
    killed = []
    monkeypatch.setattr(idle_culler.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    idle_culler._cull_if_eligible(4242)
    assert killed == []


@posix_only
def test_cull_if_eligible_skips_live_session_under_age_threshold(monkeypatch):
    monkeypatch.setattr(idle_culler, "has_live_children", lambda pid: False)
    monkeypatch.setattr(idle_culler, "is_orphaned", lambda pid: False)
    monkeypatch.setattr(idle_culler, "process_age_seconds", lambda pid: 10.0)
    monkeypatch.setattr(idle_culler, "IDLE_THRESHOLD_SEC", 100)
    killed = []
    monkeypatch.setattr(idle_culler.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    idle_culler._cull_if_eligible(4242)
    assert killed == []


@posix_only
def test_cull_if_eligible_kills_orphan_immediately(monkeypatch):
    monkeypatch.setattr(idle_culler, "has_live_children", lambda pid: False)
    monkeypatch.setattr(idle_culler, "is_orphaned", lambda pid: True)
    monkeypatch.setattr(idle_culler, "process_age_seconds", lambda pid: 0.0)
    monkeypatch.setattr(idle_culler, "time", type("T", (), {"sleep": staticmethod(lambda s: None)})())

    calls = []

    def fake_kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError()

    monkeypatch.setattr(idle_culler.os, "kill", fake_kill)
    idle_culler._cull_if_eligible(4242)
    assert (4242, idle_culler.signal.SIGTERM) in calls


@posix_only
def test_cull_if_eligible_kills_via_age_backstop(monkeypatch):
    monkeypatch.setattr(idle_culler, "has_live_children", lambda pid: False)
    monkeypatch.setattr(idle_culler, "is_orphaned", lambda pid: False)
    monkeypatch.setattr(idle_culler, "process_age_seconds", lambda pid: 99999.0)
    monkeypatch.setattr(idle_culler, "IDLE_THRESHOLD_SEC", 100)
    monkeypatch.setattr(idle_culler, "time", type("T", (), {"sleep": staticmethod(lambda s: None)})())

    calls = []

    def fake_kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError()

    monkeypatch.setattr(idle_culler.os, "kill", fake_kill)
    idle_culler._cull_if_eligible(4242)
    assert (4242, idle_culler.signal.SIGTERM) in calls


# ─── main() ─────────────────────────────────────────────────────────────────

def test_main_returns_1_on_windows(monkeypatch):
    monkeypatch.setattr(idle_culler.sys, "platform", "win32")
    assert idle_culler.main() == 1


@posix_only
def test_main_returns_0_and_scans_pids(monkeypatch):
    monkeypatch.setattr(idle_culler, "find_ssh_agent_pids", lambda: [111, 222])
    calls = []
    monkeypatch.setattr(idle_culler, "_cull_if_eligible", lambda pid: calls.append(pid))
    assert idle_culler.main() == 0
    assert calls == [111, 222]


@posix_only
def test_main_continues_past_race_errors(monkeypatch):
    monkeypatch.setattr(idle_culler, "find_ssh_agent_pids", lambda: [111, 222])

    def fake_cull(pid):
        if pid == 111:
            raise OSError("pid vanished")

    monkeypatch.setattr(idle_culler, "_cull_if_eligible", fake_cull)
    assert idle_culler.main() == 0
