"""Node lease, node reset and hardware-e2e scripts, driven with fakes on PATH.

Nothing here reaches a network: ``ssh``, ``scp``, ``rsync``, ``curl`` and
``sleep`` are fakes that log their calls. In "exec" mode the fake ssh runs the
remote script locally with node paths rewritten into a temp dir; in "script"
mode it answers from canned rules. All addresses are RFC 5737 documentation IPs.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UTILS = REPO / "utils"
IP1, IP2 = "192.0.2.11", "192.0.2.12"

pytestmark = [
    pytest.mark.skipif(not (UTILS / "node-lease.sh").exists(), reason="lease/CI tooling is not part of this tree"),
    pytest.mark.skipif(
        not all(shutil.which(c) for c in ("bash", "jq", "flock", "python3")), reason="needs bash, jq, flock"
    ),
]

FAKE_SSH = r"""#!/usr/bin/env python3
import json, os, re, subprocess, sys
argv = sys.argv[1:]
i = 0
while i < len(argv) and argv[i].startswith("-"):
    i += 2 if argv[i] in ("-i", "-o", "-p", "-l", "-F") else 1
host, cmd = argv[i], " ".join(argv[i + 1:])
opts = argv[:i]
stdin = sys.stdin.read()
state = os.environ["FAKE_STATE"]
with open(os.path.join(state, "ssh.log"), "a") as f:
    f.write(json.dumps({"host": host, "cmd": cmd, "opts": opts, "stdin": stdin}) + "\n")
if os.environ.get("FAKE_SSH_MODE") == "exec":
    for src, dst in json.loads(os.environ.get("FAKE_REWRITE", "{}")).items():
        cmd, stdin = cmd.replace(src, dst), stdin.replace(src, dst)
    sys.exit(subprocess.run(["bash", "-c", cmd], input=stdin, text=True).returncode)
if "bash /root/run-reset.sh" in cmd:
    m = re.search(r"LEASE_DATA='([^']*)'", cmd)
    open(os.path.join(state, "written-lease"), "w").write(m.group(1))
rules = json.load(open(os.path.join(state, "ssh-rules.json"))) if os.path.exists(os.path.join(state, "ssh-rules.json")) else []
for pattern, out, rc in rules:
    if re.search(pattern, cmd):
        if out == "@lease":
            out = open(os.path.join(state, "written-lease")).read() + "\n"
        sys.stdout.write(out)
        sys.exit(rc)
"""

FAKE_CURL = r"""#!/usr/bin/env python3
import json, os, re, sys
state = os.environ["FAKE_STATE"]
args = sys.argv[1:]
with open(os.path.join(state, "curl.log"), "a") as f:
    f.write(json.dumps(args) + "\n")
url = next(a for a in args if a.startswith("https://"))
for pattern, out, rc in json.load(open(os.path.join(state, "curl-rules.json"))):
    if re.search(pattern, url):
        sys.stdout.write(out)
        sys.exit(rc)
sys.exit(22)
"""

LOGGER = '#!/usr/bin/env bash\necho "$*" >> "$FAKE_STATE/{name}.log"\n'
FAKE_SLEEP = "#!/usr/bin/env python3\nimport time\ntime.sleep(0.02)\n"

STUB_LEASE = """#!/usr/bin/env bash
echo "$1 $2 ttl=${K7_LEASE_TTL:-} owner=${K7_LEASE_OWNER:-}" >> "$FAKE_STATE/lease.log"
f="$FAKE_STATE/lease-rc-$1-$2"; [[ -f $f ]] && exit "$(cat "$f")"; exit 0
"""
STUB_RESET = """#!/usr/bin/env bash
echo "$1 $2 node=${K7_NODE_IP:-} host=${HOSTNAME_VALUE:-} two_disk=${TWO_DISK:-}" >> "$FAKE_STATE/reset.log"
f="$FAKE_STATE/reset-rc-$2"; [[ -f $f ]] && exit "$(cat "$f")"; exit 0
"""
STUB_ITEST = """#!/usr/bin/env bash
echo "node=${K7_NODE_IP:-} artifacts=${K7_ITEST_ARTIFACTS:-} kube=${K7_KUBECONFIG:-} ns=${K7_SYSTEM_NAMESPACE:-}\
 marker=${K7_ITEST_MARKER:-} run=${K7_ITEST_RUN:-} destructive=${K7_DESTRUCTIVE:-}\
 install=${K7_ITEST_INSTALL_ARGS:-}" >> "$FAKE_STATE/itest.log"
