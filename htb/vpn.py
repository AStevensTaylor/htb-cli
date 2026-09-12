"""OpenVPN management.

Two modes:

  netns  (default)  OpenVPN runs in the root namespace but its tun device is
                    moved into a dedicated network namespace.  Only processes
                    started inside that namespace can reach the HTB lab; the
                    rest of the machine keeps its normal routing.  Optionally a
                    NAT'd veth pair gives the namespace internet access too.

  global            The classic "VPN for the whole box" behaviour.

Everything that needs root is collected into one generated shell script so the
user is asked for their sudo password once per action.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import api, config, ui

PRODUCTS = ("labs", "starting_point", "competitive", "fortresses")

HOST_IP = "10.200.200.1"
NS_IP = "10.200.200.2"
SUBNET = "10.200.200.0/30"


# --- paths ------------------------------------------------------------------

def vpn_dir() -> Path:
    path = config.STATE_DIR / "vpn"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def paths(ns: str) -> dict:
    d = vpn_dir()
    return {
        "conf": d / f"{ns}.ovpn",
        "log": d / f"{ns}.log",
        "pid": d / f"{ns}.pid",
        "ip": d / f"{ns}.ip",
        "ready": d / f"{ns}.ready",
        "meta": d / f"{ns}.json",
        "up": d / f"{ns}-up.sh",
        "routeup": d / f"{ns}-routeup.sh",
        "setup": d / f"{ns}-setup.sh",
        "teardown": d / f"{ns}-teardown.sh",
        "forward": d / f"{ns}.ipforward",
        "resolvbase": d / f"{ns}.resolv-base",
    }


def dev_name(ns: str) -> str:
    # Interface names are capped at 15 characters.
    return f"htb-{ns}"[:15]


def veth_names(ns: str) -> tuple[str, str]:
    return (f"vhtb-{ns}"[:15], f"vhtb-{ns}-ns"[:15])


# --- small helpers ----------------------------------------------------------

def run(argv, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=False, text=True, capture_output=True, **kw)


def sudo_run(argv, why: str = "") -> int:
    """Run a command under sudo, letting it own the terminal for the prompt."""
    if os.geteuid() != 0 and shutil.which("sudo") is None:
        ui.die("`sudo` is required to manage OpenVPN and network namespaces.")
    if why and os.geteuid() != 0:
        ui.info(why)
    cmd = argv if os.geteuid() == 0 else ["sudo", *argv]
    return subprocess.call(cmd)


def netns_exists(ns: str) -> bool:
    return Path("/var/run/netns", ns).exists() or Path("/run/netns", ns).exists()


def pid_of(ns: str) -> int | None:
    try:
        pid = int(paths(ns)["pid"].read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return pid if b"openvpn" in cmdline else None


def read_meta(ns: str) -> dict:
    try:
        return json.loads(paths(ns)["meta"].read_text())
    except (OSError, ValueError):
        return {}


def tun_ip(ns: str) -> str | None:
    try:
        value = paths(ns)["ip"].read_text().strip()
    except OSError:
        return None
    return value or None


def is_up(ns: str, mode: str = "netns") -> bool:
    if pid_of(ns) is None:
        return False
    if mode == "netns":
        return netns_exists(ns) and bool(tun_ip(ns))
    return bool(tun_ip(ns))


def global_tun_present() -> bool:
    """Any system-wide tun interface holding an HTB-looking address?"""
    result = run(["ip", "-o", "-4", "addr", "show"])
    for line in result.stdout.splitlines():
        if " tun" in line and (" 10.10." in line or " 10.13." in line):
            return True
    return False


# --- config download --------------------------------------------------------

def assigned_server(client: api.Client, product: str) -> dict | None:
    data = client.vpn_servers(product).get("data") or {}
    return data.get("assigned") or None


def list_servers(client: api.Client, product: str) -> list:
    data = client.vpn_servers(product).get("data") or {}
    found, seen = [], set()

    def walk(node, location=""):
        if isinstance(node, dict):
            if "id" in node and ("friendly_name" in node or "name" in node):
                key = node["id"]
                if key not in seen:
                    seen.add(key)
                    found.append({
                        "id": node["id"],
                        "name": node.get("friendly_name") or node.get("name"),
                        "location": node.get("location") or location,
                        "clients": node.get("current_clients"),
                        "full": node.get("full"),
                    })
                return
            for key, value in node.items():
                walk(value, key if isinstance(key, str) and len(key) <= 12 else location)
        elif isinstance(node, list):
            for item in node:
                walk(item, location)

    walk(data.get("options") or {})
    assigned = data.get("assigned")
    if assigned:
        for server in found:
            if server["id"] == assigned.get("id"):
                server["assigned"] = True
    return found


def download_config(client: api.Client, product: str, tcp: bool, dest: Path) -> dict:
    server = assigned_server(client, product)
    if not server:
        servers = list_servers(client, product)
        if not servers:
            ui.die(f"No VPN servers available for product {product!r}.")
        server = servers[0]
        ui.info(f"No server assigned; switching to {server['name']}")
        client.vpn_switch(server["id"])
    with ui.Spinner(f"Downloading OpenVPN config for {server.get('friendly_name') or server.get('name')}…"):
        blob = client.ovpn_file(server["id"], tcp=tcp)
    if b"BEGIN CERTIFICATE" not in blob:
        ui.die("HTB did not return a usable .ovpn file (try again in a minute).")
    dest.write_bytes(blob)
    dest.chmod(0o600)
    return {
        "id": server["id"],
        "name": server.get("friendly_name") or server.get("name"),
        "location": server.get("location"),
        "product": product,
        "protocol": "tcp" if tcp else "udp",
    }


# --- generated shell scripts ------------------------------------------------

MASK2CIDR = r"""
mask2cidr() {
  local mask="$1" bits=0 octet
  local IFS=.
  for octet in $mask; do
    case "$octet" in
      255) bits=$((bits+8));; 254) bits=$((bits+7));; 252) bits=$((bits+6));;
      248) bits=$((bits+5));; 240) bits=$((bits+4));; 224) bits=$((bits+3));;
      192) bits=$((bits+2));; 128) bits=$((bits+1));; 0) ;;
      *) echo 32; return;;
    esac
  done
  echo "$bits"
}
"""

UP_SCRIPT = """#!/bin/bash
# Generated by htb-cli. Run by OpenVPN (as root) once the tun device exists.
set -u
NS="%(ns)s"
IPFILE="%(ipfile)s"
dev="${dev:-$1}"
%(mask2cidr)s
ip link set dev "$dev" netns "$NS"
ip -n "$NS" link set dev "$dev" mtu "${tun_mtu:-1500}" up
if [ -n "${ifconfig_netmask:-}" ]; then
  ip -n "$NS" addr add "${ifconfig_local}/$(mask2cidr "$ifconfig_netmask")" dev "$dev"
