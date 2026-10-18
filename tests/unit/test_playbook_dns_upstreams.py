"""k7_dns_upstreams: the k7_k3s dns_validate.yml + dns.yml tasks, run for real
on localhost with the resolver and k3s env files redirected to tmp_path."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROLES = Path(__file__).resolve().parents[2] / "src/k7/deploy/roles"
ANSIBLE_PLAYBOOK = Path(sys.executable).parent / "ansible-playbook"
HOST_RESOLV = (
    "/run/systemd/resolve/resolv.conf" if Path("/run/systemd/resolve/resolv.conf").exists() else "/etc/resolv.conf"
)
PLAY = """
- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - ansible.builtin.import_role: {name: k7_k3s, tasks_from: dns_validate}
    - ansible.builtin.import_role: {name: k7_k3s, tasks_from: dns}
    - ansible.builtin.copy:
        dest: "{{ out }}"
        content: "{{ {'changed': k7_dns_changed | bool, 'resolv': k3s_resolv_conf} | to_json }}"
"""


def _run(tmp_path: Path, upstreams=None) -> tuple[subprocess.CompletedProcess, dict]:
    (tmp_path / "play.yml").write_text(PLAY)
    out = tmp_path / "out.json"
    out.unlink(missing_ok=True)
    extra = {
        "k7_node_role": "server",
        "k7_dns_resolv_path": str(tmp_path / "k7/resolv.conf"),
        "k7_k3s_env_file": str(tmp_path / "k3s.service.env"),
        "out": str(out),
    }
    if upstreams is not None:
        extra["k7_dns_upstreams"] = upstreams
    env = {**os.environ, "ANSIBLE_ROLES_PATH": str(ROLES), "ANSIBLE_NOCOLOR": "1", "ANSIBLE_LOCALHOST_WARNING": "0"}
    proc = subprocess.run(
        [str(ANSIBLE_PLAYBOOK), "-i", "localhost,", "-e", json.dumps(extra), str(tmp_path / "play.yml")],
        capture_output=True,
        text=True,
        env=env,
    )
    return proc, (json.loads(out.read_text()) if out.exists() else {})


def _ok(tmp_path: Path, upstreams=None) -> dict:
    proc, result = _run(tmp_path, upstreams)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return result


def test_fresh_install_writes_resolvers_and_is_idempotent(tmp_path: Path):
    first = _ok(tmp_path, "1.1.1.1, 9.9.9.9,8.8.8.8")
    resolv = tmp_path / "k7/resolv.conf"
    assert first == {"changed": True, "resolv": str(resolv)}
    lines = resolv.read_text().splitlines()
    assert lines[1:] == ["nameserver 1.1.1.1", "nameserver 9.9.9.9", "nameserver 8.8.8.8"]
    assert not (tmp_path / "k3s.service.env").exists(), "install.sh writes the env file on a fresh node"
    assert _ok(tmp_path, "1.1.1.1,9.9.9.9,8.8.8.8") == {"changed": False, "resolv": str(resolv)}


def test_yaml_list_matches_comma_string(tmp_path: Path):
    _ok(tmp_path, ["1.1.1.1", "9.9.9.9"])
    assert _ok(tmp_path, "1.1.1.1,9.9.9.9")["changed"] is False


def test_installed_node_converges_and_reverts(tmp_path: Path):
    env_file = tmp_path / "k3s.service.env"
    installer = f"K3S_TOKEN='t'\nK3S_RESOLV_CONF='{HOST_RESOLV}'\n"
    env_file.write_text(installer)
    env_file.chmod(0o600)
    assert _ok(tmp_path) == {"changed": False, "resolv": HOST_RESOLV}, "unset must leave today's install alone"
    assert env_file.read_text() == installer

    resolv = tmp_path / "k7/resolv.conf"
    assert _ok(tmp_path, "1.1.1.1")["changed"] is True
    assert env_file.read_text() == f"K3S_TOKEN='t'\nK3S_RESOLV_CONF='{resolv}'\n"
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert _ok(tmp_path, "1.1.1.1")["changed"] is False

    assert _ok(tmp_path, "9.9.9.9")["changed"] is True, "new content alone must restart CoreDNS"

    assert _ok(tmp_path, "") == {"changed": True, "resolv": HOST_RESOLV}
    assert env_file.read_text() == installer
    assert not resolv.exists()
    assert _ok(tmp_path)["changed"] is False


@pytest.mark.parametrize(
    ("upstreams", "message"),
    [
        ("1.1.1.256", "'1.1.1.256' is not a usable IPv4 resolver"),
        ("dns.example.com", "'dns.example.com' is not a usable IPv4 resolver"),
        ("2a01:4ff:ff00::add:1", "IPv6 is refused"),
        ("127.0.0.53", "'127.0.0.53' is not a usable IPv4 resolver"),
        ("1.1.1.1,9.9.9.9,8.8.8.8,8.8.4.4", "has 4 entries"),
    ],
)
def test_invalid_upstreams_fail_loudly(tmp_path: Path, upstreams: str, message: str):
    proc, _ = _run(tmp_path, upstreams)
    assert proc.returncode != 0
    assert message in proc.stdout, proc.stdout
    assert not (tmp_path / "k7").exists(), "validation must fail before any file is written"
