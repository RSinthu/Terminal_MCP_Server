# Terminal MCP Server

A small, complete **Model Context Protocol (MCP)** server written with **FastMCP** and managed with **uv**. It gives an AI host — Claude Desktop, Cursor, or your own app — the ability to run shell commands inside a sandboxed workspace folder on your machine.

The code is ~30 lines. The rest of this README explains the protocol it implements, because that is the part worth learning.

---

## Table of contents

- [The 60-second version](#the-60-second-version)
- [What problem does MCP solve?](#what-problem-does-mcp-solve)
- [The components: host, client, server, service](#the-components-host-client-server-service)
- [The full workflow, step by step](#the-full-workflow-step-by-step)
- [FastMCP](#fastmcp)
- [uv](#uv)
- [This project](#this-project)
- [Setup](#setup)
- [Connecting to Claude Desktop](#connecting-to-claude-desktop)
- [Trying it out](#trying-it-out)
- [Code walkthrough](#code-walkthrough)
- [Troubleshooting](#troubleshooting)
- [Security](#security)

---

## The 60-second version

> **MCP** is an open protocol from Anthropic that standardises how an application gives an LLM access to tools and data. Think of it as **USB-C for AI applications**: one connector shape, and any compliant peripheral works.
>
> - **MCP host** — the app the user is in (Claude Desktop, Cursor, your agent). It holds the LLM conversation.
> - **MCP client** — the connector living *inside* the host. One client per server, 1:1.
> - **MCP server** — a process that exposes tools/resources over the protocol. **This repository is one.**
> - **Service** — the actual thing behind the server: a shell, a database, an API.
>
> A user message triggers **two round trips to the server** (first "what tools do you have?", then "run this one") and **two round trips to the LLM** (first "which tool?", then "here's the result, answer the user").
>
> The payoff: the service provider maintains everything behind the protocol. They can rewrite their internals freely and **your client code never changes**, because the contract sits at the protocol, not at their API.

---

## What problem does MCP solve?

### Life before MCP

Before MCP, connecting an AI app to an external tool meant writing a REST client — a `GET`/`POST` against some HTTP endpoint, parse the JSON, map it into your tool-calling layer. The same pattern you'd use in any web app, and the same pattern LangChain/LangGraph tool integrations were built on.

That works. It just doesn't scale across vendors:

- Every tool is a **separate bespoke integration**. Ten tools, ten clients to write and maintain.
- The **provider owns the API**, and you own the client. When they rename a field, version an endpoint, or change auth, **your code breaks and you fix it**.
- Nothing is reusable. Your Wikipedia integration teaches you nothing about integrating a vector DB.

Multiply that across an agentic app where a dozen graph nodes each call out to external services, and integration maintenance quietly becomes the bulk of the work.

### Life with MCP

MCP inserts a **standard protocol layer** between the AI app and the tools. Instead of your app speaking four different REST dialects, it speaks MCP — and each provider ships a server that also speaks MCP.

![REST versus MCP](docs/01-rest-vs-mcp.svg)

The critical shift is **who maintains what**:

| | Before MCP | With MCP |
|---|---|---|
| Integration code | One custom client **per tool**, written by you | One protocol implementation, reused for every server |
| Who absorbs breaking changes | **You** | **The provider**, behind their server |
| Tool discovery | Hardcoded — you read docs and write schemas by hand | Runtime — the client asks the server what it offers |
| Adding a tool | Write and test a new client | Add a config entry |
| Transport | HTTP request/response | JSON-RPC 2.0 over stdio or HTTP |
| Direction of calls | Client → server only | Bidirectional — servers can also ask the client for things |

### The USB-C analogy, precisely

A laptop has one USB-C port. Keyboard vendors, mouse vendors and charger vendors each build to the USB-C spec. When a vendor redesigns their mouse internals, your laptop needs no change — both sides agreed on the connector, not on the internals.

MCP is that connector for AI apps. Your host implements the protocol once; every compliant server plugs in.

### Is MCP a replacement for REST?

No — and this trips people up. MCP servers very often call REST APIs *internally*. MCP standardises the **AI-app-to-tool** edge; REST still lives behind it. The difference is that the REST client now lives in the provider's server, maintained by them, instead of in your codebase, maintained by you.

---

## The components: host, client, server, service

![MCP components](docs/02-mcp-components.svg)

### MCP Host

The application the user actually interacts with, and the only component that talks to *both* the LLM and the servers.

Examples: **Claude Desktop**, **Cursor**, VS Code extensions, or any app you write. Its responsibilities:

- own the conversation and the LLM calls
- read config to know which servers exist, and launch them
- create one MCP client per server
- assemble the tool catalog from all connected servers and pass it to the model
- enforce permission (the "allow this tool?" prompt you see in Claude Desktop)

### MCP Client

The connector object that lives inside the host. **One client per server — a strict 1:1 relationship.** Four servers configured means the host creates four clients.

The client handles the protocol handshake, `tools/list` and `tools/call` requests, and keeps the session alive. You rarely write one by hand; the host does it for you. It is the *only* thing that speaks to a server.

### MCP Server

A program that exposes capabilities over MCP. Three kinds of capability:

| Capability | What it is | Controlled by |
|---|---|---|
| **Tools** | Functions the model can invoke to *do* something | the model |
| **Resources** | Read-only data the host can load as context (files, records) | the host/app |
| **Prompts** | Reusable prompt templates the user can select | the user |

This repo exposes exactly one **tool**, `run_command`. Servers can run locally as a child process (talking over **stdio**, what we use here) or remotely over HTTP.

### Service

The real system the server fronts: a shell, Postgres, the GitHub API, a vector store. The server is a thin adapter; the service does the work.

**This is the layer the provider owns.** All the churn happens here, and none of it reaches you.

---

## The full workflow, step by step

Here is what actually happens when you type *"create a folder called demo"* into a host with this server connected.

![MCP workflow](docs/03-mcp-workflow.svg)

**Phase 1 — Discovery**

1. The user sends a prompt to the host.
2. The host's MCP client asks each connected server: *what tools do you have?* (`tools/list`)
3. Each server replies with its catalog: tool **name**, **description** (from the docstring) and **argument schema** (from the type hints).

**Phase 2 — Decision**

4. The host sends the LLM the user's prompt **plus** the tool catalog.
5. The LLM replies with a decision — not with an answer: *call `run_command` with `command="mkdir demo"`.* The model chooses; it does not execute.

**Phase 3 — Execution**

6. The host's client sends the actual invocation to the server (`tools/call`). This is where the host asks the user for permission, if configured.
7. The server runs the real work against the service — here, `subprocess.run` in your shell.
8. The result travels back: service → server → client → host. **This result is the context.** Everything so far exists to produce this string.

**Phase 4 — Answer**

9. The host sends the LLM the conversation **plus** the tool output as context.
10. The LLM, now grounded in a real result rather than guessing, writes the answer.
11. The host shows it to the user.

### Three things worth internalising

- **The LLM never touches the tool.** It only ever emits a *request* to call one. The host executes it. That separation is what makes permission prompts, logging and sandboxing possible.
- **The loop repeats.** If the model wants another tool call after seeing a result, steps 5–10 run again. Multi-step tasks are just this loop iterating.
- **Discovery is at runtime.** The host doesn't know your tools until it asks. That's why you can add a tool to a server, restart the host, and the model immediately knows about it — no host code changed.

---

## FastMCP

Implementing MCP from scratch means writing a JSON-RPC message loop, the initialize handshake, protocol version negotiation, tool registry, content-type handling and error envelopes. That is a lot of boilerplate for what is conceptually "expose this function."

**FastMCP** (bundled in the `mcp` Python package) handles all of it. It's the high-level, Pythonic API — in most cases **decorating a function is all you need**:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Terminal")        # name the host will display

@mcp.tool()                      # register the function as an MCP tool
async def run_command(command: str) -> str:
    """Run a terminal command inside the workspace directory."""
    ...

if __name__ == "__main__":
    mcp.run(transport="stdio")   # start serving
```

### How your function becomes a tool definition

`@mcp.tool()` introspects the function and derives the exact JSON the LLM will see:

| Source in your code | Becomes | Why it matters |
|---|---|---|
| Function name `run_command` | tool `name` | how the model refers to it |
| **Docstring** | tool `description` | **how the model decides whether to use it** |
| Type hints `command: str` | `inputSchema` | how the model formats arguments |
| Return value | tool result content | the context fed back to the model |

### Write the docstring like a prompt, because it is one

The docstring is not a comment for humans — it is shipped to the LLM verbatim during discovery, and it is the *only* thing the model has to go on when deciding whether this tool fits the request. A vague docstring means the tool is silently never chosen.

Say what it does, when to use it, what each argument is, and what comes back:

```python
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
```

That middle sentence is deliberate steering: without it, a model will often reply *"I can't run commands on your computer"* instead of reaching for the tool it was just handed.

### `async`

Tool functions are declared `async`. FastMCP serves requests on an event loop, so an async signature lets a tool await I/O without blocking the whole server. Note that `subprocess.run` in this file is *synchronous* and will block the loop for the duration of the command — fine for a single-user local server, worth revisiting if you ever run long commands concurrently.

---

## uv

**uv** is an extremely fast Python package and project manager, written in Rust. It replaces `pip` + `venv` + `pyproject` tooling with one binary, and it's the standard way to run MCP servers because the host needs a single deterministic command to launch your script.

Install on Windows (PowerShell):

```bash
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

On macOS / Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

It lands in `C:\Users\<you>\.local\bin\uv.exe` on Windows — **note that path, the host config needs it.**

Commands used in this project:

| Command | What it does |
|---|---|
| `uv --version` | verify the install |
| `uv init` | scaffold a project (`pyproject.toml`, `main.py`, `.python-version`) |
| `uv venv` | create a virtual environment in `.venv` |
| `uv add "mcp[cli]"` | add a dependency and update `pyproject.toml` + `uv.lock` |
| `uv run main.py` | run inside the project env, syncing deps first |
| `uv run mcp install main.py` | register this server into Claude Desktop's config |

`uv.lock` pins exact resolved versions so the environment is reproducible. Commit it.

---

## This project

![This project mapped onto MCP roles](docs/04-this-project.svg)

```
Terminal_MCP_Server/
├── main.py           # the MCP server — FastMCP instance + one tool
├── pyproject.toml    # project metadata and dependencies
├── uv.lock           # pinned dependency versions
├── .python-version   # Python version pin for uv
├── docs/             # the diagrams in this README
└── temp/             # the sandboxed workspace (created on first run)
```

| MCP role | Filled by |
|---|---|
| Host | Claude Desktop |
| Client | created by Claude Desktop from `claude_desktop_config.json` |
| Server | `main.py` — `FastMCP("Terminal")` |
| Tool | `run_command(command: str) -> str` |
| Service | your Windows shell, via `subprocess` |
| Transport | stdio (server runs as a child process of the host) |

---

## Setup

From scratch:

```bash
mkdir -p Terminal_MCP_Server
```

```bash
cd Terminal_MCP_Server
```

```bash
uv init
```

```bash
uv venv
```

Activate it — PowerShell:

```bash
.venv\Scripts\Activate.ps1
```

Add the dependency (`[cli]` pulls in the `mcp` command-line tool used for install/dev):

```bash
uv add "mcp[cli]"
```

Run the server to confirm it starts:

```bash
uv run main.py
```

It will sit there with no output. That is correct — it's waiting for a host to speak JSON-RPC on stdin. `Ctrl+C` to stop.

To poke at it interactively before wiring up a host, use the MCP Inspector:

```bash
uv run mcp dev main.py
```

---

## Connecting to Claude Desktop

### Option A — automatic

```bash
uv run mcp install main.py
```

This locates `claude_desktop_config.json` and writes the server entry for you.

> **If it fails with "failed to update Claude config":** the config file is probably completely empty. Open it and put a single `{}` in it, save, and re-run. The installer needs valid JSON to merge into.

Open the config from Claude Desktop: **File → Settings → Developer → Edit Config**.

### Option B — manual

Edit `claude_desktop_config.json` yourself:

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "Terminal": {
      "command": "C:\\Users\\<you>\\.local\\bin\\uv.exe",
      "args": [
        "run",
        "--with",
        "mcp[cli]",
        "mcp",
        "run",
        "D:\\Project\\MCP\\Terminal_MCP_Server\\main.py"
      ]
    }
  }
}
```

Two Windows-specific things that cause most failures:

1. **Use the absolute path to `uv.exe`.** The host does not inherit your shell's `PATH`. If the auto-installer wrote an Anaconda `uv` path and that isn't the uv you actually use, replace it with `C:\Users\<you>\.local\bin\uv.exe`.
2. **Escape backslashes** — `\\` in JSON, everywhere.

### Restart Claude Desktop properly

Closing the window is not enough; it keeps running in the tray. Quit it fully — Task Manager if needed — then reopen.

Verify: the tools icon in the chat box should list **Terminal**, and **Settings → Developer → Terminal** should show status *running*.

---

## Trying it out

Ask in plain language — you never name the tool:

> Can you create a directory called `claude_output` and make a file inside called `terminal_test.txt`?

Claude will announce it's using the terminal tool, prompt you to allow it the first time, and the folder will appear under `temp/`.

Then:

> Add the text "I have successfully made my first MCP server 🎉" to that file, then open it in VS Code.

**Contrast with no server connected:** ask the same thing and Claude will hand you back the string `mkdir claude_output` as text. It knows the command; it has no way to run it. The tool is the difference between describing an action and taking one.

---

## Code walkthrough

`main.py`, in order:

```python
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("Terminal")
```

Creates the server. `"Terminal"` is the display name in the host UI and the key `mcp install` writes into the config.

```python
DEFAULT_WORKSPACE = os.path.expanduser("d:/Project/MCP/Terminal_MCP_Server/temp")
os.makedirs(DEFAULT_WORKSPACE, exist_ok=True)
```

The workspace. Every command runs with this as its working directory, so `mkdir demo` lands here rather than wherever the host was launched from. `expanduser` is what makes a leading `~` work; absolute paths pass through unchanged. `exist_ok=True` makes startup idempotent.

```python
@mcp.tool()
async def run_command(command: str) -> str:
```

Registration. Name, description and schema are all derived from this signature and its docstring — see [FastMCP](#fastmcp).

```python
result = subprocess.run(command, shell=True, capture_output=True,
                        cwd=DEFAULT_WORKSPACE, text=True)
```

| Argument | Why |
|---|---|
| `shell=True` | hand the string to the system shell so pipes, `&&` and globbing work as typed |
| `capture_output=True` | collect stdout/stderr instead of letting them leak into the stdio channel — **printing to stdout would corrupt the JSON-RPC stream and kill the connection** |
| `cwd=DEFAULT_WORKSPACE` | run inside the workspace |
| `text=True` | decode bytes to `str` so the result can be returned directly |

```python
return result.stdout or result.stderr
```

Prefer stdout, fall back to stderr when the command wrote nothing to stdout (usually because it failed). This return value **is** the context handed back to the LLM at step 9.

```python
except Exception as e:
    return str(e)
```

Errors come back as a normal string rather than raising, so the model can read the failure, explain it, and retry with a corrected command.

```python
if __name__ == "__main__":
    mcp.run(transport="stdio")
```

Starts the event loop and blocks, serving MCP over stdin/stdout until the host disconnects. Guarded so importing the module doesn't start a server.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `failed to update Claude config` | config file is empty, not valid JSON | put `{}` in it, save, re-run `mcp install` |
| Server missing from the tools list | Claude Desktop wasn't fully restarted | quit from the tray / Task Manager, reopen |
| Status shows *failed to start* | wrong `uv.exe` path, or unescaped backslashes | use the absolute path and `\\` in JSON |
| Connection drops mid-command | something wrote to stdout | never `print()` in a stdio server — stdout is the protocol channel |
| Model won't use the tool | weak docstring | describe when to use it, explicitly |
| Files appear in the wrong place | `cwd` not what you expect | check `DEFAULT_WORKSPACE`, and remember `cd` inside a command changes it |

Server logs live next to the Claude config, under `logs/mcp-server-Terminal.log`.

---

## Security

This server runs **arbitrary shell commands with your user account's full privileges**. Be clear-eyed about what that means:

- `cwd` sets a *starting directory*, not a boundary. `cd ..`, absolute paths, and destructive commands all still work. **It is not a sandbox.**
- `shell=True` means shell metacharacters are interpreted. Anything reaching the `command` argument is executed.
- Anything that can influence the model's output — a web page it reads, a file it opens — can potentially influence what command it proposes. Keep the approval prompts on; don't blanket-allow the tool.
- Keep this server local and stdio-only. Do not expose it over HTTP to anything you don't control.

For real use, consider an allowlist of permitted commands, a timeout on `subprocess.run`, and rejecting paths that resolve outside the workspace.

---

## Further reading

- [Model Context Protocol — docs](https://modelcontextprotocol.io)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [uv](https://github.com/astral-sh/uv)
