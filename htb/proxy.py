"""Lifecycle for the in-namespace SOCKS5/HTTP proxy.

The proxy process itself lives in `htb/socks.py`.  It is started inside the lab
namespace but bound to the namespace end of the veth pair, which the host can
reach - so Burp, ZAP, curl or proxychains on the host tunnel into the lab
without being launched inside the namespace.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import config, ui, vpn

DEFAULT_PORT = 1080


def paths(ns: str) -> dict:
    d = vpn.vpn_dir()
    return {
        "pid": d / f"{ns}-proxy.pid",
        "log": d / f"{ns}-proxy.log",
        "meta": d / f"{ns}-proxy.json",
        "fwd_pid": d / f"{ns}-proxy-fwd.pid",
        "fwd_log": d / f"{ns}-proxy-fwd.log",
    }


def read_meta(ns: str) -> dict:
    try:
        return json.loads(paths(ns)["meta"].read_text())
    except (OSError, ValueError):
        return {}


def _pid_from(path: Path, needle: str = "htb.socks") -> int | None:
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return pid if needle.encode() in cmdline else None


def pid_of(ns: str) -> int | None:
    return _pid_from(paths(ns)["pid"])


def forwarder_pid(ns: str) -> int | None:
    return _pid_from(paths(ns)["fwd_pid"])


def is_running(ns: str) -> bool:
    return pid_of(ns) is not None


def endpoint(ns: str) -> tuple[str, int] | None:
    meta = read_meta(ns)
    if not meta.get("host"):
        return None
    return meta["host"], int(meta.get("port", DEFAULT_PORT))


def url(ns: str, scheme: str = "socks5") -> str | None:
    where = endpoint(ns)
    if not where:
        return None
    meta = read_meta(ns)
    credentials = f"{meta['auth'].split(':')[0]}:***@" if meta.get("auth") else ""
    return f"{scheme}://{credentials}{where[0]}:{where[1]}"


# --- readiness --------------------------------------------------------------

def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def handshake(host: str, port: int, auth: str | None = None,
              timeout: float = 3.0) -> bool:
    """Verify something on the other end actually speaks SOCKS5."""
    method = 0x02 if auth else 0x00
    try:
        with socket.create_connection((host, port), timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(bytes([0x05, 0x01, method]))
            reply = sock.recv(2)
    except OSError:
        return False
    return len(reply) == 2 and reply[0] == 0x05 and reply[1] == method


# --- lifecycle --------------------------------------------------------------

def _python() -> str:
    return sys.executable or "python3"


def _package_root() -> str:
    """Directory that must be on PYTHONPATH for `-m htb.socks` to import."""
    return str(Path(__file__).resolve().parent.parent)


def _spawn(argv: list, log: Path, env: dict | None = None) -> subprocess.Popen:
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log, "a")
    return subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
        start_new_session=True, env={**os.environ, **(env or {})})


def _ensure_sudo() -> bool:
    """Refresh the sudo timestamp interactively so the daemon can start detached."""
    if os.geteuid() == 0:
        return True
    if subprocess.call(["sudo", "-n", "true"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
        return True
    ui.info("sudo is needed to start the proxy inside the namespace")
    return subprocess.call(["sudo", "-v"]) == 0


def up(ns: str, *, port: int | None = None, listen: str | None = None,
       auth: str | None = None, localhost: bool | None = None,
       quiet: bool = False) -> dict | None:
    """Start the proxy inside `ns`. Returns its metadata, or None on failure."""
    port = int(port or config.get("proxy_port") or DEFAULT_PORT)
    auth = auth if auth is not None else config.get("proxy_auth")
    localhost = config.get("proxy_localhost") if localhost is None else localhost
    vpn_meta = vpn.read_meta(ns)

    if vpn_meta.get("mode") == "global":
        if not quiet:
            ui.warn("A system-wide VPN needs no proxy - everything already routes to the lab.")
        return None
    if vpn_meta.get("veth") is False:
        ui.warn("This namespace was started with no veth link, so a proxy would be "
                "unreachable from the host. Reconnect without --no-veth.")
        return None

    listen = listen or vpn.NS_IP
    if listen.startswith("127.") or listen == "::1":
        ui.warn(f"{listen} is the namespace's own loopback - the host cannot reach it. "
                f"Use {vpn.NS_IP} (the default) or 0.0.0.0.")
        return None
    host_view = vpn.NS_IP if listen in ("0.0.0.0", vpn.NS_IP) else listen

    if is_running(ns):
        current = read_meta(ns)
        if int(current.get("port", 0)) == port and current.get("host") == host_view:
            return current
        down(ns, quiet=True)

    p = paths(ns)
    p["pid"].unlink(missing_ok=True)
    if not _ensure_sudo():
        ui.warn("Could not obtain sudo; proxy not started.")
        return None

    argv = vpn.ns_command(
        ns,
        [_python(), "-m", "htb.socks", "--listen", listen, "--port", str(port),
         "--pidfile", str(p["pid"])] + (["--auth", auth] if auth else []),
        env={"PYTHONPATH": _package_root()},
        non_interactive=True,
    )
    _spawn(argv, p["log"])

    deadline = time.time() + 8
    while time.time() < deadline:
        if pid_of(ns) and port_open(host_view, port):
            break
        time.sleep(0.2)
    else:
        tail = _tail(p["log"])
        ui.warn(f"Proxy did not come up on {host_view}:{port}." + (f"\n{tail}" if tail else ""))
        return None

    meta = {"ns": ns, "host": host_view, "listen": listen, "port": port,
            "auth": auth, "pid": pid_of(ns), "started": time.time(),
            "localhost": False}

    if localhost:
        meta["localhost"] = _forwarder_up(ns, host_view, port)

    p["meta"].write_text(json.dumps(meta))
    if not quiet:
        announce(ns, meta)
    return meta


def _forwarder_up(ns: str, host: str, port: int) -> bool:
    """Expose the namespace proxy on 127.0.0.1 as well (no root needed)."""
    p = paths(ns)
    p["fwd_pid"].unlink(missing_ok=True)
    if port_open("127.0.0.1", port):
        ui.warn(f"127.0.0.1:{port} is already in use; skipping the localhost forwarder.")
        return False
    _spawn([_python(), "-m", "htb.socks", "--forward", f"{host}:{port}",
            "--listen", "127.0.0.1", "--port", str(port),
            "--pidfile", str(p["fwd_pid"])],
           p["fwd_log"], env={"PYTHONPATH": _package_root()})
    deadline = time.time() + 5
    while time.time() < deadline:
        if forwarder_pid(ns) and port_open("127.0.0.1", port):
            return True
        time.sleep(0.2)
    ui.warn("Localhost forwarder did not start; use the namespace address instead.")
    return False


def down(ns: str, quiet: bool = False) -> bool:
    """Stop the proxy (and its localhost forwarder)."""
    stopped = False
    for key, pid in (("pid", pid_of(ns)), ("fwd_pid", forwarder_pid(ns))):
        if pid is None:
            paths(ns)[key].unlink(missing_ok=True)
            continue
        stopped = True
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        for _ in range(20):
            if not Path(f"/proc/{pid}").exists():
                break
            time.sleep(0.1)
        else:
            # Started under sudo and no longer ours to kill? Ask root once.
            subprocess.call(["sudo", "-n", "kill", "-TERM", str(pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        paths(ns)[key].unlink(missing_ok=True)
    paths(ns)["meta"].unlink(missing_ok=True)
    if stopped and not quiet:
        ui.success("Proxy stopped.")
    elif not stopped and not quiet:
        ui.info("Proxy is not running.")
    return stopped


def maybe_up(args, vpn_meta: dict | None) -> dict | None:
    """Start the proxy after a VPN connect, honouring flags and config."""
    if not vpn_meta or vpn_meta.get("mode") != "netns":
        return None
    if getattr(args, "no_proxy", False) or not config.get("proxy"):
        return None
    return up(vpn_meta.get("ns") or config.get("netns"),
              port=getattr(args, "proxy_port", None),
              auth=getattr(args, "proxy_auth", None),
              localhost=getattr(args, "proxy_localhost", None) or None)


def announce(ns: str, meta: dict | None = None) -> None:
    meta = meta or read_meta(ns)
    if not meta:
        return
    where = f"{meta['host']}:{meta['port']}"
    ui.success(f"Lab proxy listening on {ui.c(where, 'bold')} "
               f"(socks5 + http, inside namespace {meta['ns']})")
    hints = [("burp", f"Settings → Network → Connections → Upstream proxy: "
                      f"SOCKS host {meta['host']}, port {meta['port']}"),
             ("curl", f"curl --socks5-hostname {where} http://target.htb/")]
    if meta.get("localhost"):
        hints.insert(0, ("localhost", f"also on 127.0.0.1:{meta['port']}"))
    if meta.get("auth"):
        hints.append(("auth", f"user {meta['auth'].split(':')[0]} (password as configured)"))
    ui.kv(hints, indent=2)


def _tail(path: Path, limit: int = 1200) -> str:
    try:
        return path.read_text(errors="replace")[-limit:].strip()
    except OSError:
        return ""


def status(ns: str) -> dict:
    meta = read_meta(ns)
    pid = pid_of(ns)
    where = endpoint(ns)
    return {
        "running": pid is not None,
        "pid": pid,
        "host": meta.get("host"),
        "port": meta.get("port"),
        "listen": meta.get("listen"),
        "auth": bool(meta.get("auth")),
        "localhost": bool(meta.get("localhost")) and forwarder_pid(ns) is not None,
        "reachable": bool(where) and handshake(where[0], where[1], meta.get("auth")),
        "log": str(paths(ns)["log"]),
    }
