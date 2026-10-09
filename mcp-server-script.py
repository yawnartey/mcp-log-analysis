import os
import gzip
import stat
import shlex
import fnmatch
import posixpath
import socket
import shutil
import threading
import itertools
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import paramiko
from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

mcp = FastMCP("dir-analysis")

MAX_OUTPUT_CHARS = int(os.environ.get("FILE_MAX_OUTPUT_CHARS", "100000"))
SEARCH_TIMEOUT = int(os.environ.get("SEARCH_TIMEOUT_SECONDS", "120"))
LOCAL_TRANSFER_DIRS = [
    Path(p.strip()).expanduser().resolve()
    for p in os.environ.get("LOCAL_TRANSFER_DIRS", "").split(",")
    if p.strip()
]
MAX_TRANSFER_BYTES = int(os.environ.get("MAX_TRANSFER_BYTES", str(10 * 1024 ** 3)))


class _Host:
    def __init__(self, name: str):
        self.name = name.lower()
        self.env_key = name.upper().replace("-", "_")
        self.ssh_host = os.environ.get(f"SSH_HOST_{self.env_key}", "")
        self.user = os.environ.get(f"SSH_USER_{self.env_key}") or os.environ.get("SSH_USER", "")
        self.port = os.environ.get(f"SSH_PORT_{self.env_key}") or os.environ.get("SSH_PORT", "")
        self.key_file = os.environ.get(f"SSH_KEY_{self.env_key}") or os.environ.get("SSH_KEY", "")
        self.allowed = [p.strip() for p in os.environ.get(f"FILE_ALLOWED_DIRS_{self.env_key}", "").split(",") if p.strip()]
        self.lock = threading.Lock()
        self.client = None
        self.sftp = None
        self.roots = []

    def connect(self):
        transport = self.client.get_transport() if self.client else None
        if transport and transport.is_active():
            return
        if not self.ssh_host:
            raise RuntimeError(f"SSH_HOST_{self.env_key} is not set in .env.")
        cfg = {}
        ssh_config = Path("~/.ssh/config").expanduser()
        if ssh_config.exists():
            cfg = paramiko.SSHConfig.from_path(str(ssh_config)).lookup(self.ssh_host)
        if self.key_file:
            key_files = [os.path.expanduser(self.key_file)]
        else:
            key_files = [os.path.expanduser(k) for k in cfg.get("identityfile", [])] or None
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            hostname=cfg.get("hostname", self.ssh_host),
            port=int(self.port or cfg.get("port", 22)),
            username=self.user or cfg.get("user"),
            key_filename=key_files,
            sock=paramiko.ProxyCommand(cfg["proxycommand"]) if "proxycommand" in cfg else None,
            timeout=15,
        )
        self.client = client
        self.sftp = client.open_sftp()
        self.roots = [self.sftp.normalize(r) for r in self.allowed]

    def resolve(self, path: str) -> str:
        self.connect()
        if not self.roots:
            raise PermissionError(f"No directories are allowed on {self.name}. Set FILE_ALLOWED_DIRS_{self.env_key} in .env.")
        p = path if path.startswith("/") else posixpath.join(self.roots[0], path)
        try:
            p = self.sftp.normalize(p)
        except IOError:
            raise FileNotFoundError(f"No such file or directory on {self.name}: {p}")
        for root in self.roots:
            if p == root or p.startswith(root.rstrip("/") + "/"):
                return p
        raise PermissionError(f"{p} is outside the allowed directories on {self.name}: {', '.join(self.roots)}")


_HOSTS = {
    n.strip().lower(): _Host(n.strip())
    for n in os.environ.get("DIR_HOSTS", "").split(",")
    if n.strip()
}


def _host(name: str) -> _Host:
    if not _HOSTS:
        raise RuntimeError("No hosts configured. Set DIR_HOSTS in .env.")
    if not name:
        if len(_HOSTS) == 1:
            return next(iter(_HOSTS.values()))
        raise ValueError(f"Specify a host. Available: {', '.join(_HOSTS)}")
    h = _HOSTS.get(name.strip().lower())
    if not h:
        raise ValueError(f"Unknown host '{name}'. Available: {', '.join(_HOSTS)}")
    return h