else
  ip -n "$NS" addr add "${ifconfig_local}" peer "${ifconfig_remote:-$ifconfig_local}" dev "$dev"
fi
printf '%%s' "${ifconfig_local}" > "$IPFILE"
chmod 644 "$IPFILE"
exit 0
"""

ROUTEUP_SCRIPT = """#!/bin/bash
# Generated by htb-cli. Installs the routes HTB pushed into the namespace.
set -u
NS="%(ns)s"
READY="%(ready)s"
RESOLV_BASE="%(resolvbase)s"
dev="${dev:-$1}"
%(mask2cidr)s
i=1
while true; do
  net_var="route_network_$i"; mask_var="route_netmask_$i"
  net="${!net_var:-}"
  [ -z "$net" ] && break
  mask="${!mask_var:-255.255.255.255}"
  ip -n "$NS" route replace "$net/$(mask2cidr "$mask")" dev "$dev" || true
  i=$((i+1))
done

# DNS servers pushed by the lab, kept only when actually routable.
i=1; dns=""
while true; do
  opt_var="foreign_option_$i"; opt="${!opt_var:-}"
  [ -z "$opt" ] && break
  case "$opt" in
    "dhcp-option DNS "*) dns="$dns ${opt#dhcp-option DNS }";;
  esac
  i=$((i+1))
