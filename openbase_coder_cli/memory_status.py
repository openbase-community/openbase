"""Small memory snapshot for the authenticated service-status poll."""

from __future__ import annotations

import heapq

import psutil

from openbase_coder_cli.workspace_memory import memory_snapshot


def memory_status_payload() -> dict:
    memory = memory_snapshot()
    percent = 100 * memory.used / memory.total if memory.total else 0
    # Process names/RSS only: never expose arguments, environment or paths.
    # Skip enumeration while healthy to keep the regular status poll cheap.
    processes = []
    if percent > 85:
        for proc in psutil.process_iter(attrs=["name", "memory_info"], ad_value=None):
            info = proc.info
            if info["memory_info"] is not None and info["name"]:
                processes.append({
                    "name": info["name"][:64],
                    "rss_bytes": info["memory_info"].rss,
                })
    return {
        "used_bytes": memory.used,
        "total_bytes": memory.total,
        "percent": round(percent, 2),
        "top_processes": heapq.nlargest(3, processes, key=lambda p: p["rss_bytes"]),
    }