"""
STUB_DETACHED = """remote_detached() {
    printf '%s|%s|%s\\n' "$1" "$2" "$NODE" >> "$FAKE_STATE/detached.log"
    return "$(cat "$FAKE_STATE/detached-rc" 2>/dev/null || echo 0)"
}
"""


class Fakes:
    def __init__(self, tmp: Path):
        self.tmp, self.bin, self.state, self.home = tmp, tmp / "bin", tmp / "state", tmp / "home"
        for d in (self.bin, self.state, self.home / ".ssh"):
            d.mkdir(parents=True)
        (self.home / ".ssh/id_ed25519").write_text("fake private key\n")
        (self.home / ".ssh/id_ed25519.pub").write_text("ssh-ed25519 AAAAfake test@k7\n")
        self._write("ssh", FAKE_SSH)
        self._write("curl", FAKE_CURL)
        self._write("sleep", FAKE_SLEEP)
        for name in ("scp", "rsync"):
            self._write(name, LOGGER.format(name=name))

    def _write(self, name: str, text: str, where: Path | None = None) -> Path:
        p = (where or self.bin) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        p.chmod(0o755)
        return p

    def tree(self, *scripts: str, stubs: dict[str, str] | None = None) -> Path:
        """A copy of utils/ holding ``scripts`` (real), the key helper (real) and ``stubs``."""
        utils = self.tmp / "repo" / "utils"
        for rel in (*scripts, "ssh-key-helper.sh"):
            (utils / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(UTILS / rel, utils / rel)
        for rel, text in (stubs or {}).items():
            self._write(rel, text, utils)
        return utils

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.home),
            "FAKE_STATE": str(self.state),
            "LANG": "C.UTF-8",
        }
        env.update(extra)
        assert shutil.which("ssh", path=env["PATH"]) == str(self.bin / "ssh")
        assert shutil.which("curl", path=env["PATH"]) == str(self.bin / "curl")
        return env

    def run(self, argv: list[str], env: dict[str, str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", *argv], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=timeout
        )

    def rules(self, name: str, rules: list[tuple[str, str, int]]) -> None:
        (self.state / f"{name}-rules.json").write_text(json.dumps(rules))

    def log(self, name: str) -> list[str]:
        p = self.state / f"{name}.log"
        return p.read_text().splitlines() if p.exists() else []

    def ssh_calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log("ssh")]


@pytest.fixture()
def fakes(tmp_path: Path) -> Fakes:
    return Fakes(tmp_path)


# --- node-lease.sh -------------------------------------------------------------


class Node:
    """node-lease.sh with its remote half run locally against a fake node root."""

    def __init__(self, fakes: Fakes, os_id: str = "ubuntu"):
        self.fakes, self.root = fakes, fakes.tmp / "node"
        (self.root / "etc").mkdir(parents=True)
        (self.root / "etc/os-release").write_text(f'NAME="X"\nID={os_id}\n')
        self.script = fakes.tree("node-lease.sh") / "node-lease.sh"
        self.rewrite = {
            "/var/lib/k7-lease": str(self.root / "var/lib/k7-lease"),
            "/etc/os-release": str(self.root / "etc/os-release"),
        }
        self.lease = self.root / "var/lib/k7-lease/lease"

    def __call__(self, action: str, owner: str | None = "agent:a", **env: str) -> subprocess.CompletedProcess[str]:
        e = {"FAKE_SSH_MODE": "exec", "FAKE_REWRITE": json.dumps(self.rewrite), **env}
        if owner is not None:
            e["K7_LEASE_OWNER"] = owner
        return self.fakes.run([str(self.script), action, IP1], self.fakes.env(**e))

    def put(self, owner: str, expires_in: int) -> None:
        self.lease.parent.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        self.lease.write_text(f"owner={owner}\nexpires={now + expires_in}\nacquired={now}\n")

    def fields(self) -> dict[str, str]:
        return dict(line.split("=", 1) for line in self.lease.read_text().splitlines())


def test_lease_acquire_writes_owner_and_expiry(fakes: Fakes):
    node = Node(fakes)
    before = int(time.time())
    r = node("acquire", K7_LEASE_TTL="600")
    assert r.returncode == 0, r.stderr
    f = node.fields()
    assert f["owner"] == "agent:a"
    assert before + 600 <= int(f["expires"]) <= int(time.time()) + 600
    (call,) = fakes.ssh_calls()
    assert call["host"] == f"root@{IP1}"
    assert call["cmd"] == "bash -s -- acquire agent:a 600"
    assert "BatchMode=yes" in call["opts"] and "ConnectTimeout=10" in call["opts"]


def test_lease_acquire_respects_a_live_lease_and_takes_an_expired_one(fakes: Fakes):
    node = Node(fakes)
    node.put("ci:other#1", 3600)
    r = node("acquire")
    assert r.returncode == 3 and "held by ci:other#1" in r.stderr
    assert node.fields()["owner"] == "ci:other#1"
    node.put("ci:other#1", -1)
    assert node("acquire").returncode == 0
    assert node.fields()["owner"] == "agent:a"


def test_lease_reacquire_by_the_owner_extends_it(fakes: Fakes):
    node = Node(fakes)
    node.put("agent:a", 10)
    assert node("acquire", K7_LEASE_TTL="5000").returncode == 0
    assert int(node.fields()["expires"]) >= int(time.time()) + 4990


def test_lease_acquire_refuses_outside_ubuntu(fakes: Fakes):
    node = Node(fakes, os_id="debian")
    r = node("acquire")
    assert r.returncode == 3 and "not booted into Ubuntu" in r.stderr
    assert not node.lease.exists()


def test_lease_renew_only_by_the_owner(fakes: Fakes):
    node = Node(fakes)
    node.put("ci:other#1", 3600)
    r = node("renew")
    assert r.returncode == 3 and "not held by agent:a (owner: ci:other#1)" in r.stderr
    node.put("agent:a", 5)
    assert node("renew", K7_LEASE_TTL="900").returncode == 0
    assert int(node.fields()["expires"]) >= int(time.time()) + 890


def test_lease_release_rules(fakes: Fakes):
    node = Node(fakes)
    r = node("release")
    assert r.returncode == 0 and "lease already free" in r.stdout
    node.put("ci:other#1", 3600)
    r = node("release")
    assert r.returncode == 3 and node.lease.exists()
    node.put("ci:other#1", -5)
    r = node("release")
    assert r.returncode == 0 and "expired lease of ci:other#1 left as is" in r.stdout and node.lease.exists()
    node.put("agent:a", 3600)
    r = node("release")
    assert r.returncode == 0 and not node.lease.exists()


def test_lease_check_needs_an_unexpired_lease_of_ours(fakes: Fakes):
    node = Node(fakes)
    assert node("check").returncode == 3
    node.put("agent:a", 3600)
    r = node("check")
    assert r.returncode == 0 and "lease held by agent:a" in r.stdout
    node.put("agent:a", -1)
    assert node("check").returncode == 3
    node.put("ci:other#1", 3600)
    r = node("check")
    assert r.returncode == 3 and "owner: ci:other#1" in r.stderr


def test_lease_status_and_break_need_no_owner(fakes: Fakes):
    node = Node(fakes)
    assert node("status", owner=None).stdout.strip() == "free"
    node.put("ci:other#1", 3600)
    assert node("status", owner=None).stdout.startswith("held: owner=ci:other#1 expires=")
    node.put("ci:other#1", -1)
    assert node("status", owner=None).stdout.strip() == "expired: owner=ci:other#1"
    r = node("break", owner=None)
    assert r.returncode == 0 and "was: ci:other#1" in r.stdout and not node.lease.exists()
    assert fakes.ssh_calls()[0]["cmd"] == "bash -s -- status - 14400"


@pytest.mark.parametrize(
    ("env", "msg"),
    [
        ({"K7_LEASE_TTL": "4h"}, "K7_LEASE_TTL must be seconds"),
        ({"K7_LEASE_OWNER": "agent a"}, "must not contain spaces or '='"),
        ({"K7_LEASE_OWNER": "owner=x"}, "must not contain spaces or '='"),
    ],
)
def test_lease_bad_input_exits_2_before_ssh(fakes: Fakes, env: dict[str, str], msg: str):
    node = Node(fakes)
    r = node("acquire", **{"owner": "agent:a", **{k.replace("K7_LEASE_OWNER", "owner"): v for k, v in env.items()}})
    assert r.returncode == 2 and msg in r.stderr
    assert fakes.ssh_calls() == []


def test_lease_owner_required_except_for_status(fakes: Fakes):
    node = Node(fakes)
    r = node("acquire", owner=None)
    assert r.returncode != 0 and "set K7_LEASE_OWNER" in r.stderr
    assert fakes.ssh_calls() == []


def test_lease_unknown_action_is_2(fakes: Fakes):
    node = Node(fakes)
    r = node("steal")
    assert r.returncode == 2 and "unknown action: steal" in r.stderr


def test_lease_busy_lock_is_4(fakes: Fakes):
    node = Node(fakes)
    node.rewrite["flock -w 30"] = "flock -w 0"
    lock = node.root / "var/lib/k7-lease/.lock"
    lock.parent.mkdir(parents=True)
    with open(lock, "w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        r = node("acquire")
    assert r.returncode == 4 and "lease lock busy" in r.stderr


# --- reset_robot_node.sh ---------------------------------------------------------

RESCUE_BOOT, UBUNTU_BOOT = "11111111-rescue", "22222222-ubuntu"


def _reset_setup(fakes: Fakes, **ssh_overrides: tuple[str, int]) -> Path:
    utils = fakes.tree("reset_robot_node.sh", stubs={"node-lease.sh": STUB_LEASE, "wipe-disk.sh": "#!/bin/sh\n"})
    fakes.rules(
        "curl",
        [
            (r"/key$", json.dumps([{"key": {"fingerprint": "aa:bb"}}]), 0),
            (r"/boot/\d+/rescue$", json.dumps({"rescue": {"password": "SECRET-PW", "active": True}}), 0),
            (r"/reset/\d+$", json.dumps({"reset": {"type": "hw"}}), 0),
        ],
    )
    defaults = {
        "reboot": ("", 255),
        "sshd -T": ("", 0),
        "^hostname$": ("k7-node-07\n", 0),
        "cat /var/lib/k7-lease/lease": ("@lease", 0),
        'grep -q "\\^ID=ubuntu"': (UBUNTU_BOOT + "\n", 0),
        "^cat /proc/sys/kernel/random/boot_id$": (RESCUE_BOOT + "\n", 0),
    }
    defaults.update(ssh_overrides)
    fakes.rules("ssh", [(p, out, rc) for p, (out, rc) in defaults.items()])
    return utils / "reset_robot_node.sh"


def _reset(fakes: Fakes, script: Path, **env: str) -> subprocess.CompletedProcess[str]:
    base = {
        "ROBOT_USER": "u",
        "ROBOT_PASS": "p",
        "K7_LEASE_OWNER": "agent:a",
        "K7_LEASE_TTL": "7200",
        "HOSTNAME_VALUE": "k7-node-07",
    }
    return fakes.run([str(script), "424242", IP1], fakes.env(**{**base, **env}))


def test_reset_happy_path_writes_the_lease_into_the_new_os(fakes: Fakes):
    script = _reset_setup(fakes)
    before = int(time.time())
    r = _reset(fakes, script, TWO_DISK="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "==> sshd is key-only" in r.stdout
    assert "SECRET-PW" not in r.stdout
    assert fakes.log("lease") == [f"check {IP1} ttl=7200 owner=agent:a"]
    curls = [json.loads(line) for line in fakes.log("curl")]
    assert [next(a for a in c if a.startswith("https://")).rsplit("/", 2)[-2:] for c in curls] == [
        ["robot-ws.your-server.de", "key"],
        ["424242", "rescue"],
        ["reset", "424242"],
    ]
    assert "authorized_key[]=aa:bb" in curls[1] and "type=hw" in curls[2]
    run = next(c["cmd"] for c in fakes.ssh_calls() if "bash /root/run-reset.sh" in c["cmd"])
    assert "TWO_DISK='1'" in run and "HOSTNAME_VALUE='k7-node-07'" in run
    lease = dict(line.split("=", 1) for line in (fakes.state / "written-lease").read_text().splitlines())
    assert lease["owner"] == "agent:a"
    assert before + 7200 <= int(lease["expires"]) <= int(time.time()) + 7200
    assert all(c["host"] == f"root@{IP1}" for c in fakes.ssh_calls())
    assert fakes.log("scp") and fakes.log("scp")[0].endswith(f"root@{IP1}:/root/wipe-disk.sh")


@pytest.mark.parametrize(
    ("env", "msg"),
    [
        ({"K7_LEASE_OWNER": ""}, "set K7_LEASE_OWNER"),
        ({"K7_LEASE_OWNER": "agent a"}, "must have no spaces"),
        ({"K7_LEASE_OWNER": "agent'a"}, "must have no spaces"),
        ({"K7_LEASE_TTL": "2h"}, "K7_LEASE_TTL must be seconds"),
        ({"ROBOT_PASS": ""}, "set ROBOT_PASS"),
    ],
)
def test_reset_refuses_bad_input_before_touching_anything(fakes: Fakes, env: dict[str, str], msg: str):
    script = _reset_setup(fakes)
    r = _reset(fakes, script, **env)
    assert r.returncode != 0 and msg in r.stderr
    assert fakes.log("curl") == [] and fakes.ssh_calls() == [] and fakes.log("lease") == []


def test_reset_needs_the_callers_lease(fakes: Fakes):
    script = _reset_setup(fakes)
    (fakes.state / f"lease-rc-check-{IP1}").write_text("3")
    r = _reset(fakes, script)
    assert r.returncode == 3
    assert fakes.log("curl") == [] and fakes.ssh_calls() == []


def test_reset_of_an_unreachable_node_skips_the_lease_check(fakes: Fakes):
    script = _reset_setup(fakes)
    (fakes.state / f"lease-rc-check-{IP1}").write_text("3")
    r = _reset(fakes, script, K7_LEASE_NODE_UNREACHABLE="1")
    assert r.returncode == 0, r.stderr
    assert fakes.log("lease") == [] and "not verifying the lease" in r.stdout


def test_reset_without_a_robot_key_stops_before_rescue(fakes: Fakes):
    script = _reset_setup(fakes)
    fakes.rules("curl", [(r"/key$", "[]", 0)])
    r = _reset(fakes, script)
    assert r.returncode == 1 and "Could not get SSH key fingerprint" in r.stderr
    assert len(fakes.log("curl")) == 1 and fakes.ssh_calls() == []


@pytest.mark.parametrize(
    ("override", "msg"),
    [
        ({"reboot": ("", 1)}, "reboot command failed with exit 1"),
        ({'grep -q "\\^ID=ubuntu"': (RESCUE_BOOT + "\n", 0)}, "refusing to trust it"),
        ({"^hostname$": ("rescue\n", 0)}, "Fresh OS hostname is 'rescue', expected 'k7-node-07'"),
        ({"cat /var/lib/k7-lease/lease": ("owner=someone-else\n", 0)}, "does not match the one written"),
        ({"sshd -T": ("", 1)}, "still accepts passwords or keyboard-interactive"),
        ({"^cat /proc/sys/kernel/random/boot_id$": ("", 0)}, "Could not read rescue boot_id"),
    ],
)
def test_reset_refuses_to_hand_over_an_unverified_os(fakes: Fakes, override: dict, msg: str):
    script = _reset_setup(fakes, **override)
    r = _reset(fakes, script)
    assert r.returncode == 1 and msg in r.stderr, r.stdout + r.stderr


def test_reset_reboot_exit_0_is_accepted(fakes: Fakes):
    script = _reset_setup(fakes, reboot=("", 0))
    assert _reset(fakes, script).returncode == 0


# --- utils/ci/k7-e2e.sh -----------------------------------------------------------


def _e2e(fakes: Fakes, nodes: str, phases: str, robot: dict[str, str] | None = None, **env: str):
    utils = fakes.tree(
        "ci/k7-e2e.sh",
        stubs={
            "node-lease.sh": STUB_LEASE,
            "reset_robot_node.sh": STUB_RESET,
            "run-integration-tests.sh": STUB_ITEST,
            "remote-detached.sh": STUB_DETACHED,
            "ci/apt-get.sh": "",
        },
    )
    robot = robot if robot is not None else {"1": IP1, "2": IP2}
    fakes.rules("curl", [(f"/server/{s}$", json.dumps({"server": {"server_ip": ip}}), 0) for s, ip in robot.items()])
    fakes.rules("ssh", [("^hostname$", "k7-ci-host\n", 0)])
    base = {
        "K7_CI_NODES": nodes,
        "K7_CI_FORBIDDEN_SERVERS": "999",
        "K7_LEASE_OWNER": "ci:test#1",
        "K7_CI_PHASES": phases,
        "K7_CI_ARTIFACTS": str(fakes.tmp / "art"),
        "ROBOT_USER": "u",
        "ROBOT_PASS": "p",
    }
    return fakes.run([str(utils / "ci/k7-e2e.sh")], fakes.env(**{**base, **env}))


@pytest.mark.parametrize(
    ("nodes", "msg"),
    [
        (f"1:{IP1} 999:{IP2}", "server 999 is forbidden; refusing"),
        (f"1-{IP1}", f"bad K7_CI_NODES entry: 1-{IP1}"),
        (f"1:{IP1}:22", "bad K7_CI_NODES entry"),
    ],
)
def test_e2e_refuses_bad_or_forbidden_nodes_before_leasing(fakes: Fakes, nodes: str, msg: str):
    r = _e2e(fakes, nodes, "reset")
    assert r.returncode == 1 and msg in r.stderr
    assert fakes.log("lease") == [] and fakes.log("reset") == []


def test_e2e_robot_mismatch_refuses_and_releases_only_its_own_leases(fakes: Fakes):
    (fakes.state / f"lease-rc-check-{IP2}").write_text("3")
    r = _e2e(fakes, f"1:{IP1} 2:{IP2}", "reset", robot={"1": IP1, "2": "192.0.2.99"})
    assert r.returncode == 1 and f"Robot server 2 is 192.0.2.99, not {IP2}; refusing" in r.stderr
    assert fakes.log("reset") == []
    assert [line.split()[:2] for line in fakes.log("lease")] == [
        ["check", IP1],
        ["check", IP2],
        ["acquire", IP2],
        ["release", IP2],
    ]


def test_e2e_resets_every_node_in_parallel_with_numbered_hostnames(fakes: Fakes):
    for ip in (IP1, IP2):
        (fakes.state / f"lease-rc-check-{ip}").write_text("3")
    r = _e2e(fakes, f"1:{IP1} 2:{IP2}", "reset")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(fakes.log("reset")) == [
        f"1 {IP1} node={IP1} host=k7-ci-01 two_disk=1",
        f"2 {IP2} node={IP2} host=k7-ci-02 two_disk=1",
    ]
    lease = fakes.log("lease")
    assert f"acquire {IP1} ttl=21600 owner=ci:test#1" in lease
    assert [line.split()[:2] for line in lease if line.startswith("release")] == [["release", IP1], ["release", IP2]]
    assert (fakes.tmp / "art/cluster").is_dir()


def test_e2e_one_failed_reset_fails_the_run(fakes: Fakes):
    (fakes.state / f"reset-rc-{IP2}").write_text("1")
    r = _e2e(fakes, f"1:{IP1} 2:{IP2}", "reset")
    assert r.returncode == 1
    assert f"{IP1}: reset ok" in r.stdout and f"{IP2}: reset FAILED" in r.stderr
    assert not [line for line in fakes.log("lease") if line.startswith("release")]


def _itest_line(art: str, **env: str) -> str:
    keys = ("kube", "ns", "marker", "run", "destructive", "install")
    return f"node={IP1} artifacts={art} " + " ".join(f"{k}={env.get(k, '')}" for k in keys)


def test_e2e_test_phase_runs_the_suite_on_the_first_node(fakes: Fakes):
    r = _e2e(fakes, f"1:{IP1} 2:{IP2}", "test")
    assert r.returncode == 0, r.stderr
    assert fakes.log("itest") == [_itest_line(f"{fakes.tmp / 'art'}/itest")]
    assert fakes.log("reset") == [] and fakes.log("curl") == []


@pytest.mark.parametrize(
    ("env", "msg"),
    [
        ({"K7_CI_PHASES": "reset deploy"}, "unknown phase: deploy"),
        ({"K7_CI_DISTRO": "kubeadm"}, "K7_CI_DISTRO must be k3s or rke2, got kubeadm"),
    ],
)
def test_e2e_refuses_unknown_phases_and_distros_before_leasing(fakes: Fakes, env: dict[str, str], msg: str):
    r = _e2e(fakes, f"1:{IP1}", "reset", **env)
    assert r.returncode == 1 and msg in r.stderr
    assert fakes.log("lease") == []


def test_e2e_rke2_install_is_single_node_and_runs_the_node_script(fakes: Fakes):
    r = _e2e(fakes, f"1:{IP1} 2:{IP2}", "install", K7_CI_DISTRO="rke2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"K7_CI_DISTRO=rke2 is single-node: using 1:{IP1} only" in r.stdout
    assert {line.split()[1] for line in fakes.log("lease")} == {IP1}
    assert fakes.log("detached") == [f"rke2|cd /root/k7 && deploy/helm/k7/ci/rke2-node.sh all|root@{IP1}"]
    rke2_kubectl = "KUBECONFIG=/etc/rancher/rke2/rke2.yaml /var/lib/rancher/rke2/bin/kubectl"
    waits = [c["cmd"] for c in fakes.ssh_calls() if "condition=Ready node" in c["cmd"]]
    assert len(waits) == 1 and waits[0].startswith(f"{rke2_kubectl} wait ") and waits[0].endswith("-eq 1")
    assert (fakes.tmp / "art/inventory.ini").read_text().count("ansible_host=") == 1
    assert {c["host"] for c in fakes.ssh_calls()} == {f"root@{IP1}"}


def test_e2e_k3s_install_runs_k7_install_from_the_controller(fakes: Fakes):
    r = _e2e(fakes, f"1:{IP1} 2:{IP2}", "install", K7_CI_INSTALL_ARGS="--api-image local --backends kfd")
    assert r.returncode == 0, r.stdout + r.stderr
    (line,) = fakes.log("detached")
    name, cmd, node = line.split("|")
    assert (name, node) == ("install", f"root@{IP1}")
    assert cmd.endswith("src/k7/cli/dev.sh install -i /root/k7-ci/inventory.ini --api-image local --backends kfd")
    assert any(c["cmd"].startswith("k3s kubectl wait ") for c in fakes.ssh_calls())


def test_e2e_rke2_test_and_destructive_phases_point_the_suite_at_the_chart(fakes: Fakes):
    r = _e2e(fakes, f"1:{IP1}", "test destructive", K7_CI_DISTRO="rke2")
    assert r.returncode == 0, r.stdout + r.stderr
    chart = {"kube": "/etc/rancher/rke2/rke2.yaml", "ns": "k7-system"}
    art = fakes.tmp / "art"
    assert fakes.log("itest") == [
        _itest_line(f"{art}/itest", **chart),
        _itest_line(
            f"{art}/destructive",
            **chart,
            marker="destructive",
            run="destructive",
            destructive="1",
            install="-i /root/k7-ci/inventory.ini --api-mode development --api-image local",
        ),
    ]
    assert fakes.log("rsync") == []


def test_e2e_k3s_destructive_alone_prepares_the_controller_for_the_reinstall(fakes: Fakes):
    nodes = " ".join(f"{i}:192.0.2.{10 + i}" for i in (1, 2, 3))
    r = _e2e(fakes, nodes, "destructive", robot={})
    assert r.returncode == 0, r.stdout + r.stderr
    assert fakes.log("itest") == [
        _itest_line(
            f"{fakes.tmp / 'art'}/destructive",
            marker="destructive",
            run="destructive",
            destructive="1",
            install="-i /root/k7-ci/inventory.ini --ha --api-mode development --api-image local",
        )
    ]
    assert len(fakes.log("rsync")) == 1
    assert (fakes.tmp / "art/inventory.ini").read_text().count("ansible_host=") == 3


# --- run-integration-tests.sh -----------------------------------------------------


def _itest(fakes: Fakes, **env: str) -> subprocess.CompletedProcess[str]:
    utils = fakes.tree(
        "run-integration-tests.sh", stubs={"node-lease.sh": STUB_LEASE, "remote-detached.sh": STUB_DETACHED}
    )
    base = {"K7_NODE_IP": IP1, "K7_LEASE_OWNER": "agent:a", "K7_ITEST_ARTIFACTS": str(fakes.tmp / "art")}
    return fakes.run([str(utils / "run-integration-tests.sh")], fakes.env(**{**base, **env}))


def test_itest_requires_a_lease_owner(fakes: Fakes):
    r = _itest(fakes, K7_LEASE_OWNER="")
    assert r.returncode != 0 and "set K7_LEASE_OWNER" in r.stderr
    assert fakes.log("lease") == [] and fakes.log("rsync") == []


def test_itest_uses_a_held_lease_and_leaves_it(fakes: Fakes):
    r = _itest(fakes)
    assert r.returncode == 0, r.stderr
    assert [line.split()[:2] for line in fakes.log("lease")] == [["check", IP1]]


def test_itest_takes_a_free_lease_for_the_run_and_releases_it_even_on_failure(fakes: Fakes):
    (fakes.state / f"lease-rc-check-{IP1}").write_text("3")
    (fakes.state / "detached-rc").write_text("5")
    r = _itest(fakes)
    assert r.returncode == 5
    assert fakes.log("lease") == [
        f"check {IP1} ttl= owner=agent:a",
        f"acquire {IP1} ttl=10800 owner=agent:a",
        f"release {IP1} ttl= owner=agent:a",
    ]
    assert "finished with exit code 5" in r.stdout


def test_itest_a_lease_held_by_someone_else_stops_the_run(fakes: Fakes):
    for action in ("check", "acquire"):
        (fakes.state / f"lease-rc-{action}-{IP1}").write_text("3")
    r = _itest(fakes)
    assert r.returncode == 3
    assert fakes.log("rsync") == [] and fakes.log("detached") == []
    assert not [line for line in fakes.log("lease") if line.startswith("release")]


def test_itest_forwards_the_chart_cluster_env(fakes: Fakes):
    r = _itest(
        fakes,
        K7_KUBECONFIG="/root/.kube/rke2 config",
        K7_SYSTEM_NAMESPACE="k7-system",
        K7_PYTEST_ARGS="-k egress",
    )
    assert r.returncode == 0, r.stderr
    (line,) = fakes.log("detached")
    name, cmd, node = line.split("|")
    assert (name, node) == ("itest", f"root@{IP1}")
    assert cmd.startswith("export K7_KUBECONFIG=/root/.kube/rke2\\ config && export K7_SYSTEM_NAMESPACE=k7-system && ")
    assert "K7_API_CA" not in cmd
    assert cmd.endswith("--junitxml=/root/k7-runs/itest/junit.xml -k egress")
    (sync,) = fakes.log("rsync")
    assert sync.endswith(f"root@{IP1}:/root/k7/")
    assert [c["cmd"] for c in fakes.ssh_calls()] == ["bash -s"]
    assert "uv sync --locked" in fakes.ssh_calls()[0]["stdin"]


def test_itest_destructive_run_has_its_own_marker_run_dir_and_env(fakes: Fakes):
    r = _itest(
        fakes,
        K7_ITEST_MARKER="destructive",
        K7_ITEST_RUN="destructive",
        K7_DESTRUCTIVE="1",
        K7_ITEST_INSTALL_ARGS="-i /root/k7-ci/inventory.ini --ha",
    )
    assert r.returncode == 0, r.stderr
    (line,) = fakes.log("detached")
    name, cmd, _ = line.split("|")
    assert name == "destructive"
    assert cmd.startswith(
        "export K7_DESTRUCTIVE=1 && export K7_ITEST_INSTALL_ARGS=-i\\ /root/k7-ci/inventory.ini\\ --ha && "
    )
    assert "pytest -m destructive -v --junitxml=/root/k7-runs/destructive/junit.xml" in cmd
    assert [s.split()[-2] for s in fakes.log("scp")] == [
        f"root@{IP1}:/root/k7-runs/destructive/log",
        f"root@{IP1}:/root/k7-runs/destructive/junit.xml",
    ]


def test_itest_default_marker_is_integration(fakes: Fakes):
    assert _itest(fakes).returncode == 0
    (line,) = fakes.log("detached")
    assert "pytest -m integration -v --junitxml=/root/k7-runs/itest/junit.xml" in line


def test_itest_bad_run_name_exits_2_before_the_lease(fakes: Fakes):
    r = _itest(fakes, K7_ITEST_RUN="../itest")
    assert r.returncode == 2 and "K7_ITEST_RUN must match" in r.stderr
    assert fakes.log("lease") == [] and fakes.log("rsync") == []


# --- remote-detached.sh -----------------------------------------------------------


def _detached(fakes: Fakes, name: str, cmd: str) -> subprocess.CompletedProcess[str]:
    root = fakes.tmp / "node"
    root.mkdir(exist_ok=True)
    env = fakes.env(FAKE_SSH_MODE="exec", FAKE_REWRITE=json.dumps({"/root/k7-runs": str(root / "k7-runs")}))
    driver = (
        f"source {UTILS / 'remote-detached.sh'}\n"
        f"SSH_OPTS=(-o BatchMode=yes) NODE=root@{IP1}\n"
        f"remote_detached {name} {json.dumps(cmd)}\n"
    )
    return fakes.run(["-c", driver], env, timeout=60)


def test_detached_streams_the_log_and_returns_the_exit_status(fakes: Fakes):
    r = _detached(fakes, "job", "echo first; echo second; exit 7")
    assert r.returncode == 7, r.stderr
    assert r.stdout.count("first") == 1 and r.stdout.count("second") == 1
    assert "==> [job] exited 7" in r.stdout
    run_dir = fakes.tmp / "node/k7-runs/job"
    assert (run_dir / "rc").read_text().strip() == "7"
    assert (run_dir / "cmd").read_text().strip() == "echo first; echo second; exit 7"


def test_detached_refuses_a_second_run_with_the_same_name(fakes: Fakes):
    lock = fakes.tmp / "node/k7-runs/job.lock"
    lock.parent.mkdir(parents=True)
    with open(lock, "w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        r = _detached(fakes, "job", "echo should-not-run")
    assert r.returncode == 1 and "is already running" in r.stderr
    assert not (fakes.tmp / "node/k7-runs/job/cmd").exists()


# --- ci/apt-get.sh -----------------------------------------------------------------


def _apt(fakes: Fakes, wait: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    locks = fakes.tmp / "locks"
    locks.mkdir(exist_ok=True)
    text = (UTILS / "ci/apt-get.sh").read_text()
    for p in ("/var/lib/apt/lists/lock", "/var/cache/apt/archives/lock", "/var/lib/dpkg/lock-frontend"):
        assert p in text
        text = text.replace(p, str(locks / Path(p).parent.name))
    script = fakes.tmp / "apt-get.sh"
    script.write_text(text)
    fakes._write("apt-get", '#!/usr/bin/env bash\necho "$DEBIAN_FRONTEND $*" >> "$FAKE_STATE/apt.log"\n')
    r = fakes.run([str(script), "install", "-y", "rsync"], fakes.env(K7_APT_LOCK_WAIT=wait))
    return r, locks


def test_apt_wrapper_runs_apt_get_with_the_lock_timeout(fakes: Fakes):
    r, _ = _apt(fakes, "120")
    assert r.returncode == 0, r.stderr
    assert fakes.log("apt") == ["noninteractive -o DPkg::Lock::Timeout=120 install -y rsync"]


def test_apt_wrapper_gives_up_on_a_held_lock(fakes: Fakes):
    locks = fakes.tmp / "locks"
    locks.mkdir()
    held = locks / "lists"
    held.write_text("")
    fd = os.open(held, os.O_RDWR)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX)
        r, _ = _apt(fakes, "0")
    finally:
        os.close(fd)
    assert r.returncode != 0 and re.search(r"lists still locked after 0s", r.stderr), r.stderr
    assert fakes.log("apt") == []