done
mkdir -p "/etc/netns/$NS"
tmp="$(mktemp)"
if [ -f "$RESOLV_BASE" ]; then
  cat "$RESOLV_BASE" > "$tmp"
fi
for server in $dns; do
  # keep a pushed resolver only when the lab routes actually reach it
  if ip netns exec "$NS" ip route get "$server" >/dev/null 2>&1 \\
     && ! grep -qx "nameserver $server" "$tmp" 2>/dev/null; then
    echo "nameserver $server" >> "$tmp"
  fi
done
cat "$tmp" > "/etc/netns/$NS/resolv.conf"
rm -f "$tmp"
touch "$READY"; chmod 644 "$READY"
exit 0
"""

SETUP_SCRIPT = """#!/bin/bash
# Generated by htb-cli: create the namespace, optional NAT'd uplink, start OpenVPN.
set -euo pipefail
NS="%(ns)s"
DEV="%(dev)s"
VETH_HOST="%(veth_host)s"
VETH_NS="%(veth_ns)s"
CONF="%(conf)s"
LOG="%(log)s"
PIDFILE="%(pid)s"
UP="%(up)s"
ROUTEUP="%(routeup)s"
FORWARD_STATE="%(forward)s"
RESOLV_BASE="%(resolvbase)s"
INTERNET=%(internet)d
VETH=%(veth)d

ip netns list | awk '{print $1}' | grep -qx "$NS" || ip netns add "$NS"
ip netns exec "$NS" ip link set lo up

mkdir -p "/etc/netns/$NS"
if [ "$INTERNET" = "1" ]; then
  printf 'nameserver 1.1.1.1\\nnameserver 9.9.9.9\\n' > "$RESOLV_BASE"
else
  printf '# htb-cli: lab-only namespace, no upstream DNS\\n' > "$RESOLV_BASE"
fi
cp "$RESOLV_BASE" "/etc/netns/$NS/resolv.conf"

# Seed the namespace hosts file now: `ip netns exec` bind-mounts it over
# /etc/hosts when a process starts, so it has to exist before the proxy does.
# Later edits rewrite this same inode, which running processes do see.
if [ ! -f "/etc/netns/$NS/hosts" ]; then
  if [ -f /etc/hosts ]; then
    cp /etc/hosts "/etc/netns/$NS/hosts"
  else
    printf '127.0.0.1\tlocalhost\n::1\t\tlocalhost\n' > "/etc/netns/$NS/hosts"
  fi
fi

# systemd-resolved's NSS module stays reachable through the shared /run socket,
# and with the usual "resolve [!UNAVAIL=return] files" order it answers NOTFOUND
# before the hosts file is ever read - which would make target.htb unresolvable
# in here. Give the namespace an nsswitch.conf that reads files, then DNS.
# Written in place rather than renamed, so processes already running in the
# namespace keep seeing it.
if [ -f /etc/nsswitch.conf ]; then
  sed 's/^hosts:.*/hosts: files dns/' /etc/nsswitch.conf > "/etc/netns/$NS/nsswitch.conf"
else
  printf 'hosts: files dns\n' > "/etc/netns/$NS/nsswitch.conf"
fi

# A veth pair to the host carries two things: the optional NAT'd uplink, and
# the proxy socket that tools on the host connect to. It adds no route from the
# lab to the host's other networks on its own.
if [ "$VETH" = "1" ]; then
  if ! ip link show "$VETH_HOST" >/dev/null 2>&1; then
    ip link add "$VETH_HOST" type veth peer name "$VETH_NS"
    ip link set "$VETH_NS" netns "$NS"
    ip addr add %(host_ip)s/30 dev "$VETH_HOST"
    ip link set "$VETH_HOST" up
    ip -n "$NS" addr add %(ns_ip)s/30 dev "$VETH_NS"
    ip -n "$NS" link set "$VETH_NS" up
  fi
fi