def _is_binary(sftp, p: str) -> bool:
    if p.endswith(".gz"):
        return False
    with sftp.open(p, "rb") as f:
        return b"\0" in f.read(8192)


def _iter_lines(sftp, p: str):
    with sftp.open(p, "rb", bufsize=65536) as f:
        stream = gzip.GzipFile(fileobj=f) if p.endswith(".gz") else f
        for raw in stream:
            yield raw.decode(errors="replace").rstrip("\r\n")


def _cap(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n... [output truncated at {MAX_OUTPUT_CHARS} characters]"


def _tail_lines(sftp, p: str, n: int) -> list:
    if p.endswith(".gz"):
        return list(deque(_iter_lines(sftp, p), maxlen=n))
    with sftp.open(p, "rb") as f:
        pos = f.stat().st_size
        data = b""
        while pos > 0 and data.count(b"\n") <= n:
            step = min(65536, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    return data.decode(errors="replace").splitlines()[-n:]


def _walk(sftp, d: str, recursive: bool):
    stack = [d]
    while stack:
        cur = stack.pop()
        for a in sorted(sftp.listdir_attr(cur), key=lambda a: a.filename):
            full = posixpath.join(cur, a.filename)
            yield full, a
            if recursive and stat.S_ISDIR(a.st_mode or 0):
                stack.append(full)


def _local_resolve(path: str) -> Path:
    if not LOCAL_TRANSFER_DIRS:
        raise PermissionError("No local transfer directories are allowed. Set LOCAL_TRANSFER_DIRS in .env.")
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = LOCAL_TRANSFER_DIRS[0] / p
    p = p.resolve()
    for root in LOCAL_TRANSFER_DIRS:
        if p == root or root in p.parents:
            return p
    raise PermissionError(f"{p} is outside the local transfer directories: {', '.join(map(str, LOCAL_TRANSFER_DIRS))}")


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


@mcp.tool()
def list_hosts() -> str:
    """List the configured hosts, the SSH host each connects to, and the directories allowed on each."""
    if not _HOSTS:
        return "No hosts configured. Set DIR_HOSTS in .env."
    lines = ["Hosts:"]
    for h in _HOSTS.values():
        dirs = ", ".join(h.allowed) or "(none set)"
        lines.append(f"  {h.name}  ->  {h.ssh_host or '(SSH_HOST not set)'}  |  allowed: {dirs}")
    return "\n".join(lines)


@mcp.tool()
def list_directory(host: str, path: str, pattern: str = "*", recursive: bool = False, max_entries: int = 500) -> str:
    """
    List files and folders in an allowed directory on a host, with size and last-modified time (UTC).

    Args:
        host:        Host name from list_hosts (may be empty if only one host is configured)
        path:        Directory to list (absolute, or relative to the host's first allowed directory)
        pattern:     Glob filter on the name e.g. '*.log', '*-haproxy.log'
        recursive:   Include subdirectories (symlinked folders are not followed)
        max_entries: Max entries to return
    """
    try:
        h = _host(host)
        with h.lock:
            d = h.resolve(path)
            if not stat.S_ISDIR(h.sftp.stat(d).st_mode):
                return f"Not a directory: {h.name}:{d}"
            matches = ((full, a) for full, a in _walk(h.sftp, d, recursive) if fnmatch.fnmatch(posixpath.basename(full), pattern))
            picked = sorted(itertools.islice(matches, max_entries + 1), key=lambda x: x[0])
        rows = []
        for full, a in picked[:max_entries]:
            mode = a.st_mode or 0
            kind = "dir " if stat.S_ISDIR(mode) else "link" if stat.S_ISLNK(mode) else "file"
            mtime = datetime.fromtimestamp(a.st_mtime or 0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            rows.append(f"{kind}  {a.st_size or 0:>14,}  {mtime}  {posixpath.relpath(full, d)}")
        if len(picked) > max_entries:
            rows.append(f"... more entries not shown (max_entries={max_entries})")
        return _cap(f"{h.name}:{d}\n" + ("\n".join(rows) if rows else "(no matching entries)"))
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def read_file(host: str, path: str, start_line: int = 1, num_lines: int = 200) -> str:
    """
    Read a range of lines from a text or .gz file on a host.

    Args:
        host:       Host name from list_hosts (may be empty if only one host is configured)
        path:       File to read
        start_line: First line to return (1-based)
        num_lines:  Number of lines to return
    """
    try:
        h = _host(host)
        with h.lock:
            p = h.resolve(path)
            if not stat.S_ISREG(h.sftp.stat(p).st_mode):
                return f"Not a file: {h.name}:{p}"
            if _is_binary(h.sftp, p):
                return f"Binary file, not shown: {h.name}:{p}"
            start_line = max(1, start_line)
            out = [
                f"{n:>8}  {line}"
                for n, line in enumerate(itertools.islice(_iter_lines(h.sftp, p), start_line - 1, start_line - 1 + num_lines), start_line)
            ]
        if not out:
            return f"No lines from line {start_line} onward in {h.name}:{p}"
        return _cap(f"{h.name}:{p} (lines {start_line}-{start_line + len(out) - 1})\n" + "\n".join(out))
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def tail_file(host: str, path: str, lines: int = 100) -> str:
    """
    Return the last N lines of a text or .gz file on a host.

    Args:
        host:  Host name from list_hosts (may be empty if only one host is configured)
        path:  File to read
        lines: Number of lines from the end
    """
    try:
        h = _host(host)
        with h.lock:
            p = h.resolve(path)
            if not stat.S_ISREG(h.sftp.stat(p).st_mode):
                return f"Not a file: {h.name}:{p}"
            if _is_binary(h.sftp, p):
                return f"Binary file, not shown: {h.name}:{p}"
            tail = _tail_lines(h.sftp, p, lines)
        return _cap(f"{h.name}:{p} (last {lines} lines)\n" + "\n".join(tail))
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def search_files(host: str, path: str, pattern: str, file_glob: str = "*", regex: bool = False,
                 ignore_case: bool = True, max_matches: int = 200) -> str:
    """
    Search a file, or every matching file under a directory, on a host (runs grep on that host).
    Hidden files and folders (names starting with '.') are skipped when searching a directory.
    .gz files are searched only when given directly as the path.

    Args:
        host:        Host name from list_hosts (may be empty if only one host is configured)
        path:        File or directory to search
        pattern:     Text to look for (a grep -E extended regex if regex=True)
        file_glob:   Which files to search inside a directory e.g. '*.log'
        regex:       Treat pattern as a regular expression
        ignore_case: Case-insensitive match
        max_matches: Stop after this many matching lines
    """
    try:
        h = _host(host)
        with h.lock:
            target = h.resolve(path)
            is_file = stat.S_ISREG(h.sftp.stat(target).st_mode)
            client = h.client
        base = posixpath.dirname(target) if is_file else target
        tool = "zgrep" if is_file and target.endswith(".gz") else "grep"
        args = [tool, "-nH", "-E" if regex else "-F"]
        if tool == "grep":
            args.append("-I")
        if ignore_case:
            args.append("-i")
        if not is_file:
            args += ["-r", "--exclude-dir=.*", "--exclude=.*", f"--include={file_glob}"]
        args += ["-e", pattern, "--", target]
        cmd = " ".join(shlex.quote(a) for a in args) + f" 2>/dev/null | head -n {max_matches + 1}"
        _, stdout, _ = client.exec_command(cmd, timeout=SEARCH_TIMEOUT)
        lines = stdout.read().decode(errors="replace").splitlines()
        prefix = base.rstrip("/") + "/"
        hits = [l[len(prefix):] if l.startswith(prefix) else l for l in lines[:max_matches]]
        header = f"{len(hits)} match(es) under {h.name}:{target}"
        if len(lines) > max_matches:
            header += f" (stopped at max_matches={max_matches})"
        return _cap(header + "\n" + "\n".join(hits))
    except socket.timeout:
        return f"Error: search timed out after {SEARCH_TIMEOUT}s. Narrow the path or file_glob."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def download_file(host: str, remote_path: str, local_dir: str = "", overwrite: bool = False) -> str:
    """
    Copy a file from a host to this Mac (like scp host:file local_dir/). Max size is MAX_TRANSFER_BYTES (default 10 GB).

    Args:
        host:        Host name from list_hosts
        remote_path: File on the host (must be inside the host's allowed directories)
        local_dir:   Local folder to save into (must be inside LOCAL_TRANSFER_DIRS; default: the first one)
        overwrite:   Replace the local file if it already exists
    """
    tmp = None
    try:
        h = _host(host)
        with h.lock:
            src = h.resolve(remote_path)
            st = h.sftp.stat(src)
            client = h.client
        if not stat.S_ISREG(st.st_mode):
            return f"Not a file: {h.name}:{src}"
        if st.st_size > MAX_TRANSFER_BYTES:
            return f"File is {_human(st.st_size)}, over the {_human(MAX_TRANSFER_BYTES)} limit."
        dest_dir = _local_resolve(local_dir or ".")
        if not dest_dir.is_dir():
            return f"Local folder does not exist: {dest_dir}"
        dest = dest_dir / posixpath.basename(src)
        if dest.exists() and not overwrite:
            return f"{dest} already exists. Set overwrite=True to replace it."
        if shutil.disk_usage(dest_dir).free < st.st_size:
            return f"Not enough free space in {dest_dir} for {_human(st.st_size)}."
        tmp = dest.with_name(dest.name + ".part")
        sftp = client.open_sftp()
        try:
            sftp.get(src, str(tmp))
        finally:
            sftp.close()
        tmp.replace(dest)
        tmp = None
        return f"Downloaded {h.name}:{src} -> {dest} ({_human(st.st_size)})"
    except Exception as e:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        return f"Error: {e}"


@mcp.tool()
def upload_file(host: str, local_path: str, remote_dir: str, overwrite: bool = False) -> str:
    """
    Copy a file from this Mac to a host (like scp local_file host:remote_dir/). Max size is MAX_TRANSFER_BYTES (default 10 GB).
    Uploading into hidden folders, or uploading hidden files, is not allowed.

    Args:
        host:       Host name from list_hosts
        local_path: File on this Mac (must be inside LOCAL_TRANSFER_DIRS)
        remote_dir: Existing folder on the host to put it in (must be inside the host's allowed directories)
        overwrite:  Replace the remote file if it already exists
    """
    sftp = None
    tmp = None
    try:
        h = _host(host)
        src = _local_resolve(local_path)
        if not src.is_file():
            return f"Not a file: {src}"
        size = src.stat().st_size
        if size > MAX_TRANSFER_BYTES:
            return f"File is {_human(size)}, over the {_human(MAX_TRANSFER_BYTES)} limit."
        with h.lock:
            dest_dir = h.resolve(remote_dir)
            if not stat.S_ISDIR(h.sftp.stat(dest_dir).st_mode):
                return f"Not a directory: {h.name}:{dest_dir}"
            client = h.client
        dest = posixpath.join(dest_dir, src.name)
        if any(part.startswith(".") for part in dest.split("/")):
            return f"Uploading to hidden files or folders is not allowed: {h.name}:{dest}"
        sftp = client.open_sftp()
        try:
            sftp.stat(dest)
            exists = True
        except IOError:
            exists = False
        if exists and not overwrite:
            return f"{h.name}:{dest} already exists. Set overwrite=True to replace it."
        tmp = dest + ".part"
        sftp.put(str(src), tmp)
        if exists:
            sftp.remove(dest)
        sftp.rename(tmp, dest)
        tmp = None
        return f"Uploaded {src} -> {h.name}:{dest} ({_human(size)})"
    except Exception as e:
        if sftp is not None and tmp is not None:
            try:
                sftp.remove(tmp)
            except IOError:
                pass
        return f"Error: {e}"
    finally:
        if sftp is not None:
            sftp.close()


if __name__ == "__main__":
    mcp.run()