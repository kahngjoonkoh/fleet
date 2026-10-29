"""Facts about this machine for the Devices tab. Standard library only, so fleetd can also run this
file on another machine with `ssh HOST python3 -` before Fleet is installed there."""

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _run(*cmd, env=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5, env=env).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _os_name():
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return platform.system()


def _cpu_model():
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor()


def _memory():
    m = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            m[k] = int(v.split()[0]) * 1024
    total = m.get("MemTotal", 0)
    return {"total": total, "used": total - m.get("MemAvailable", total)}


def _disk(path):
    try:
        d = shutil.disk_usage(path)
        return {"path": path, "total": d.total, "used": d.used}
    except OSError:
        return None


def _gpus():
    if not shutil.which("nvidia-smi"):
        return []
    out = _run("nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
               "--format=csv,noheader,nounits")
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 5:
            name, used, total, util, temp = parts
            num = lambda s: int(s) if s.isdigit() else None
            gpus.append({"name": name, "mem_used": (num(used) or 0) * 2**20, "mem_total": (num(total) or 0) * 2**20,
                         "util": num(util), "temp": num(temp)})
    return gpus


def _claude_processes():
    n = 0
    for pid in filter(str.isdigit, os.listdir("/proc")):
        if _read(f"/proc/{pid}/comm").strip() == "claude":
            n += 1
    return n


def _fleetd():
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    state = _run("systemctl", "--user", "is-active", "fleetd", env=env) or "unknown"
    if state in ("inactive", "unknown") and not os.path.exists(os.path.expanduser("~/.config/systemd/user/fleetd.service")):
        return "not installed"
    return state


def _hardware():
    home = os.path.expanduser("~")
    disks = [d for d in (_disk("/"), _disk(home)) if d]
    if len(disks) == 2 and os.stat("/").st_dev == os.stat(home).st_dev:
        disks = disks[:1]
    return {
        "os": _os_name(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "uptime": int(float((_read("/proc/uptime").split() or ["0"])[0])),
        "cpus": os.cpu_count(),
        "cpu_model": _cpu_model(),
        "load": [round(x, 2) for x in os.getloadavg()],
        "memory": _memory(),
        "disks": disks,
        "gpus": _gpus(),
    }


def collect(hardware=False):
    """Always: is fleetd here, and what Claude is running. With hardware=True also OS, CPU, memory,
    disks and GPUs, which the hub shows only when installed with --show-hardware."""
    tmux = _run("tmux", "list-sessions", "-F", "#{session_name}") if shutil.which("tmux") else ""
    facts = {
        "hostname": socket.gethostname(),
        "tmux_sessions": tmux.splitlines() if tmux else [],
        "claude_processes": _claude_processes(),
        "fleetd": _fleetd(),
        "collected_at": int(time.time()),
    }
    if hardware:
        facts["hardware"] = _hardware()
    return facts


if __name__ == "__main__":
    print(json.dumps(collect(hardware="--hardware" in sys.argv)))
