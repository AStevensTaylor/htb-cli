"""Tests for moving the tunnel onto the machine's lab server.

    python3 tests/vpnswitch.py

No account, no network, no root: `vpn.down` and the reconnect are stubbed so
only the decision logic runs.
"""

from __future__ import annotations

import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="htb-vpnswitch-")
os.environ["HTB_CONFIG_DIR"] = _tmp + "/config"
os.environ["HTB_CACHE_DIR"] = _tmp + "/cache"
os.environ["HTB_STATE_DIR"] = _tmp + "/state"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from htb import api, vpn  # noqa: E402
from htb.commands import common  # noqa: E402

checks = []


def check(label, ok):
    checks.append(bool(ok))
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


class Args:
    def __init__(self, **kw):
        self.netns, self.global_vpn, self.no_vpn = "t", False, False
        self.__dict__.update(kw)


class FakeClient:
    def __init__(self, refuse=False):
        self.switched, self.refuse = [], refuse

    def vpn_switch(self, vpn_id):
        if self.refuse:
            raise api.ApiError("VIP only", status=403)
        self.switched.append(int(vpn_id))
        return {"message": "switched"}

    def machine_active(self):
        return {"id": 1, "vpn_server_id": 253}

    def season_machine_active(self):
        return {"id": 9, "vpn_server_id": 271}


def with_stubs(meta):
    """Point the helper at a fake tunnel; record teardown/reconnect calls."""
    calls = {"down": 0, "up": 0}
    vpn.read_meta = lambda ns: dict(meta)
    vpn.down = lambda ns, quiet=False: calls.__setitem__("down", calls["down"] + 1)
    common.ensure_vpn = lambda a, c, product="labs": (
        calls.__setitem__("up", calls["up"] + 1) or {"id": 253, "name": "new"})
    return calls


print("\nreading the machine's server")
client = FakeClient()
check("takes vpn_server_id from the active machine",
      common.machine_vpn_server(client, {"id": 1}, "active") == 253)
check("ignores a machine that is not the active one",
      common.machine_vpn_server(client, {"id": 2}, "active") is None)
check("reads season machines from the season endpoint",
      common.machine_vpn_server(client, {"id": 9}, "release") == 271)


class NoField(FakeClient):
    def machine_active(self):
        return {"id": 1}


check("no vpn_server_id in the payload is not an error",
      common.machine_vpn_server(NoField(), {"id": 1}, "active") is None)


class Broken(FakeClient):
    def machine_active(self):
        raise api.ApiError("boom", status=500)


check("an API error while looking it up is not fatal",
      common.machine_vpn_server(Broken(), {"id": 1}, "active") is None)

print("\ndeciding whether to move")
calls = with_stubs({"id": 253, "name": "EU Machines 3"})
client = FakeClient()
common.ensure_vpn_server(Args(), client, 253)
check("already on the right server: no switch", not client.switched)
check("already on the right server: no reconnect", calls == {"down": 0, "up": 0})

calls = with_stubs({"id": 271, "name": "EU Free 1"})
client = FakeClient()
common.ensure_vpn_server(Args(), client, 253)
check("wrong server: asks HTB to switch", client.switched == [253])
check("wrong server: tears the tunnel down once", calls["down"] == 1)
check("wrong server: brings it back up once", calls["up"] == 1)

calls = with_stubs({"id": 271, "name": "EU Free 1"})
client = FakeClient()
common.ensure_vpn_server(Args(no_vpn=True), client, 253)
check("--no-vpn leaves the tunnel alone",
      not client.switched and calls == {"down": 0, "up": 0})

calls = with_stubs({})
client = FakeClient()
common.ensure_vpn_server(Args(), client, 253)
check("a tunnel we did not start is left alone",
      not client.switched and calls == {"down": 0, "up": 0})

calls = with_stubs({"id": 271, "name": "EU Free 1"})
client = FakeClient()
common.ensure_vpn_server(Args(), client, None)
check("no server id: nothing happens",
      not client.switched and calls == {"down": 0, "up": 0})

calls = with_stubs({"id": 271, "name": "EU Free 1"})
client = FakeClient(refuse=True)
common.ensure_vpn_server(Args(), client, 253)
check("a refused switch warns instead of killing the tunnel",
      calls == {"down": 0, "up": 0})

import shutil  # noqa: E402

shutil.rmtree(_tmp, ignore_errors=True)
failed = checks.count(False)
print()
if failed:
    print(f"\033[1;41m {failed} of {len(checks)} checks failed \033[0m")
    sys.exit(1)
print(f"\033[1;42m all {len(checks)} vpn-switch checks passed \033[0m")
