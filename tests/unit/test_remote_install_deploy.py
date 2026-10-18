"""Deploy side of POST /api/v1/install: `k7 install --api-remote-install` flags, the
playbook tasks that deploy the k7-node-installer stack, and the k7-api's access to it."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jinja2
import pytest
import yaml
from typer.testing import CliRunner

from k7.api import remote_install
from k7.cli.k7 import app
from k7.deploy.node_installer import entrypoint
from tests.unit.conftest import DEPLOY_DIR, FAKE_INSTALLER_IMAGE

ROLE_TASKS = DEPLOY_DIR / "roles/k7_cluster/tasks"
runner = CliRunner()


def _install(args: list[str]) -> tuple[object, dict]:
    seen: dict = {}

    def _capture(*_a, **kw):
        seen.update(kw)
        return SimpleNamespace(success=True, message="ok", error="")

    with (
        patch("k7.cli.k7.K7Core") as core_cls,
        patch("k7.cli.k7._build_default_inventory", return_value="[k7_servers]\nhost ansible_host=1.2.3.4"),
    ):
        core_cls.return_value.install_node = _capture
        result = runner.invoke(app, ["install", "--backend", "k7d", "--api-mode", "development", *args, "host"])
    return result, seen.get("extra_vars", {})


def _tasks(name: str) -> list[dict]:
    return yaml.safe_load((ROLE_TASKS / name).read_text())


# --- CLI ---


def test_remote_install_is_off_by_default():
    result, ev = _install([])
    assert result.exit_code == 0, result.output
    assert ev["k7_api_remote_install"] == "false" and ev["k7_node_installer_image"] == ""
    assert ev["k7_node_installer_allow_server_restart"] == "false"


def test_remote_install_pins_the_locked_installer_image():
    result, ev = _install(["--api-remote-install"])
    assert result.exit_code == 0, result.output
    assert ev["k7_api_remote_install"] == "true" and ev["k7_node_installer_image"] == FAKE_INSTALLER_IMAGE
    assert "k7_repo_root" not in ev
    assert f"k7-node-installer image: {FAKE_INSTALLER_IMAGE}" in result.output


def test_local_installer_image_builds_from_the_cwd_and_warns_unsigned():
    result, ev = _install(
        ["--api-remote-install", "--node-installer-image", "local", "--node-installer-allow-server-restart"]
    )
    assert result.exit_code == 0, result.output
    assert ev["k7_node_installer_image"] == "local" and ev["k7_repo_root"]
    assert ev["k7_node_installer_allow_server_restart"] == "true"
    assert "k7-node-installer:local" in result.output and "unsigned" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["--node-installer-image", "local"],
        ["--node-installer-allow-server-restart"],
        ["--api-remote-install", "--no-api"],
        ["--api-remote-install", "--node-installer-image", "ghcr.io/katakate/k7-node-installer:0.7.0"],
    ],
)
def test_bad_remote_install_flags_fail_before_ansible(args):
    result, ev = _install(args)
    assert result.exit_code != 0 and ev == {}


def test_production_mode_with_remote_install_fails_before_ansible():
    result, ev = _install(["--api-mode", "production", "--metering-sample-interval", "15", "--api-remote-install"])
    assert result.exit_code == 2 and ev == {}
    assert "--api-mode production refuses --api-remote-install" in " ".join(result.output.split())


def test_remote_install_without_a_locked_installer_image_fails_before_ansible(monkeypatch):
    monkeypatch.setattr("k7.deploy.images.load_lock", lambda: {"k7-api": "ghcr.io/katakate/k7-api@sha256:" + "a" * 64})
    result, ev = _install(["--api-remote-install"])
    assert result.exit_code == 1 and "no 'k7-node-installer' entry" in result.output and ev == {}


# --- playbook ---


def test_play_defaults_keep_remote_install_off():
    v = yaml.safe_load((DEPLOY_DIR / "k7-install-node.yaml").read_text())[0]["vars"]
    assert v["k7_api_remote_install"] is False and v["k7_node_installer_image"] == ""
    assert v["k7_node_installer_allow_server_restart"] is False


OFF_GATES = ("not k7_api_remote_install | bool", "k7_installer_remove | default(false) | bool")


def test_every_installer_stack_task_is_gated_on_remote_install_on_or_off():
    tasks = _tasks("node_installer.yml")
    on = [t for t in tasks if t["when"] in ("k7_api_remote_install | bool", "k7_node_installer_local | bool")]
    off = [t for t in tasks if t["when"] in OFF_GATES]
    copy = [t for t in tasks if t["when"] == f"k7_api_remote_install | bool or {OFF_GATES[1]}"]
    assert len(on) >= 6 and len(off) == 5 and len(copy) == 1
    assert len(on) + len(off) + len(copy) == len(tasks)
    assert all(t.get("run_once") for t in off + copy)
    # resources.yml imports it unconditionally, so `--no-api` re-runs converge too.
    (imp,) = [t for t in _tasks("resources.yml") if t.get("ansible.builtin.import_tasks") == "node_installer.yml"]
    assert "when" not in imp


def _decide(kubectl_stdout: str) -> bool:
    (t,) = [t for t in _tasks("node_installer.yml") if t["name"].startswith("Decide whether to remove")]
    env = jinja2.Environment()
    env.filters["from_json"] = json.loads
    env.tests["contains"] = lambda seq, v: v in seq
    out = env.from_string(t["ansible.builtin.set_fact"]["k7_installer_remove"]).render(
        k7_installer_live={"stdout": kubectl_stdout}
    )
    return {"True": True, "False": False}[out.strip()]


def _obj(kind: str, annotations: dict | None = None) -> dict:
    return {"kind": kind, "metadata": {"name": "x", **({"annotations": annotations} if annotations else {})}}


@pytest.mark.parametrize(
    ("live", "remove"),
    [
        ("", False),
        (json.dumps({"kind": "List", "items": []}), False),
        (json.dumps(_obj("ClusterRole", {"kubectl.kubernetes.io/last-applied-configuration": "{}"})), True),
        (json.dumps({"items": [_obj("ClusterRole"), _obj("Namespace")]}), True),
        (json.dumps({"items": [_obj("Namespace")]}), True),
        (json.dumps({"items": [_obj("ClusterRole", {"meta.helm.sh/release-name": "k7"}), _obj("Namespace")]}), False),
    ],
)
def test_removal_decision_skips_absent_and_helm_owned_stacks(live, remove):
    assert _decide(live) is remove


def test_removal_check_looks_at_the_cluster_role_and_namespace():
    (t,) = [t for t in _tasks("node_installer.yml") if t["name"].startswith("Check whether a non-Helm")]
    cmd = t["ansible.builtin.command"]
    assert "get clusterrole/k7-node-installer namespace/k7-system --ignore-not-found -o json" in cmd
    assert t["changed_when"] is False


def test_removal_unlabels_then_deletes_everything_the_apply_created_then_the_files():
    tasks = _tasks("node_installer.yml")
    names = [t["name"] for t in tasks]
    unlabel, delete, rm = (tasks[i] for i in range(len(tasks)) if tasks[i]["when"] == OFF_GATES[1])
    assert names.index(unlabel["name"]) < names.index(delete["name"]) < names.index(rm["name"])
    sh = unlabel["ansible.builtin.shell"]
    assert f"for key in {remote_install.POOL_LABEL} {remote_install.ADDED_BY_LABEL}; do" in sh
    assert 'label $nodes "$key-"' in sh and "set -euo pipefail" in sh
    cmd = delete["ansible.builtin.command"]
    (apply,) = [t for t in tasks if t["name"].startswith("Apply the k7-node-installer stack")]
    paths = re.findall(r"-f (\S+)", cmd)
    assert sorted(paths) == sorted(apply["loop"])
    assert paths[-1] == "/etc/k7/manifests/k7-node-installer/"  # its rbac.yaml (the ClusterRole) goes last
    assert "--ignore-not-found" in cmd and "--recursive=false" in cmd and "--timeout=" in cmd
    assert rm["ansible.builtin.file"] == {"path": "/etc/k7/manifests/k7-node-installer", "state": "absent"}


def test_installer_rbac_file_sorts_last_so_a_failed_removal_retries():
    files = sorted(p.name for p in (DEPLOY_DIR / "manifests/k7-node-installer").glob("*.yaml"))
    assert files[-1] == "rbac.yaml"
    kinds = [
        d["kind"]
        for d in yaml.safe_load_all((DEPLOY_DIR / "manifests/k7-node-installer/00-namespace.yaml").read_text())
    ]
    assert kinds == ["Namespace"]


def test_validate_requires_the_api_and_a_pinned_installer_image():
    (t,) = [t for t in _tasks("validate.yml") if t["name"].startswith("Require the k7-api")]
    assert t["when"] == "k7_api_remote_install | bool"
    that = t["ansible.builtin.assert"]["that"]
    assert any("k7_api_enabled" in c for c in that)
    assert any("k7_node_installer_image == 'local'" in c and "@sha256:[0-9a-f]{64}$" in c for c in that)


@pytest.mark.parametrize(
    ("mode", "remote", "fails"),
    [("production", True, True), ("production", False, False), ("development", True, False), ("", True, False)],
)
def test_validate_refuses_production_mode_with_remote_install(mode, remote, fails):
    (t,) = [t for t in _tasks("validate.yml") if t["name"] == "Fail if production mode is combined with remote install"]
    env = jinja2.Environment()
    env.filters["bool"] = bool
    facts = {"k7_api_enabled": True, "k7_api_mode": mode, "k7_api_remote_install": remote}
    assert all(env.compile_expression(c)(**facts) for c in t["when"]) is fails
    msg = " ".join(t["ansible.builtin.fail"]["msg"].split())
    assert "k7_api_mode=production refuses k7_api_remote_install=true" in msg
    assert t["tags"] == ["always"]
    # Before anything touches the host: the play imports validate.yml first, ahead of node prep.
    play = yaml.safe_load((DEPLOY_DIR / "k7-install-node.yaml").read_text())[0]["tasks"]
    names = [x.get("ansible.builtin.import_role", {}).get("tasks_from") for x in play]
    assert names.index("validate") < names.index("host")


def test_installer_config_uses_only_keys_and_backends_the_installer_accepts():
    (t,) = [
        t for t in _tasks("node_installer.yml") if t["name"] == "Compute the installer config from the first master"
    ]
    cfg = t["ansible.builtin.set_fact"]["k7_node_installer_config"]
    assert set(cfg) <= entrypoint.CONFIG_KEYS and {"distro", "backends", "diskSource"} <= set(cfg)
    assert "wipeDisks" not in cfg
    assert t["vars"]["k7_installer_backend_names"] == {v: k for k, v in entrypoint.BACKENDS.items()}
    assert "k7_node_installer_allow_server_restart" in cfg["allowServerRestart"]


def test_stack_apply_skips_the_cleanup_daemonset_and_includes_api_access():
    (t,) = [t for t in _tasks("node_installer.yml") if t["name"].startswith("Apply the k7-node-installer stack")]
    assert "--recursive=false" in t["ansible.builtin.command"]
    assert t["loop"] == ["/etc/k7/manifests/k7-node-installer/", "/etc/k7/manifests/k7-node-installer/k7-api-access/"]


def test_api_env_is_rendered_from_the_same_deploy_time_values():
    (t,) = [t for t in _tasks("api.yml") if t["name"] == "Render the k7-api image into the manifests"]
    subs = t["vars"]["k7_api_image_subs"]
    assert "k7_api_remote_install" in subs["__K7_API_REMOTE_INSTALL__"]
    assert "k7_node_installer_image_ref" in subs["__K7_NODE_INSTALLER_IMAGE__"]
    assert "'k7-system'" in subs["__K7_NODE_INSTALLER_NAMESPACE__"]
    (render,) = [t for t in _tasks("node_installer.yml") if t["name"] == "Render the pinned installer image"]
    assert render["ansible.builtin.replace"]["replace"] == "{{ k7_node_installer_image_ref }}"


def test_deployment_env_names_match_the_api_module():
    dep = yaml.safe_load((DEPLOY_DIR / "manifests/k7-api/deployment.yaml").read_text())
    env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env[remote_install.ENABLE_ENV] == "__K7_API_REMOTE_INSTALL__"
    assert env[remote_install.IMAGE_ENV] == "__K7_NODE_INSTALLER_IMAGE__"
    assert env[remote_install.NAMESPACE_ENV] == "__K7_NODE_INSTALLER_NAMESPACE__"
    assert "K7_API_IMAGE" not in env


def test_installer_manifests_match_the_api_module():
    ds = yaml.safe_load((DEPLOY_DIR / "manifests/k7-node-installer/daemonset.yaml").read_text())
    spec = ds["spec"]["template"]["spec"]
    assert ds["metadata"]["name"] == remote_install.DAEMONSET
    assert ds["metadata"]["namespace"] == "k7-system"
    assert [c["name"] for c in spec["containers"]] == [remote_install.CONTAINER]
    assert spec["nodeSelector"] == {remote_install.POOL_LABEL: "true"}
    k, v = remote_install.POD_SELECTOR.split("=")
    assert ds["spec"]["template"]["metadata"]["labels"][k] == v


def test_api_access_is_read_only_on_the_installer_daemonset():
    docs = list(yaml.safe_load_all((DEPLOY_DIR / "manifests/k7-node-installer/k7-api-access/rbac.yaml").read_text()))
    role, binding = docs
    assert role["rules"] == [
        {"apiGroups": ["apps"], "resources": ["daemonsets"], "resourceNames": ["k7-node-installer"], "verbs": ["get"]}
    ]
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "k7-api", "namespace": "kube-system"}]
    assert role["metadata"]["namespace"] == binding["metadata"]["namespace"] == "k7-system"


def test_release_bundles_leave_out_the_api_access():
    text = "\n".join(f.read_text() for f in (DEPLOY_DIR / "manifests/k7-node-installer").glob("*.yaml"))
    assert "k7-api-remote-install" not in text


def test_api_never_runs_ansible_or_creates_workloads():
    src = Path(remote_install.__file__).read_text()
    assert not re.search(r"^\s*(import|from)\s+\S*(subprocess|ansible|tempfile)", src, re.M)
    calls = set(re.findall(r"\.(\w+)\(", src))
    k8s_writes = {c for c in calls if re.match(r"(create|patch|replace|delete)_", c)}
    assert k8s_writes == {"create_namespaced_config_map", "patch_node", "delete_namespaced_config_map"}
