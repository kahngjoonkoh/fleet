"""fleetd: the per-host Fleet daemon. It listens only on a unix socket that fleet-web reaches, and
does the host-side work as this user. So far that is the terminal: one login shell per connection."""

import asyncio
import fcntl
import json
import os
import pty
import pwd
import signal
import socket
import struct
import termios
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from . import SOCKET, STATE_DIR, config, info

HOST = socket.gethostname()
INFO_SCRIPT = Path(info.__file__).read_bytes()
# ssh connection sharing, kept out of STATE_DIR because fleet-web can see that directory.
SSH_MUX = Path.home() / ".cache/fleet"


async def health(request):
    return web.json_response({"ok": True, "host": HOST})


async def probe(device, hardware):
    """Runs info.py on another machine over ssh; it needs only python3 there, not Fleet."""
    entry = {"name": device["name"], "ssh": device["ssh"], "local": False}
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ControlMaster=auto",
            "-o", f"ControlPath={SSH_MUX}/%C", "-o", "ControlPersist=120", device["ssh"],
            "python3", "-", *(["--hardware"] if hardware else []),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(INFO_SCRIPT), 15)
        except TimeoutError:
            proc.kill()
            return entry | {"online": False, "error": "no answer within 15 seconds"}
        if proc.returncode:
            lines = err.decode(errors="replace").strip().splitlines()
            return entry | {"online": False, "error": lines[-1] if lines else f"ssh exited {proc.returncode}"}
        return entry | {"online": True, "info": json.loads(out)}
    except (OSError, ValueError) as e:
        return entry | {"online": False, "error": str(e)}


async def devices(request):
    """This machine plus every machine in config.toml, checked in parallel. ?hardware=1 adds
    OS, CPU, memory, disk and GPU facts."""
    hardware = request.query.get("hardware") == "1"
    SSH_MUX.mkdir(mode=0o700, parents=True, exist_ok=True)
    local = asyncio.to_thread(info.collect, hardware)
    remote = [probe(d, hardware) for d in config.devices()]
    mine, *others = await asyncio.gather(local, *remote)
    here = {"name": HOST, "ssh": None, "local": True, "online": True, "info": mine}
    return web.json_response({"devices": [here, *others], "checked_at": int(time.time())})


def spawn_shell():
    """Forks a login shell on a new pty and returns (pid, master fd)."""
    user = pwd.getpwuid(os.getuid())
    pid, fd = pty.fork()
    if pid == 0:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("INVOCATION_ID", "JOURNAL_", "LISTEN_"))}
        env.update(TERM="xterm-256color", COLORTERM="truecolor", HOME=user.pw_dir, SHELL=user.pw_shell,
                   USER=user.pw_name, LOGNAME=user.pw_name)
        shell = user.pw_shell or "/bin/bash"
        try:
            os.chdir(user.pw_dir)
            os.execve(shell, ["-" + os.path.basename(shell)], env)  # a leading "-" makes it a login shell
        finally:
            os._exit(127)
    os.set_blocking(fd, False)
    return pid, fd


def resize(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def end_shell(pid):
    """SIGHUP like a closed ssh session, then SIGKILL if it hasn't exited within two seconds."""
    try:
        for sig in (signal.SIGHUP, signal.SIGKILL):
            os.kill(pid, sig)
            for _ in range(20):
                if os.waitpid(pid, os.WNOHANG) != (0, 0):
                    return
                await asyncio.sleep(0.1)
    except (ProcessLookupError, ChildProcessError):
        pass


async def terminal(request):
    """Binary frames carry terminal bytes both ways; text frames carry {"resize": [cols, rows]}."""
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)
    pid, fd = spawn_shell()
    loop = asyncio.get_running_loop()
    output = asyncio.Queue()

    def readable():
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO: the shell exited
            data = b""
        output.put_nowait(data)
        if not data:
            loop.remove_reader(fd)

    async def send_output():
        while data := await output.get():
            await ws.send_bytes(data)
        await ws.close()

    async def write(data):
        while data:
            try:
                data = data[os.write(fd, data):]
            except BlockingIOError:
                await asyncio.sleep(0.01)

    loop.add_reader(fd, readable)
    sender = asyncio.create_task(send_output())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                await write(msg.data)
            elif msg.type == WSMsgType.TEXT:
                cols, rows = json.loads(msg.data)["resize"]
                resize(fd, max(1, int(cols)), max(1, int(rows)))
    finally:
        sender.cancel()
        try:
            loop.remove_reader(fd)
        except (ValueError, OSError):
            pass
        os.close(fd)
        await end_shell(pid)
    return ws


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o750)
    SOCKET.unlink(missing_ok=True)
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/terminal", terminal)
    app.router.add_get("/devices", devices)

    async def serve():
        runner = web.AppRunner(app)
        await runner.setup()
        await web.UnixSite(runner, str(SOCKET)).start()
        # Group-writable so fleet-web, which joins this user's group, can connect; nobody else can.
        os.chmod(SOCKET, 0o660)
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        await stop.wait()
        await runner.cleanup()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
