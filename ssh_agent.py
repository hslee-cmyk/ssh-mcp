"""
ssh_agent.py — MCP server for remote shell access via SSH stdio transport.
Run on the remote server: python3 ssh_agent.py
Register in ~/.claude.json:
  "ssh": { "command": "ssh", "args": ["-o","BatchMode=yes","cloud0",
           "/opt/mcp-env/bin/python3", "/opt/mcp-servers/ssh_agent.py"] }
"""

import asyncio
import os
import subprocess
import uuid

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

app = Server("ssh-agent")

# Background job registry: job_id -> {process, outfile}
_jobs: dict = {}


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
        outfile = f"/tmp/mcp_job_{job_id}.txt"
        cwd     = arguments.get("cwd", "~")
        cmd     = (
            f"cd {cwd} && "
            f"{{ {arguments['command']}; }} >& {outfile}; "
            f"echo MCP_DONE_{job_id} >> {outfile}"
        )
        proc = subprocess.Popen(["bash", "-c", cmd])
        _jobs[job_id] = {"process": proc, "outfile": outfile}
        return [TextContent(type="text", text=f"job_id: {job_id}\noutput: {outfile}")]

    # ── ssh_bg_poll ───────────────────────────────────────────────────────────
    elif name == "ssh_bg_poll":
        job_id = arguments["job_id"]
        job    = _jobs.get(job_id)
        if not job:
            return [TextContent(type="text", text=f"Unknown job: {job_id}")]
        done   = job["process"].poll() is not None
        output = ""
        if os.path.exists(job["outfile"]):
            with open(job["outfile"]) as f:
                output = f.read()
        status = "done" if done else "running"
        return [TextContent(type="text", text=f"[{status}]\n{output}")]

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
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )

if __name__ == "__main__":
    asyncio.run(main())
