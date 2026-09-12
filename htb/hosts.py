"""Hosts-file entries for spawned machines.

In namespace mode the entries go to /etc/netns/<ns>/hosts, which `ip netns
exec` bind-mounts over /etc/hosts *inside the namespace only* - the real
/etc/hosts is never touched.  In global mode we fall back to editing
/etc/hosts, marking our lines so they can be removed again.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import ui

MARKER = "# htb-cli"
SYSTEM_HOSTS = Path("/etc/hosts")


def names_for(machine_name: str) -> list:
    slug = str(machine_name).strip().lower().replace(" ", "-")
    if not slug:
        return []
    return [f"{slug}.htb", slug]


def _write_root(path: Path, content: str, prompt: bool = True) -> bool:
    """Write `content` to a root-owned path. `prompt=False` never asks for a password."""
    if os.geteuid() == 0:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return True
    sudo = ["sudo"] if prompt else ["sudo", "-n"]
    subprocess.run([*sudo, "mkdir", "-p", str(path.parent)], check=False,
                   stderr=subprocess.DEVNULL if not prompt else None)
    proc = subprocess.run([*sudo, "tee", str(path)], input=content, text=True,
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL if not prompt else None)
    return proc.returncode == 0


def _strip_ours(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if MARKER not in line)


def ns_hosts_path(ns: str) -> Path:
    return Path("/etc/netns") / ns / "hosts"


def update(ip: str, machine_name: str, ns: str | None = None) -> bool:
    """Point `<name>.htb` and `<name>` at `ip`. Returns True on success."""
    names = names_for(machine_name)
    if not ip or not names:
        return False

    base = SYSTEM_HOSTS.read_text() if SYSTEM_HOSTS.exists() else ""
    target = ns_hosts_path(ns) if ns else SYSTEM_HOSTS
    if ns:
        existing = target.read_text() if target.exists() else base
    else:
        existing = base

    body = _strip_ours(existing).rstrip()
    entry = f"{ip}\t{' '.join(names)}\t{MARKER}"
    content = f"{body}\n{entry}\n"
    if not _write_root(target, content):
        ui.warn(f"Could not update {target}")
        return False
    return True


def clear(ns: str | None = None, prompt: bool = False) -> bool:
    """Remove our entries again. Best effort - never blocks on a sudo prompt."""
    target = ns_hosts_path(ns) if ns else SYSTEM_HOSTS
    if not target.exists():
        return True
    text = target.read_text()
    if MARKER not in text:
        return True
    return _write_root(target, _strip_ours(text).rstrip() + "\n", prompt=prompt)