if [ "$INTERNET" = "1" ] && [ "$VETH" = "1" ]; then
  ip -n "$NS" route replace default via %(host_ip)s
  if [ ! -f "$FORWARD_STATE" ]; then
    sysctl -n net.ipv4.ip_forward > "$FORWARD_STATE"
  fi
  sysctl -qw net.ipv4.ip_forward=1
  # Prefer iptables: on hosts running Docker/ufw/firewalld the filter FORWARD
  # chain has a DROP policy, and an accept in a separate nft chain does not
  # override it - the rules have to live in the same table.
  if command -v iptables >/dev/null 2>&1; then
    iptables -t nat -C POSTROUTING -s %(subnet)s ! -o "$VETH_HOST" -j MASQUERADE 2>/dev/null || \
      iptables -t nat -A POSTROUTING -s %(subnet)s ! -o "$VETH_HOST" -j MASQUERADE
    iptables -C FORWARD -i "$VETH_HOST" -j ACCEPT 2>/dev/null || \
      iptables -I FORWARD 1 -i "$VETH_HOST" -j ACCEPT
    iptables -C FORWARD -o "$VETH_HOST" -j ACCEPT 2>/dev/null || \
      iptables -I FORWARD 1 -o "$VETH_HOST" -j ACCEPT
  elif command -v nft >/dev/null 2>&1; then
    nft list table ip htbcli >/dev/null 2>&1 || {
      nft add table ip htbcli
      nft add chain ip htbcli postrouting '{ type nat hook postrouting priority srcnat; }'
      nft add chain ip htbcli forward '{ type filter hook forward priority filter; }'
      nft add rule ip htbcli postrouting ip saddr %(subnet)s oifname != "$VETH_HOST" masquerade
      nft add rule ip htbcli forward iifname "$VETH_HOST" accept
      nft add rule ip htbcli forward oifname "$VETH_HOST" accept
    }
  else
    echo "htb-cli: neither nft nor iptables found - namespace will have no internet" >&2
  fi
fi

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  kill "$(cat "$PIDFILE")" 2>/dev/null || true
  sleep 1
fi

exec openvpn \\
  --config "$CONF" \\
  --dev-type tun --dev "$DEV" \\
  --ifconfig-noexec --route-noexec \\
  --script-security 2 --up "$UP" --route-up "$ROUTEUP" \\
  --persist-tun \\
  --daemon "htb-$NS" --log-append "$LOG" --writepid "$PIDFILE"
"""

GLOBAL_SETUP_SCRIPT = """#!/bin/bash
# Generated by htb-cli: system-wide OpenVPN connection.
set -euo pipefail
CONF="%(conf)s"
LOG="%(log)s"
PIDFILE="%(pid)s"
IPFILE="%(ipfile)s"
READY="%(ready)s"
DEV="%(dev)s"
UP="%(up)s"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  kill "$(cat "$PIDFILE")" 2>/dev/null || true
  sleep 1
fi

exec openvpn \\
  --config "$CONF" \\
  --dev-type tun --dev "$DEV" \\
  --script-security 2 --up "$UP" \\
  --daemon "htb-global" --log-append "$LOG" --writepid "$PIDFILE"
"""

GLOBAL_UP_SCRIPT = """#!/bin/bash
# Generated by htb-cli: record the tunnel address of a system-wide connection.
set -u
printf '%%s' "${ifconfig_local:-}" > "%(ipfile)s"
chmod 644 "%(ipfile)s"
touch "%(ready)s"; chmod 644 "%(ready)s"
exit 0
"""

TEARDOWN_SCRIPT = """#!/bin/bash
# Generated by htb-cli: stop OpenVPN and remove everything it created.
set -u
NS="%(ns)s"
VETH_HOST="%(veth_host)s"
PIDFILE="%(pid)s"
FORWARD_STATE="%(forward)s"

if [ -f "$PIDFILE" ]; then
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; fi
  rm -f "$PIDFILE"
fi
pkill -f "openvpn .*--daemon htb-$NS" 2>/dev/null || true

if [ -n "$NS" ]; then
  ip netns del "$NS" 2>/dev/null || true
  rm -rf "/etc/netns/$NS"
fi
ip link del "$VETH_HOST" 2>/dev/null || true

