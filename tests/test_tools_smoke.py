# L1 tool smoke tests — Design §8.2. Guards the 7 MCP tool signatures/behavior
# against regressions while lifecycle code (idle self-exit, job reaper, ssh_bg_kill)
# is added around them.
import asyncio
import os
import time

import pytest

import ssh_agent
from ssh_agent import call_tool, list_tools


def run(coro):
    return asyncio.run(coro)


def test_list_tools_includes_all_seven():
    tools = run(list_tools())
    names = {t.name for t in tools}
    assert names == {
        "ssh_run", "ssh_bg_run", "ssh_bg_poll", "ssh_bg_kill",
        "file_read", "file_write", "file_ls", "file_grep",
    }


def test_ssh_run_success():
    result = run(call_tool("ssh_run", {"command": "echo hello"}))
    assert "hello" in result[0].text
    assert "[exit" not in result[0].text


def test_ssh_run_nonzero_exit():
    result = run(call_tool("ssh_run", {"command": "exit 3"}))
    assert "[exit 3]" in result[0].text


def test_ssh_run_nonexistent_command_hits_stderr_branch():
    result = run(call_tool("ssh_run", {"command": "this_command_does_not_exist_anywhere"}))
    assert "[stderr]" in result[0].text
    assert "[exit" in result[0].text


def test_ssh_run_timeout():
    result = run(call_tool("ssh_run", {"command": "sleep 5", "timeout": 1}))
    assert "[timeout after 1s]" in result[0].text


def test_file_read_write_roundtrip(tmp_path):
    path = str(tmp_path / "roundtrip.txt")
    w = run(call_tool("file_write", {"path": path, "content": "hello world"}))
    assert "Written:" in w[0].text
    r = run(call_tool("file_read", {"path": path}))
    assert r[0].text == "hello world"


def test_file_read_missing_returns_error():
    r = run(call_tool("file_read", {"path": "/no/such/file/at/all.txt"}))
    assert "[error]" in r[0].text


def test_file_ls(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    result = run(call_tool("file_ls", {"path": str(tmp_path)}))
    assert "a.txt" in result[0].text and "b.txt" in result[0].text


def test_file_grep(tmp_path, bash_tmp):
    _, bash_dir = bash_tmp
    target = tmp_path / "grep_target.txt"
    target.write_text("needle in haystack\nnothing here\n")
    result = run(call_tool("file_grep", {"pattern": "needle", "path": f"{bash_dir}/grep_target.txt"}))
    assert "needle" in result[0].text


def test_ssh_bg_run_and_poll(monkeypatch, bash_tmp):
    win_dir, bash_dir = bash_tmp
    monkeypatch.setattr(ssh_agent, "JOB_OUTFILE_TMPL", bash_dir + "/mcp_job_{job_id}.txt")
    monkeypatch.setattr(ssh_agent, "JOB_PIDFILE_TMPL", bash_dir + "/mcp_job_{job_id}.pid")

    started = run(call_tool("ssh_bg_run", {"command": "echo bg-output"}))
    text = started[0].text
    job_id = text.split("job_id: ")[1].split("\n")[0]

    polled = None
    for _ in range(50):
        polled = run(call_tool("ssh_bg_poll", {"job_id": job_id}))
        if polled[0].text.startswith("[done]"):
            break
        time.sleep(0.1)
    else:
        pytest.fail("job did not complete in time")

    assert "bg-output" in polled[0].text
    assert os.path.exists(win_dir / f"mcp_job_{job_id}.pid")


def test_ssh_bg_poll_unknown_job():
    result = run(call_tool("ssh_bg_poll", {"job_id": "doesnotexist"}))
    assert "Unknown job" in result[0].text
