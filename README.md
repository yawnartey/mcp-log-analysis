# Loki MCP Server

A Model Context Protocol (MCP) server that connects Claude to Grafana Loki, enabling natural language log querying directly from Claude Code.

## Overview

### Components

| Component      | Description                                                                   |
| -------------- | ----------------------------------------------------------------------------- |
| **MCP Server** | Python script (`mcp-server-script.py`) that exposes Loki as tools via FastMCP |
| **MCP Host**   | Claude Code (CLI or Desktop App) — runs and manages the MCP server process    |
| **MCP Client** | Claude — uses the tools exposed by the MCP server to query Loki               |

### Architecture

```
Claude (MCP Client)
      │
      ▼
Claude Code (MCP Host)
      │  spawns
      ▼
mcp-server-script.py (MCP Server)
      │  HTTP
      ▼
Grafana Loki
```

### Available Tools

| Tool                | Description                                               |
| ------------------- | --------------------------------------------------------- |
| `list_labels`       | List all label names in Loki                              |
| `list_label_values` | List all values for a given label                         |
| `query_logs`        | Run a LogQL query and return matching log lines           |
| `count_occurrences` | Count log lines matching a pattern over N hours           |
| `error_summary`     | Count ERROR/WARN/FATAL occurrences per level over N hours |

---

## Prerequisites

- Python 3.8+
- Claude Code CLI (`npm install -g @anthropic-ai/claude-code`) or Claude Code Desktop App
- Network access to your Loki instance

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/yawnartey/mcp-log-analysis.git
cd mcp-log-analysis
```

### 2. Create a virtual environment & install the required packages

```bash
python3 -m venv venv
pip install -r requirement.txt
```

Verify the packages are installed:

```bash
venv/bin/python3 -c "import fastmcp, httpx, dotenv; print('ok')"
```

### 3. Configure environment variables

Copy the example and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:

```env
LOKI_URL_DEV=http://<dev-loki-host>:3100
LOKI_URL_PROD=http://<prod-loki-host>:3100
LOKI_USER=
LOKI_PASS=
```

> `LOKI_USER` and `LOKI_PASS` are optional — leave blank if your Loki instance has no authentication.

### 4. Register the MCP server with Claude Code

#### CLI

Run these commands to register both environments: (Note: I did setup a dev and prod environments)

```bash
claude mcp add loki-dev --scope user \
  /absolute/path/to/mcp-log-analysis/venv/bin/python3 \
  /absolute/path/to/mcp-log-analysis/mcp-server-script.py \
  -e LOKI_ENV=dev

claude mcp add loki-prod --scope user \
  /absolute/path/to/mcp-log-analysis/venv/bin/python3 \
  /absolute/path/to/mcp-log-analysis/mcp-server-script.py \
  -e LOKI_ENV=prod
```

Verify both are connected:

```bash
claude mcp list
```

You should see `loki-dev` and `loki-prod` with status `Connected`.

#### Desktop App

Add the following `mcpServers` block to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "loki-dev": {
      "type": "stdio",
      "command": "/absolute/path/to/mcp-log-analysis/venv/bin/python3",
      "args": ["/absolute/path/to/mcp-log-analysis/mcp-server-script.py"],
      "env": {
        "LOKI_ENV": "dev"
      }
    },
    "loki-prod": {
      "type": "stdio",
      "command": "/absolute/path/to/mcp-log-analysis/venv/bin/python3",
      "args": ["/absolute/path/to/mcp-log-analysis/mcp-server-script.py"],
      "env": {
        "LOKI_ENV": "prod"
      }
    }
  }
}
```

Then fully quit (`Cmd+Q`) and reopen the Desktop App.

> **Important:** Always use the absolute path to `venv/bin/python3` — not the bare `python3` command. The Desktop App and CLI do not inherit your shell's PATH, so the bare command will resolve to the wrong Python (one that doesn't have the dependencies installed).

---

## Usage

In any Claude Code session, specify the environment in your prompt:

```
using loki-prod, list all available labels
```

```
query loki-dev for ERROR logs in the last 2 hours for all clusters
```

```
using loki-prod, give me an error summary for service mbs in the last 24 hours
```

```
search loki-prod for OOMKilled events across all clusters
```

Claude will automatically use the appropriate MCP tools (`list_labels`, `query_logs`, etc.) to fetch the data directly from Loki.

---

## Environment Variable Reference

| Variable        | Description                         | Required |
| --------------- | ----------------------------------- | -------- |
| `LOKI_URL_DEV`  | Base URL of your dev Loki instance  | Yes      |
| `LOKI_URL_PROD` | Base URL of your prod Loki instance | Yes      |
| `LOKI_USER`     | Basic auth username                 | No       |
| `LOKI_PASS`     | Basic auth password                 | No       |

The `LOKI_ENV` variable (`dev` or `prod`) is injected by the MCP host config — it selects which `LOKI_URL_*` value to use. You do not set it in `.env`.

---

## Adding a New Environment

1. Add the URL to `.env`:

   ```env
   LOKI_URL_STAGING=http://<staging-loki-host>:3100
   ```

2. Register with the CLI:

   ```bash
   claude mcp add loki-staging --scope user \
     /absolute/path/to/venv/bin/python3 \
     /absolute/path/to/mcp-server-script.py \
     -e LOKI_ENV=staging
   ```

3. Add to `~/.claude/settings.json`:
   ```json
   "loki-staging": {
     "type": "stdio",
     "command": "/absolute/path/to/mcp-log-analysis/venv/bin/python3",
     "args": [
       "/absolute/path/to/mcp-log-analysis/mcp-server-script.py"
     ],
     "env": {
       "LOKI_ENV": "staging"
     }
   }
   ```

---

## Troubleshooting

**MCP server shows `Failed to connect`**

- Confirm the venv Python path is absolute and correct:
  ```bash
  ls /absolute/path/to/venv/bin/python3
  ```
- Confirm dependencies are installed in that venv:
  ```bash
  /absolute/path/to/venv/bin/python3 -c "import fastmcp; print('ok')"
  ```

**Claude asks for the Loki URL instead of using tools**

- The MCP server is not connected in this session. Run `claude mcp list` to check status.
- For the Desktop App, ensure the config is in `~/.claude/settings.json` (not just `~/.claude.json`) and restart the app.

**`ModuleNotFoundError: No module named 'fastmcp'`**

- The wrong Python is being used. Make sure the `command` in your config points to `venv/bin/python3`, not a system Python.

**Loki queries return no results**

- Check your network/VPN can reach the Loki host:
  ```bash
  nc -zv <loki-host> 3100
  ```
- Verify the label names with: `list all loki-prod labels`
