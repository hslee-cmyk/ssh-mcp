import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssh_agent  # noqa: E402


def _to_bash_path(win_path: Path) -> str:
    """Convert a native path to a form both native Python (open()/os.path) AND
    ssh_agent's bash subprocess (MSYS2 on Windows) can resolve to the SAME file.

    JOB_OUTFILE_TMPL/JOB_PIDFILE_TMPL are read/written from both sides (Python
    directly, and bash via shell redirection), so a plain '/c/...'-style MSYS
    mount path won't do — native Python doesn't understand that form. A drive-
    letter path with forward slashes ('C:/Users/...') is understood by both:
    MSYS2's runtime auto-translates recognizable Windows paths in argv, and
    Python accepts forward slashes natively. On POSIX this is a plain no-op
    (backslashes never appear in a POSIX path to begin with).
    """
    return str(win_path).replace("\\", "/")


@pytest.fixture
def bash_tmp(tmp_path):
    """(native_path, bash_path) pair pointing at the same directory, usable both
    from Python's os.path and from the bash subprocess ssh_agent spawns."""
    return tmp_path, _to_bash_path(tmp_path)


@pytest.fixture(autouse=True)
def _reset_jobs():
    ssh_agent._jobs.clear()
    yield
    ssh_agent._jobs.clear()