if command -v iptables >/dev/null 2>&1; then
  while iptables -t nat -C POSTROUTING -s %(subnet)s ! -o "$VETH_HOST" -j MASQUERADE 2>/dev/null; do
    iptables -t nat -D POSTROUTING -s %(subnet)s ! -o "$VETH_HOST" -j MASQUERADE || break
  done
  while iptables -C FORWARD -i "$VETH_HOST" -j ACCEPT 2>/dev/null; do
    iptables -D FORWARD -i "$VETH_HOST" -j ACCEPT || break
  done
  while iptables -C FORWARD -o "$VETH_HOST" -j ACCEPT 2>/dev/null; do
    iptables -D FORWARD -o "$VETH_HOST" -j ACCEPT || break
  done
fi
if command -v nft >/dev/null 2>&1 && nft list table ip htbcli >/dev/null 2>&1; then
  nft delete table ip htbcli || true
fi

if [ -f "$FORWARD_STATE" ]; then
  sysctl -qw "net.ipv4.ip_forward=$(cat "$FORWARD_STATE")" 2>/dev/null || true
  rm -f "$FORWARD_STATE"
fi
exit 0
"""


def _write_script(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o700)
    return path


def _render_scripts(ns: str, internet: bool, mode: str, veth: bool = True) -> dict:
    p = paths(ns)
    veth_host, veth_ns = veth_names(ns)
    common = {
        "ns": ns,
        "dev": dev_name(ns),
        "veth_host": veth_host,
        "veth_ns": veth_ns,
        "conf": p["conf"],
        "log": p["log"],
        "pid": p["pid"],
        "ipfile": p["ip"],
        "ready": p["ready"],
        "up": p["up"],
        "routeup": p["routeup"],
        "forward": p["forward"],
        "resolvbase": p["resolvbase"],
        "internet": 1 if internet else 0,
        "veth": 1 if veth else 0,
        "host_ip": HOST_IP,
        "ns_ip": NS_IP,
        "subnet": SUBNET,
        "mask2cidr": MASK2CIDR,
    }
    if mode == "netns":
        _write_script(p["up"], UP_SCRIPT % common)
        _write_script(p["routeup"], ROUTEUP_SCRIPT % common)
        _write_script(p["setup"], SETUP_SCRIPT % common)
    else:
        _write_script(p["up"], GLOBAL_UP_SCRIPT % common)
        _write_script(p["setup"], GLOBAL_SETUP_SCRIPT % common)
    _write_script(p["teardown"], TEARDOWN_SCRIPT % common)
    return p


# --- lifecycle --------------------------------------------------------------

def up(client: api.Client, *, ns: str, product: str = "labs", tcp: bool = False,
       internet: bool = True, veth: bool = True, mode: str = "netns",
       timeout: int = 60, reuse: bool = True) -> dict:
    """Bring the VPN up. Returns metadata about the connection."""
    if shutil.which("openvpn") is None:
        ui.die("openvpn is not installed. Install it (e.g. `sudo pacman -S openvpn`).")

    if reuse and is_up(ns, mode):
        meta = read_meta(ns)
        if meta.get("product") == product:
            return meta
        ui.info(f"Reconnecting: namespace holds {meta.get('product')}, need {product}")
        down(ns=ns, quiet=True)

    p = paths(ns)
    for stale in (p["ready"], p["ip"]):
        stale.unlink(missing_ok=True)
    p["log"].write_text("")  # owned by us so we can read openvpn's output
    p["log"].chmod(0o600)

    meta = download_config(client, product, tcp, p["conf"])
    meta.update({"mode": mode, "ns": ns, "internet": internet,
                 "veth": veth or internet, "dev": dev_name(ns),
                 "started": time.time()})
    _render_scripts(ns, internet, mode, veth=veth or internet)

    where = f"namespace {ui.c(ns, 'bold')}" if mode == "netns" else "this machine"
    rc = sudo_run(["bash", str(p["setup"])],
                  why=f"sudo is needed to start OpenVPN for {where}")
    if rc != 0:
        _abort(ns, "OpenVPN did not start.", _tail(p["log"]))

    with ui.Spinner(f"Connecting to {meta['name']}…") as spin:
        deadline = time.time() + timeout
        started = time.time()
        while time.time() < deadline:
            if p["ready"].exists() and tun_ip(ns):
                break
            log = _tail(p["log"])
            for marker, msg in (
                ("AUTH_FAILED", "HTB rejected the VPN credentials. Regenerate the config."),
                ("Cannot resolve host", "Could not resolve the VPN server's address."),
                ("Cannot load CA", "The .ovpn file looks corrupt; try again."),
                ("TLS Error", "TLS handshake with the VPN server failed."),
                ("Exiting due to fatal error", "OpenVPN hit a fatal error."),
            ):
                if marker in log:
                    spin.stop()
                    _abort(ns, msg, log)
            if pid_of(ns) is None and time.time() - started > 5:
                spin.stop()
                _abort(ns, "OpenVPN exited unexpectedly.", _tail(p["log"]))
            time.sleep(0.4)
        else:
            spin.stop()
            _abort(ns, f"Timed out waiting for the tunnel after {timeout}s.",
                   _tail(p["log"]))

    meta["ip"] = tun_ip(ns)
    p["meta"].write_text(json.dumps(meta))
    if mode == "netns" and internet and not _has_internet(ns):
        ui.warn("The namespace cannot reach the internet (a host firewall is "
                "probably dropping forwarded packets). Lab traffic still works.")
    return meta


def _has_internet(ns: str) -> bool:
    """Quick check; never prompts for a password, assumes fine if it cannot tell."""
    probe = run(["sudo", "-n", "ip", "netns", "exec", ns,
                 "ping", "-c", "1", "-W", "2", "1.1.1.1"])
    if probe.returncode != 0 and "password" in (probe.stderr or "").lower():
        return True
    return probe.returncode == 0


def down(ns: str, quiet: bool = False) -> None:
    from . import proxy  # late import: proxy depends on this module
    proxy.down(ns, quiet=True)
    p = paths(ns)
    if not p["teardown"].exists():
        meta = read_meta(ns)
        _render_scripts(ns, meta.get("internet", True), meta.get("mode", "netns"),
                        veth=meta.get("veth", True))
    sudo_run(["bash", str(p["teardown"])], why="" if quiet else "sudo is needed to tear down the VPN")
    for stale in (p["ip"], p["ready"], p["pid"], p["meta"], p["resolvbase"]):
        stale.unlink(missing_ok=True)
    if not quiet:
        ui.success("VPN stopped.")


def _abort(ns: str, message: str, log: str = "") -> None:
    """Report a failed connection, leaving nothing behind."""
    down(ns, quiet=True)
    detail = "\n".join(line for line in log.strip().splitlines()
                        if "DEPRECATED" not in line)[-800:]
    ui.die(message + (f"\n\n{detail}" if detail.strip() else
                      f"\nSee {paths(ns)['log']}"))


def _tail(path: Path, limit: int = 8000) -> str:
    try:
        return path.read_text(errors="replace")[-limit:]
    except OSError:
        return ""


# --- running things inside the namespace ------------------------------------

def ns_command(ns: str, argv: list, env: dict | None = None,
               non_interactive: bool = False) -> list:
    """Build the argv that runs `argv` inside the namespace as the current user."""
    inner = list(argv)
    if env:
        inner = ["env", *[f"{k}={v}" for k, v in env.items()], *inner]

    if os.geteuid() == 0:
        return ["ip", "netns", "exec", ns, *inner]

    uid, gid = os.getuid(), os.getgid()
    if shutil.which("setpriv"):
        drop = ["setpriv", f"--reuid={uid}", f"--regid={gid}", "--init-groups",
                "--inh-caps=-all", "--"]
    else:
        drop = ["sudo", "-u", f"#{uid}", "--"]
    sudo = ["sudo", "-n", "-E"] if non_interactive else ["sudo", "-E"]
    return [*sudo, "ip", "netns", "exec", ns, *drop, *inner]


def exec_in_ns(ns: str, argv: list, env: dict | None = None) -> int:
    return subprocess.call(ns_command(ns, argv, env))


def ns_report(ns: str) -> str:
    """Human summary of the namespace's addresses (needs root, best effort)."""
    result = run(["sudo", "-n", "ip", "-n", ns, "-o", "-4", "addr", "show"])
    return result.stdout.strip()
