"""
Terminal MCP Server
===================

A minimal Model Context Protocol (MCP) server that exposes ONE tool to an MCP
host (Claude Desktop, Cursor, or any custom app): the ability to run a shell
command inside a fixed workspace folder.

Where this file sits in the MCP picture:

    Claude Desktop  <-- MCP protocol (JSON-RPC over stdio) -->  THIS FILE  -->  your shell
    (host + client)                                            (MCP server)     (the service)

The host launches this script as a child process, then talks to it over
stdin/stdout. We never write any of that plumbing ourselves — FastMCP does it.

See README.md for the full explanation and diagrams.
"""

import os
import subprocess

# FastMCP is the high-level helper shipped inside the `mcp` package.
# It hides the boilerplate of a raw MCP server: the JSON-RPC message loop,
# the initialize/handshake, protocol versioning, tool registration, error
# envelopes, and the stdio transport. What is left for us is a plain function.
from mcp.server.fastmcp import FastMCP

# Create the server instance.
# "Terminal" is the server's NAME. The host shows this name in its UI and uses
# it as the key when `uv run mcp install main.py` writes the config entry, so
# keep it short and descriptive.
mcp = FastMCP("Terminal")

# The sandbox folder. Every command this server runs will use this directory as
# its working directory (cwd), so relative paths like `mkdir demo` land here
# instead of wherever the host happened to be launched from.
#
# expanduser() is what makes a leading "~" work (e.g. "~/mcp/workspace").
# An absolute path like the one below passes through unchanged.
DEFAULT_WORKSPACE = os.path.expanduser("d:/Project/MCP/Terminal_MCP_Server/temp")

# Create the folder on startup if it does not exist yet, so the very first
# command never fails with "directory not found". exist_ok=True makes this a
# no-op on later runs.
os.makedirs(DEFAULT_WORKSPACE, exist_ok=True)


# The @mcp.tool() decorator is the whole registration step. At import time
# FastMCP inspects this function and derives the tool definition the LLM sees:
#
#   name         <- the function name .............. "run_command"
#   description  <- the docstring below ............ tells the model WHEN to use it
#   inputSchema  <- the type hints ................. {"command": {"type": "string"}}
#
# That derived definition is what gets returned during the discovery phase
# (step 2/3 in the README workflow diagram). This is why the docstring matters
# as much as the code: it is prompt text, not just a comment. A vague docstring
# means the model never picks the tool, or calls it with the wrong arguments.
@mcp.tool()
async def run_command(command: str) -> str:
    """
    Run a terminal command inside the workspace directory.
    If terminal command can accomplish the task,
    tell the user you'll use the tool to accomplish it,
    eventhough you cannot directly do it

    Args:
        command: The shell command to run.

    Returns:
        The command output or error message
    """
    try:
        # shell=True      -> the string is handed to the system shell, so shell
        #                    syntax (pipes, &&, globbing) works as typed.
        # capture_output  -> collect stdout and stderr instead of letting them
        #                    leak into the stdio channel the MCP protocol is
        #                    using. Printing to stdout here would corrupt the
        #                    JSON-RPC stream and break the connection.
        # cwd             -> run inside the workspace (see the note above).
        # text=True       -> decode bytes to str, so we can return them directly.
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            cwd=DEFAULT_WORKSPACE,
            text=True,
        )

        # Prefer stdout; fall back to stderr when the command wrote nothing to
        # stdout (typically because it failed). Whatever we return here becomes
        # the tool result -- i.e. the CONTEXT the host feeds back into the LLM
        # before it writes its final answer.
        return result.stdout or result.stderr

    except Exception as e:
        # Return the error as a normal string rather than raising. The model can
        # read it, explain it to the user, and retry with a corrected command.
        return str(e)


# Only runs when the file is executed directly (`uv run main.py`), not when the
# host imports it. mcp.run() starts the event loop and blocks, serving MCP
# requests over stdio until the host closes the connection.
if __name__ == "__main__":
    mcp.run(transport="stdio")
