import os
import httpx
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

_env      = os.environ.get("LOKI_ENV", "dev").upper()
LOKI_URL  = os.environ.get(f"LOKI_URL_{_env}", "").rstrip("/")
LOKI_USER = os.environ.get(f"LOKI_USER_{_env}", os.environ.get("LOKI_USER", ""))
LOKI_PASS = os.environ.get(f"LOKI_PASS_{_env}", os.environ.get("LOKI_PASS", ""))

mcp = FastMCP("loki")


def _auth():
    return (LOKI_USER, LOKI_PASS) if LOKI_USER else None

def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _ago(h=1):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


@mcp.tool()
def query_logs(logql: str, start: str = "", end: str = "", limit: int = 200) -> str:
    """
    Run a LogQL query and return matching log lines.

    Args:
        logql:  Full LogQL expression e.g. '{cluster="us-west-2-c5b2", service="mbs"} |= "ERROR"'
        start:  RFC3339 start time (default: 1 hour ago)
        end:    RFC3339 end time (default: now)
        limit:  Max log lines to return
    """
    params = {
        "query": logql,
        "limit": limit,
        "start": start or _ago(1),
        "end":   end   or _now(),
        "direction": "forward",
    }
    try:
        r = httpx.get(f"{LOKI_URL}/loki/api/v1/query_range",
                      params=params, auth=_auth(), timeout=30)
        r.raise_for_status()
        streams = r.json().get("data", {}).get("result", [])
        if not streams:
            return "No logs found."
        lines = []
        for s in streams:
            for ts, line in s.get("values", []):
                ts_fmt = datetime.fromtimestamp(int(ts) / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                lines.append((ts, f"[{ts_fmt}] {line}"))
        lines.sort(key=lambda x: x[0])
        return "\n".join(l for _, l in lines[-limit:])
    except httpx.HTTPStatusError as e:
        return f"HTTP {e.response.status_code}: {e.response.text}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def count_occurrences(logql: str, pattern: str, hours: int = 1) -> str:
    """
    Count how many log lines match a pattern over the last N hours.

    Args:
        logql:   Stream selector e.g. '{cluster="us-west-2-c5b2", service="mbs"}'
        pattern: Text to search for
        hours:   Look-back window in hours
    """
    full_query = f'{logql} |= "{pattern}"'
    params = {
        "query": full_query,
        "limit": 5000,
        "start": _ago(hours),
        "end":   _now(),
    }
    try:
        r = httpx.get(f"{LOKI_URL}/loki/api/v1/query_range",
                      params=params, auth=_auth(), timeout=30)
        r.raise_for_status()
        count = sum(
            len(s.get("values", []))
            for s in r.json().get("data", {}).get("result", [])
        )
        return f'Pattern "{pattern}" matched {count} log line(s) in the last {hours}h.'
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def error_summary(logql: str, hours: int = 1) -> str:
    """
    Count ERROR, WARN, FATAL occurrences per level over the last N hours.

    Args:
        logql: Stream selector e.g. '{cluster="us-west-2-c5b2", service="mbs"}'
        hours: Look-back window in hours
    """
    results = {}
    for level in ["ERROR", "WARN", "FATAL", "Exception"]:
        query = f'{logql} |= "{level}"'
        params = {"query": query, "limit": 5000, "start": _ago(hours), "end": _now()}
        try:
            r = httpx.get(f"{LOKI_URL}/loki/api/v1/query_range",
                          params=params, auth=_auth(), timeout=30)
            if r.status_code == 200:
                count = sum(
                    len(s.get("values", []))
                    for s in r.json().get("data", {}).get("result", [])
                )
                if count:
                    results[level] = count
        except Exception:
            pass
    if not results:
        return f"No errors/warnings in the last {hours}h."
    lines = [f"Last {hours}h error summary:"]
    for level, count in sorted(results.items(), key=lambda x: -x[1]):
        lines.append(f"  {level}: {count}")
    return "\n".join(lines)


@mcp.tool()
def list_labels() -> str:
    """List all label names available in Loki."""
    try:
        r = httpx.get(f"{LOKI_URL}/loki/api/v1/labels", auth=_auth(), timeout=10)
        r.raise_for_status()
        labels = sorted(r.json().get("data", []))
        return "Labels:\n" + "\n".join(f"  {l}" for l in labels)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def list_label_values(label: str) -> str:
    """
    List all values for a given label.

    Args:
        label: e.g. 'cluster', 'service', 'node'
    """
    try:
        r = httpx.get(f"{LOKI_URL}/loki/api/v1/label/{label}/values",
                      auth=_auth(), timeout=10)
        r.raise_for_status()
        values = sorted(r.json().get("data", []))
        return f"Values for '{label}':\n" + "\n".join(f"  {v}" for v in values)
    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run()