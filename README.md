# dir-analysis MCP server

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant (e.g. Claude Code) browse, read, tail and search files on one or more remote Linux hosts over SSH, and copy files between those hosts and your local machine (like `scp`). Its main use is log analysis. Each host is limited to an allowlist of directories, and so is the local machine.

Built with [FastMCP](https://github.com/jlowin/fastmcp) and [Paramiko](https://www.paramiko.org/).

## Features

- **Multiple hosts.** Define any number of named hosts in `.env`, each with its own SSH target, credentials and allowed directories.
- **Directory allowlist.** Every path is normalized on the remote side, so `..` and symlink tricks are resolved before the check. Anything outside the allowed directories is rejected.
- **Gzip support.** `.gz` files (e.g. rotated logs) can be read, tailed and searched without unpacking them.
- **Efficient tailing.** Plain files are read backwards from the end, so tailing a large log doesn't download the whole file.
- **Server-side search.** `search_files` runs `grep`/`zgrep` on the remote host, so only the matching lines are sent back.
- **File transfer.** Download files from a host, or upload files to it. Transfers go to a `.part` file first and are renamed only when complete, so you never get a half-written file. Existing files are never replaced unless you pass `overwrite=True`, and transfers are capped at a configurable size.
- **Safe output.** Binary files are detected and skipped, and every response is capped at a configurable size.
- **Uses your SSH setup.** Reads `~/.ssh/config` (`HostName`, `Port`, `User`, `IdentityFile`, `ProxyCommand`) and only connects to hosts already in `known_hosts`.

## Tools

| Tool | Description |
|------|-------------|
| `list_hosts()` | Show the configured hosts, their SSH targets and allowed directories. |
| `list_directory(host, path, pattern="*", recursive=False, max_entries=500)` | List entries with type, size and modified time (UTC). Supports glob filters and recursion. |
| `read_file(host, path, start_line=1, num_lines=200)` | Read a range of lines from a text or `.gz` file, with line numbers. |
| `tail_file(host, path, lines=100)` | Return the last N lines of a text or `.gz` file. |
| `search_files(host, path, pattern, file_glob="*", regex=False, ignore_case=True, max_matches=200)` | Search one file or a directory tree. Plain text by default, or a `grep -E` regex. Hidden files and folders are skipped. `.gz` files are searched only when given directly as `path`. |
| `download_file(host, remote_path, local_dir="", overwrite=False)` | Copy a file from a host to a local folder (default: the first of `LOCAL_TRANSFER_DIRS`). Checks that the local disk has enough free space first. |
| `upload_file(host, local_path, remote_dir, overwrite=False)` | Copy a local file into an existing folder on a host. Hidden files, and paths through hidden folders, are refused. |

`host` can be left empty when only one host is configured. A relative remote path is resolved against that host's first allowed directory, and a relative local path against the first `LOCAL_TRANSFER_DIRS` entry.

## Requirements

- Python 3.9+
- Key-based SSH access to each target host, with the host key already in `~/.ssh/known_hosts` (connect once with `ssh` manually to add it)
- `grep` (and `zgrep` for `.gz` search) on the remote hosts

## Setup

```bash
git clone <this-repo-url> mcp-dir-analysis
cd mcp-dir-analysis

python3 -m venv venv
venv/bin/pip install -r requirement.txt

cp .env.example .env
# then edit .env with your hosts, user, key and allowed directories
```

## Configuration

All settings come from `.env` in the project directory (see [.env.example](.env.example)).

| Variable | Required | Description |
|----------|----------|-------------|
| `DIR_HOSTS` | yes | Comma-separated host names, e.g. `web-dev,web-prod`. |
| `SSH_HOST_<HOST>` | yes | Hostname, IP or `~/.ssh/config` alias for the host. |
| `FILE_ALLOWED_DIRS_<HOST>` | yes | Comma-separated list of absolute directories the server may access. |
| `SSH_USER` / `SSH_USER_<HOST>` | no | SSH username: a global default, or per host. Falls back to `~/.ssh/config`. |
| `SSH_KEY` / `SSH_KEY_<HOST>` | no | Private key path: a global default, or per host. Falls back to `IdentityFile` in `~/.ssh/config`. |
| `SSH_PORT` / `SSH_PORT_<HOST>` | no | SSH port. Defaults to `~/.ssh/config`, then 22. |
| `FILE_MAX_OUTPUT_CHARS` | no | Maximum characters per tool response (default `100000`). |
| `SEARCH_TIMEOUT_SECONDS` | no | Timeout for remote searches (default `120`). |
| `LOCAL_TRANSFER_DIRS` | for transfers | Comma-separated local folders that `download_file` and `upload_file` may use, subfolders included. If it's unset, both transfer tools are disabled. |
| `MAX_TRANSFER_BYTES` | no | Maximum file size for a transfer (default `10737418240`, i.e. 10 GB). |

`<HOST>` is the host name upper-cased, with `-` replaced by `_`. For example, `web-dev` becomes `SSH_HOST_WEB_DEV`.

## Register with Claude Code

```bash
claude mcp add dir-analysis --scope user \
  -- /absolute/path/to/mcp-dir-analysis/venv/bin/python3 \
     /absolute/path/to/mcp-dir-analysis/mcp-server-script.py

claude mcp get dir-analysis   # check the registration
claude mcp list               # list all MCP servers
```

Any other MCP client that can launch a stdio server works the same way: run `venv/bin/python3 mcp-server-script.py`.

## Troubleshooting

First check that plain SSH works without any prompts:

```bash
ssh -o BatchMode=yes -i ~/.ssh/<your-key> <user>@<host> 'ls <allowed-dir> | head -3'
```

- **`Server '...' not found in known_hosts`**: the server rejects unknown host keys. SSH to the host manually once to accept its key.
- **`No directories are allowed on ...`**: set `FILE_ALLOWED_DIRS_<HOST>` for that host.
- **`... is outside the allowed directories`**: the requested path, after symlinks were resolved, is not under an allowed directory.
- **Search timed out**: narrow `path` or `file_glob`, or raise `SEARCH_TIMEOUT_SECONDS`.
- **`No local transfer directories are allowed`**: set `LOCAL_TRANSFER_DIRS` to use `download_file` or `upload_file`.

## Security notes

- `upload_file` is the only tool that writes to remote hosts. It only writes inside the allowed directories, never touches hidden files or folders, and replaces an existing file only with `overwrite=True`. No tool deletes files, apart from removing a failed transfer's `.part` file.
- `search_files` runs a `grep` command over SSH, with every argument shell-quoted.
- Use a low-privilege SSH user, and keep the allowed directories (remote and local) as narrow as possible.
