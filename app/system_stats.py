"""Host / GPU telemetry for the admin dashboard.

Jetson Thor uses unified memory, so nvidia-smi reports GPU memory as N/A;
system RAM from /proc/meminfo is the meaningful number there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

_CACHE: dict[str, Any] = {"at": 0.0, "value": None}
_CACHE_TTL_S = 1.0

_GPU_FIELDS = (
    "name",
    "temperature.gpu",
    "utilization.gpu",
    "power.draw",
    "clocks.gr",
    "clocks.max.gr",
    "memory.used",
    "memory.total",
    "driver_version",
)


def _number(value: str) -> float | str | None:
    value = value.strip()
    if not value or value.startswith("[") or value in {"N/A", "Not Supported"}:
        return None
    try:
        return float(value.split()[0])
    except ValueError:
        return value


def _gpus() -> list[dict[str, Any]]:
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                f"--query-gpu={','.join(_GPU_FIELDS)}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    gpus = []
    for line in out.strip().splitlines():
        values = [v.strip() for v in line.split(",")]
        row = dict(zip(_GPU_FIELDS, values))
        gpus.append(
            {
                "name": row.get("name"),
                "driver": row.get("driver_version"),
                "temperature_c": _number(row.get("temperature.gpu", "")),
                "utilization_pct": _number(row.get("utilization.gpu", "")),
                "power_w": _number(row.get("power.draw", "")),
                "clock_mhz": _number(row.get("clocks.gr", "")),
                "clock_max_mhz": _number(row.get("clocks.max.gr", "")),
                "memory_used_mb": _number(row.get("memory.used", "")),
                "memory_total_mb": _number(row.get("memory.total", "")),
            }
        )
    return gpus


def _meminfo() -> dict[str, float]:
    values: dict[str, float] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, rest = line.split(":", 1)
            values[key] = float(rest.split()[0]) / 1024 / 1024  # kB -> GB
    except (OSError, ValueError):
        return {}
    total = values.get("MemTotal", 0.0)
    available = values.get("MemAvailable", 0.0)
    return {
        "total_gb": round(total, 1),
        "available_gb": round(available, 1),
        "used_gb": round(total - available, 1),
    }


def _process_rss_gb() -> float | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(float(line.split()[1]) / 1024 / 1024, 2)
    except (OSError, ValueError):
        pass
    return None


def _cpu_temp_c() -> float | None:
    for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            if "cpu" in (zone / "type").read_text().lower():
                return round(int((zone / "temp").read_text()) / 1000, 1)
        except (OSError, ValueError):
            continue
    return None


def _jetson_model() -> str | None:
    try:
        return Path("/proc/device-tree/model").read_text().strip("\x00\n ")
    except OSError:
        return None


def snapshot() -> dict[str, Any]:
    now = time.monotonic()
    if _CACHE["value"] is not None and now - _CACHE["at"] < _CACHE_TTL_S:
        return _CACHE["value"]
    try:
        load1, load5, _ = os.getloadavg()
    except OSError:
        load1 = load5 = None
    value = {
        "time": time.time(),
        "host": os.uname().nodename,
        "board": _jetson_model(),
        "cpu_count": os.cpu_count(),
        "load_avg": [load1, load5],
        "cpu_temp_c": _cpu_temp_c(),
        "memory": _meminfo(),
        "process_rss_gb": _process_rss_gb(),
        "gpus": _gpus(),
    }
    _CACHE.update(at=now, value=value)
    return value
