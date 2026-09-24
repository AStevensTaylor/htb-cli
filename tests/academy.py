"""Tests for the HTB Academy VPN: importing its .ovpn and keeping it off the labs API.

    python3 tests/academy.py

No account, no network, no root: `vpn.up` is stubbed where it would call sudo.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="htb-academy-")
os.environ["HTB_CONFIG_DIR"] = _tmp + "/config"
os.environ["HTB_CACHE_DIR"] = _tmp + "/cache"
os.environ["HTB_STATE_DIR"] = _tmp + "/state"
os.environ.pop("HTB_TOKEN", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from htb import cli, vpn  # noqa: E402
from htb.commands import common  # noqa: E402

checks = []


def check(label, ok):
    checks.append(bool(ok))
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


def run(*argv):
    try:
        return cli.main(list(argv))
    except SystemExit as exc:
        return exc.code or 0


OVPN = """client
dev tun
proto tcp
remote edge-eu-academy-2.hackthebox.eu 443
<ca>
-----BEGIN CERTIFICATE-----
MIIB
-----END CERTIFICATE-----
</ca>
"""

src = Path(_tmp) / "academy-regular.ovpn"
src.write_text(OVPN)
junk = Path(_tmp) / "junk.ovpn"
junk.write_text("not a config\n")

print("missing config")
check("vpn up --academy without an import fails cleanly", run("vpn", "up", "--academy") != 0)

print("import")
check("junk file is rejected", run("vpn", "up", "--ovpn", str(junk)) != 0)
check("junk file was not stored", not vpn.local_config_path("academy").exists())

calls = []
vpn.up = lambda client, **kw: (calls.append((client, kw)),
                               {**vpn.local_config(kw["product"], Path(_tmp) / "out.ovpn"),
                                "mode": "netns", "ns": kw["ns"], "internet": True})[1]
check("vpn up --ovpn imports and connects, with no token",
      run("vpn", "up", "--ovpn", str(src), "--no-proxy", "--netns", "t") == 0)
stored = vpn.local_config_path("academy")
check("config stored 0600", stored.exists() and stored.stat().st_mode & 0o777 == 0o600)
check("connected as product academy", calls and calls[-1][1]["product"] == "academy")
check("labs API client not used", calls and calls[-1][0] is None)

print("reuse")
check("vpn up --academy reuses the imported file",
      run("vpn", "up", "--academy", "--no-proxy", "--netns", "t") == 0)
check("--product academy works too",
      run("vpn", "up", "--product", "academy", "--no-proxy", "--netns", "t") == 0
      and calls[-1][1]["product"] == "academy")

meta = vpn.local_config("academy", Path(_tmp) / "out.ovpn")
check("server name from the remote line", meta["name"] == "edge-eu-academy-2.hackthebox.eu")
check("protocol from the proto line", meta["protocol"] == "tcp")

print("labs-only commands")
common.client = lambda args: (_ for _ in ()).throw(AssertionError("labs API called"))
for sub in ("servers", "config"):
    check(f"vpn {sub} --product academy refuses without calling the API",
          run("vpn", sub, "--product", "academy") == 1)

import shutil  # noqa: E402
shutil.rmtree(_tmp, ignore_errors=True)
print(f"\n{sum(checks)}/{len(checks)} passed")
sys.exit(0 if all(checks) else 1)
