"""Standalone background poller.

Run this when you want change detection to keep working independently of the MCP server's
lifetime - under systemd on a server, Task Scheduler on Windows, or just left running:

    uv run python -m mcv_mcp.poller

It shares the same SQLite snapshot as the server, so `whats_new` picks up whatever it
found. Set MCV_POLL_ENABLED=0 for the server itself so the two do not poll in parallel.
"""

from __future__ import annotations

from .config import load_config
from .sync import run_forever

if __name__ == "__main__":
    run_forever(load_config())
