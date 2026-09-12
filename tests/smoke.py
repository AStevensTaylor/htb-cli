"""Smoke test: run every read/write command path against a fake HTB API.

    python3 tests/smoke.py

Touches no real account and no network - it only proves the command plumbing,
rendering and exit codes behave.
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="htb-smoke-")
os.environ["HTB_CONFIG_DIR"] = _tmp + "/config"
os.environ["HTB_CACHE_DIR"] = _tmp + "/cache"
os.environ["HTB_STATE_DIR"] = _tmp + "/state"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from htb import api, cli, hosts
from htb.commands import common

MACHINES = [
    {"id": 1, "name": "Lame", "os": "Linux", "difficultyText": "Easy", "points": 20,
     "star": 4.3, "release": "2017-03-14T19:00:00.000000Z", "retired": True,
     "active": False, "free": True, "user_owns_count": 9000, "root_owns_count": 8000,
     "authUserInUserOwns": True, "authUserInRootOwns": True, "isTodo": False,
     "maker": {"name": "ch4p"}, "sp_flag": 0, "ip": None},
    {"id": 2, "name": "Blurry", "os": "Linux", "difficultyText": "Medium", "points": 30,
     "star": 4.6, "release": "2024-05-25T19:00:00.000000Z", "retired": False,
     "active": True, "free": False, "user_owns_count": 500, "root_owns_count": 400,
     "authUserInUserOwns": False, "authUserInRootOwns": False, "isTodo": True,
     "maker": {"name": "0xyg3n"}, "sp_flag": 0, "ip": None},
    {"id": 3, "name": "Windows Box", "os": "Windows", "difficultyText": "Insane",
     "points": 50, "star": 4.0, "release": "2025-01-04T19:00:00.000000Z",
     "retired": False, "active": True, "free": False, "user_owns_count": 12,
     "root_owns_count": 3, "authUserInUserOwns": False, "authUserInRootOwns": False,
     "isTodo": False, "maker": {"name": "someone"}, "sp_flag": 0, "ip": None},
]

class FakeClient(api.Client):
    def __init__(self): self.token, self.debug, self.spawned = "x", False, None
    def user_info(self): return {"id": 42, "name": "tester", "canAccessVIP": True,
                                 "isDedicatedVip": True, "rank": "Hacker", "points": 123}
    def subscription(self): return "vip+"
    def machines_page(self, page=1, per_page=100, retired=False):
        data = [m for m in MACHINES if bool(m["retired"]) == retired]
        return {"data": data, "meta": {"last_page": 1}}
    def machines_unreleased(self): return []
    def machine_profile(self, slug):
        for m in MACHINES:
            if str(slug).lower() in (str(m["id"]), m["name"].lower()):
                out = dict(m)
                if self.spawned == m["id"]: out["ip"] = "10.10.11.24"
                return out
        raise api.ApiError("not found", status=404)
    def machine_active(self):
        if self.spawned:
            m = self.machine_profile(self.spawned)
            return {"id": m["id"], "name": m["name"], "ip": "10.10.11.24", "type": "Machine",
                    "expires_at": "2026-09-12T02:00:00.000000Z"}
        return None
    def machine_tags(self, mid): return {"info": [{"name": "web"}, {"name": "cve"}]}
    def season_machine_active(self): return None
    def season_list(self): return [{"id": 7, "name": "Season VII", "active": True,
                                    "end_date": "2026-11-01T00:00:00.000000Z"}]
    def season_rank(self, sid): return {"rank": 300, "total_points": 120, "current_tier": "Silver"}
    def spawn(self, mid):
        self.spawned = int(mid); return {"message": "Machine deployed to lab."}
    def terminate(self, mid):
        self.spawned = None; return {"message": "Machine terminated."}
    def own_machine(self, mid, flag, difficulty=None):
        if flag != "a" * 32: raise api.ApiError("Incorrect flag", status=400)
        return {"message": "Congratulations, you successfully owned root!"}
    def connection_status(self): return []
    def challenges(self, state="active"):
        return [{"id": 5, "name": "Baby RE", "category_name": "Reversing",
                 "difficulty": "Easy", "static_points": 20, "authUserSolve": False}]
    def vpn_servers(self, product="labs"):
        return {"data": {"assigned": {"id": 271, "friendly_name": "EU Free 1", "location": "EU"},
                         "options": {"EU": {"Free": {"servers": {
                             "271": {"id": 271, "friendly_name": "EU Free 1",
                                     "location": "EU", "current_clients": 42}}}}}}}

fake = FakeClient()
common.client = lambda args: fake
hosts.update = lambda *a, **k: True
hosts.clear = lambda *a, **k: True

def run(*argv):
    print("\n\033[1;44m $ htb " + " ".join(argv) + " \033[0m")
    try:
        rc = cli.main(list(argv))
    except SystemExit as exc:
        rc = exc.code or 0
    if rc: print(f"[exit {rc}]")
    return rc

run("search")
run("search", "blur")
run("search", "--os", "windows", "--state", "active")
run("search", "--state", "all", "--sort", "name", "--json")
run("info", "Lame")
run("start", "Blurry", "--no-vpn", "--no-hosts")
run("active")
run("ip")
run("status")
run("submit", "a" * 32, "-m", "Blurry")
rc = run("submit", "b" * 32, "-m", "Blurry")
assert rc == 2, f"expected exit 2 for a wrong flag, got {rc}"
run("stop")
run("season")
run("challenge", "list")
run("vpn", "servers")
run("proxy", "status", "--netns", "smoke-no-such-ns")
run("config", "list")
import shutil
shutil.rmtree(_tmp, ignore_errors=True)
print("\n\033[1;42m all command paths ran \033[0m")
