"""The node-prep validation tasks, run for real by ansible-playbook against localhost.

Every case is one inventory host, so a single ansible run covers them all.
Only read-only tasks run: fact gathering, set_fact/fail/include_vars, and in
drop-in mode `containerd --version` (a fake on PATH) plus a slurp of a temp file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.unit.conftest import DEPLOY_DIR

ROLE = DEPLOY_DIR / "roles" / "k7_node_prep"
ANSIBLE_PLAYBOOK = Path(sys.executable).parent / "ansible-playbook"
DROP_IN_TASKS = (
    "Validate the containerd config mode",
    "Read the containerd version (drop-in mode)",
    "Fail if containerd is not 2.x (drop-in mode)",
    "Read the main containerd config (drop-in mode)",
    "Fail if the main containerd config does not import the drop-in directory",
)

pytestmark = pytest.mark.skipif(not ANSIBLE_PLAYBOOK.exists(), reason="ansible-core is not installed in this venv")


def _run(tmp_path: Path, hosts: dict[str, dict[str, Any]], tasks: list[dict], **kw: Any) -> dict[str, str]:
    """Run ``tasks`` once per host; return host → "ok" or its fail message."""
    common = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable}
    inventory = {"all": {"hosts": {h: {**common, **v} for h, v in hosts.items()}}}
    (tmp_path / "inventory.yml").write_text(yaml.safe_dump(inventory))
    play = [{"hosts": "all", "gather_facts": False, "tasks": tasks, **kw}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    env = {
        **os.environ,
        "ANSIBLE_ROLES_PATH": str(tmp_path / "roles"),
        "ANSIBLE_HOME": str(tmp_path / "ansible-home"),
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-tmp"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_FORCE_COLOR": "0",
        "ANSIBLE_FORKS": "32",
        "ANSIBLE_HOST_KEY_CHECKING": "0",
    }
    r = subprocess.run(
        [str(ANSIBLE_PLAYBOOK), "-i", str(tmp_path / "inventory.yml"), str(tmp_path / "play.yml")],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=300,
    )
    out: dict[str, str] = {}
    for m in re.finditer(
        r"^(?:fatal: \[([^\]]+)\]: FAILED!|failed: \[([^\]]+)\] \(item=.*?\)) => (\{.*\})$", r.stdout, re.M
    ):
        out[m.group(1) or m.group(2)] = json.loads(m.group(3))["msg"]
    recap = {
        h: (int(unreachable), int(failed))
        for h, unreachable, failed in re.findall(
            r"^(\S+)\s+: ok=\d+\s+changed=\d+\s+unreachable=(\d+)\s+failed=(\d+)", r.stdout, re.M
        )
    }
    assert set(recap) == set(hosts), r.stdout + r.stderr
    assert {h for h, (u, f) in recap.items() if u or f} == set(out), r.stdout
    for h in hosts:
        out.setdefault(h, "ok")
    return out


@pytest.fixture()
def role_copy(tmp_path: Path) -> Path:
    roles = tmp_path / "roles"
    shutil.copytree(ROLE, roles / "k7_node_prep")
    k3s = yaml.safe_load((ROLE / "vars" / "k3s.yml").read_text())
    del k3s["k7_kubectl"]
    (roles / "k7_node_prep" / "vars" / "broken.yml").write_text(yaml.safe_dump(k3s))
    return roles


FACT_HOSTS: dict[str, dict[str, Any]] = {
    "valid": {"k7_backends": "k7d"},
    "aliases": {"k7_backends": "fd, ql,k7", "k7_disk_source": "device:/dev/sdb"},
    "legacy-disk": {"k7_backends": "kfd", "k7_devmapper_disk": "/dev/nvme1n1"},
    "backends-unset": {},
    "none-mixed": {"k7_backends": "none,k7d"},
    "unknown-backend": {"k7_backends": "gvisor"},
    "bad-cni": {"k7_backends": "k7d", "k7_cni": "calico"},
    "distro-unset": {"k7_backends": "k7d", "k7_distro": None},
    "distro-unknown": {"k7_backends": "k7d", "k7_distro": "openshift"},
    "adapter-missing-key": {
        "k7_backends": "k7d",
        "k7_distro": "broken",
        "k7_adapter_distros": ["k3s", "broken"],
    },
    "disk-both": {"k7_backends": "k7d", "k7_disk_source": "loopback", "k7_disk": "/dev/sdb"},
    "disk-bad": {"k7_backends": "k7d", "k7_disk_source": "usb"},
    "loopback-kfd": {"k7_backends": "kfd,k7d", "k7_disk_source": "loopback"},
}

FACT_ERRORS = {
    "backends-unset": "k7_backends is not set on backends-unset",
    "none-mixed": "mixes 'none' with other backends",
    "unknown-backend": "Unknown backend(s) in k7_backends=['gvisor']",
    "bad-cni": "Unknown CNI 'calico'; allowed: cilium, flannel",
    "distro-unset": "k7_distro=<unset> is not supported",
    "distro-unknown": "k7_distro=openshift is not supported",
    "adapter-missing-key": "roles/k7_node_prep/vars/broken.yml does not define k7_kubectl",
    "disk-both": "are both set on disk-both. Keep only k7_disk_source=device:<path>",
    "disk-bad": "k7_disk_source=usb on disk-bad; allowed: spare-disk, device:<path>, loopback",
    "loopback-kfd": "k7_disk_source=loopback on loopback-kfd provisions no LVM thin pool",
}


def test_facts_validation(tmp_path: Path, role_copy: Path) -> None:
    hosts = {}
    for h, v in FACT_HOSTS.items():
        merged = {
            "k7_api_image": "local",
            "k7_api_remote_install": False,
            "k7_node_installer_image": "",
            "k7_cni_default": "cilium",
            "k7_distro": "k3s",
            **v,
        }
        hosts[h] = {k: val for k, val in merged.items() if val is not None}
    summary = tmp_path / "summary"
    summary.mkdir()
    tasks = [
        {"ansible.builtin.include_role": {"name": "k7_node_prep", "tasks_from": "facts.yml"}},
        {
            "ansible.builtin.copy": {
                "dest": f"{summary}/{{{{ inventory_hostname }}}}.json",
                "content": "{{ {'backends': k7_backends_list, 'disk': k7_disk_source_effective, "
                "'socket': k7_containerd_socket} | to_json }}",
                "mode": "0644",
            }
        },
    ]
    result = _run(tmp_path, hosts, tasks)
    assert {h: m for h, m in result.items() if m != "ok"}.keys() == FACT_ERRORS.keys(), result
    for h, want in FACT_ERRORS.items():
        assert want in result[h], (h, result[h])
    seen = {h: json.loads((summary / f"{h}.json").read_text()) for h in ("valid", "aliases", "legacy-disk")}
    assert seen["valid"] == {
        "backends": ["k7d"],
        "disk": "spare-disk",
        "socket": "/run/k3s/containerd/containerd.sock",
    }
    assert seen["aliases"]["backends"] == ["kata-firecracker-devmapper", "kata-qemu-longhorn", "k7d"]
    assert seen["aliases"]["disk"] == "device:/dev/sdb"
    assert seen["legacy-disk"]["disk"] == "device:/dev/nvme1n1"


def _fake_containerd(d: Path, version: str) -> Path:
    d.mkdir(parents=True)
    exe = d / "containerd"
    module = "github.com/containerd/containerd" + ("/v2" if version.startswith("v2.") else "")
    exe.write_text(f"#!/bin/sh\necho 'containerd {module} {version} abc123'\n")
    exe.chmod(0o755)
    return d


def test_drop_in_containerd_checks(tmp_path: Path, role_copy: Path) -> None:
    tasks = [t for t in yaml.safe_load((ROLE / "tasks" / "containerd.yml").read_text()) if t["name"] in DROP_IN_TASKS]
    assert [t["name"] for t in tasks] == list(DROP_IN_TASKS)
    (tmp_path / "drop-in.yml").write_text(yaml.safe_dump(tasks))

    def etc(name: str, main_config: str) -> str:
        conf_d = tmp_path / name / "containerd" / "conf.d"
        conf_d.mkdir(parents=True)
        (conf_d.parent / "config.toml").write_text(main_config)
        return str(conf_d / "k7.toml")

    v2 = _fake_containerd(tmp_path / "bin-v2", "v2.1.4")
    v1 = _fake_containerd(tmp_path / "bin-v1", "v1.7.27")
    hosts = {
        "ok": {"mode": "drop-in", "bin": v2, "conf": etc("ok", 'version = 3\n\nimports = ["{dir}/*.toml"]\n')},
        "containerd-1x": {"mode": "drop-in", "bin": v1, "conf": etc("c1", 'imports = ["{dir}/*.toml"]\n')},
        "no-imports": {"mode": "drop-in", "bin": v2, "conf": etc("noimp", "version = 3\n")},
        "other-imports": {"mode": "drop-in", "bin": v2, "conf": etc("other", 'imports = ["/etc/other/*.toml"]\n')},
        "template-mode": {"mode": "k3s-template", "bin": v1, "conf": etc("tmpl", "")},
        "bad-mode": {"mode": "patch-in-place", "bin": v2, "conf": etc("bad", "")},
    }
    for v in hosts.values():
        cfg = Path(v["conf"]).parent.parent / "config.toml"
        cfg.write_text(cfg.read_text().replace("{dir}", str(Path(v["conf"]).parent)))
    inventory = {
        h: {
            "k7_containerd_config_mode": v["mode"],
            "k7_containerd_config_path": v["conf"],
            "fake_path": f"{v['bin']}:{os.environ['PATH']}",
        }
        for h, v in hosts.items()
    }
    result = _run(
        tmp_path,
        inventory,
        [{"ansible.builtin.include_tasks": str(tmp_path / "drop-in.yml")}],
        environment={"PATH": "{{ fake_path }}"},
    )
    assert result["ok"] == "ok"
    assert result["template-mode"] == "ok"
    assert "containerd 1.x is not supported" in result["containerd-1x"]
    assert "v1.7.27 abc123 on containerd-1x" in result["containerd-1x"]
    for h in ("no-imports", "other-imports"):
        conf_d = str(Path(hosts[h]["conf"]).parent)
        assert f"has no `imports` line covering {conf_d}/*.toml" in result[h], result[h]
    assert "k7_containerd_config_mode=patch-in-place; allowed: k3s-template, drop-in" in result["bad-mode"]
