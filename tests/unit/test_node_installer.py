"""k7-node-installer entrypoint, image pins, manifests, offline role."""

import json
import re
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from k7 import __version__
from k7.deploy.node_installer import entrypoint as ep
from tests.unit.conftest import DEPLOY_DIR

INSTALLER = DEPLOY_DIR / "node_installer"
MANIFESTS = DEPLOY_DIR / "manifests" / "k7-node-installer"
ROLE_TASKS = DEPLOY_DIR / "roles" / "k7_node_prep" / "tasks"
OK = {"distro": "k3s", "backends": ["kfd", "k7d-fc"], "diskSource": "spare-disk"}
READY = "k7.katakate.org/node-ready"


@pytest.mark.parametrize(
    "cfg, match",
    [
        ({**OK, "colour": "red"}, "unknown installer config key"),
        ({**OK, "distro": "talos"}, "distro="),
        ({**OK, "backends": []}, "non-empty"),
        ({**OK, "backends": ["kfd", "fd"]}, "unknown or duplicate"),
        ({**OK, "backends": ["k7d", "k7d"]}, "unknown or duplicate"),
        ({**OK, "diskSource": "device:sdb"}, "diskSource="),
        ({**OK, "diskSource": "loopback"}, "kfd needs one"),
        ({**OK, "k7dDisksImageSize": "64GB"}, "k7dDisksImageSize"),
        ({**OK, "allowServerRestart": "yes"}, "true or false"),
        ([], "JSON object"),
    ],
)
def test_config_rejects(cfg, match):
    with pytest.raises(ep.InstallerError, match=match):
        ep.validate_config(cfg)


def test_config_accepts_and_maps_to_role_vars():
    cfg = ep.validate_config({**OK, "diskSource": "device:/dev/nvme1n1", "kataThinpoolPvSize": "400G"})
    ev = ep.extra_vars(cfg)
    assert ev["k7_backends"] == "kfd,k7d-fc"
    assert ev["k7_node_prep_only"] is True and ev["k7_node_installer"] is True
    assert ev["k7_artifact_dir"] == "/opt/k7-artifacts" and ev["kata_thinpool_pv_size"] == "400G"
    assert "k7d_disks_image_size" not in ev
    assert ep.config_sha(cfg) == ep.config_sha(dict(reversed(list(cfg.items()))))


def test_ready_labels():
    labels = ep.ready_labels(OK, nested=True)
    assert labels[READY] == __version__
    assert labels["k7.katakate.org/backend-kata-firecracker-devmapper"] == "true"
    assert labels["k7.katakate.org/backend-k7d-fc"] == "true"
    assert labels["k7.katakate.org/backend-k7d"] == "true"  # k7d-fc implies the k7d selector
    assert labels["k7.katakate.org/backend-kata-qemu-longhorn"] is None
    assert labels["k7.katakate.org/virt"] == "nested"
    assert ep.ready_labels(OK, nested=False)["k7.katakate.org/virt"] is None
    assert set(ep.unready_labels()) == {READY, *ep.BACKEND_LABELS}
    assert all(v is None for v in ep.unready_labels().values())


def test_is_nested():
    assert ep.is_nested("processor\t: 0\nflags\t\t: fpu vme hypervisor lahf_lm\n")
    assert not ep.is_nested("flags\t\t: fpu vme svm lahf_lm\n")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_retry_recovers_after_apiserver_restart():
    clock, fn = Clock(), MagicMock(side_effect=[ConnectionRefusedError(), urllib.error.URLError("down"), {"ok": 1}])
    assert ep.retry(fn, "x", sleep=clock.sleep, clock=clock) == {"ok": 1}
    assert clock.t == 3.0


def test_retry_is_bounded():
    clock = Clock()
    with pytest.raises(ep.InstallerError, match="unreachable for 300s"):
        ep.retry(MagicMock(side_effect=ConnectionRefusedError()), "x", sleep=clock.sleep, clock=clock)
    assert clock.t <= ep.LABEL_PATCH_DEADLINE_S


def _http(code: int, body: bytes) -> urllib.error.HTTPError:
    err = urllib.error.HTTPError("u", code, "x", {}, None)  # type: ignore[arg-type]
    err.read = lambda: body  # type: ignore[method-assign]
    return err


def test_retry_fails_fast_on_4xx():
    with pytest.raises(ep.InstallerError, match="HTTP 422 bad label"):
        ep.retry(MagicMock(side_effect=_http(422, b"bad label")), "x", sleep=lambda s: None)


def test_retry_rides_out_rbac_cache_sync_but_not_forever():
    """Seen on K3s: the restarted apiserver answers 403 until RBAC syncs."""
    clock = Clock()
    fn = MagicMock(side_effect=[_http(503, b""), _http(403, b"cannot patch"), {"ok": 1}])
    assert ep.retry(fn, "x", sleep=clock.sleep, clock=clock) == {"ok": 1}
    with pytest.raises(ep.InstallerError, match="unreachable for 300s: HTTP 403 denied"):
        ep.retry(MagicMock(side_effect=_http(403, b"denied")), "x", sleep=clock.sleep, clock=clock)


class FakeKube:
    def __init__(self, labels=None, pods=()):
        self.labels = dict(labels or {})
        self.pods = list(pods)
        self.calls = []

    def request(self, method, path, body=None, content_type="application/json"):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/api/v1/nodes/"):
            return {"metadata": {"labels": self.labels}}
        if method == "GET" and path.startswith("/api/v1/pods"):
            return {"items": self.pods}
        if method == "DELETE":
            self.pods = []
        return {}

    def patches(self):
        return [b["metadata"]["labels"] for m, _, b in self.calls if m == "PATCH"]

    def events(self):
        return [b["reason"] for m, p, b in self.calls if m == "POST" and p.endswith("/events")]


@pytest.fixture()
def host(tmp_path, monkeypatch):
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc/cpuinfo").write_text("flags\t\t: fpu svm\n")
    monkeypatch.setattr(ep, "HOST_ROOT", tmp_path)
    monkeypatch.setattr(ep, "STATE_FILE", tmp_path / "var/lib/k7/installer-state.json")
    return tmp_path


def test_install_fresh_unlabels_runs_role_then_labels(host):
    kube, playbook = FakeKube(), MagicMock()
    ep.install(OK, kube, "n1", playbook)  # type: ignore[arg-type]
    assert playbook.call_args.args[:3] == ("k7-install-node.yaml", "n1", False)
    first, last = kube.patches()
    assert first == ep.unready_labels() and last[READY] == __version__
    state = json.loads(ep.STATE_FILE.read_text())
    assert state["version"] == __version__ and state["config_sha"] == ep.config_sha(OK)
    assert state["backends"] == OK["backends"] and state["completed_at"]
    assert kube.events() == ["K7NodeReady"]


def test_install_same_config_keeps_label_during_rerun(host):
    ep.install(OK, FakeKube(), "n1", MagicMock())  # type: ignore[arg-type]
    kube, playbook = FakeKube(), MagicMock()
    ep.install(OK, kube, "n1", playbook)  # type: ignore[arg-type]
    playbook.assert_called_once()
    assert [p[READY] for p in kube.patches()] == [__version__]


def test_install_changed_config_unlabels_first(host):
    ep.install(OK, FakeKube(), "n1", MagicMock())  # type: ignore[arg-type]
    kube = FakeKube()
    ep.install({**OK, "backends": ["kfd", "kql", "k7d-fc"]}, kube, "n1", MagicMock())  # type: ignore[arg-type]
    first, last = kube.patches()
    assert first[READY] is None and last["k7.katakate.org/backend-kata-qemu-longhorn"] == "true"


def test_install_failure_never_leaves_ready_label(host):
    kube = FakeKube()
    playbook = MagicMock(side_effect=ep.InstallerError("k7-install-node.yaml failed at task 'K7d — run install.sh'"))
    with pytest.raises(ep.InstallerError):
        ep.install(OK, kube, "n1", playbook)  # type: ignore[arg-type]
    assert kube.patches() == [ep.unready_labels(), ep.unready_labels()]
    assert kube.events() == ["K7NodeInstallFailed"]
    assert "K7d — run install.sh" in next(b["message"] for m, _, b in kube.calls if m == "POST")
    assert not ep.STATE_FILE.exists()


def test_install_failure_posts_event_even_if_unlabel_fails(host):
    class UnlabelFails(FakeKube):
        def request(self, method, path, body=None, content_type="application/json"):
            if method == "PATCH" and self.patches():
                raise ep.InstallerError("403")
            return super().request(method, path, body, content_type)

    kube = UnlabelFails()
    with pytest.raises(ep.InstallerError, match="403"):
        ep.install(OK, kube, "n1", MagicMock(side_effect=ep.InstallerError("role failed")))  # type: ignore[arg-type]
    assert kube.events() == ["K7NodeInstallFailed"]


def test_install_refuses_k3s_server_unless_allowed(host):
    server = {"node-role.kubernetes.io/control-plane": "true"}
    kube, playbook = FakeKube(server), MagicMock()
    with pytest.raises(ep.InstallerError, match="allowServerRestart"):
        ep.install(OK, kube, "n1", playbook)  # type: ignore[arg-type]
    playbook.assert_not_called()
    assert kube.patches() == []
    ep.install({**OK, "allowServerRestart": True}, FakeKube(server), "n1", playbook)  # type: ignore[arg-type]
    assert playbook.call_args.args[2] is True  # inventory puts the node in k7_servers
    ep.install({**OK, "distro": "kubeadm"}, FakeKube(server), "n1", MagicMock())  # type: ignore[arg-type]


SANDBOX = {"metadata": {"namespace": "default", "name": "sb"}, "spec": {"runtimeClassName": "k7"}, "status": {}}
OTHER = {"metadata": {"namespace": "kube-system", "name": "x"}, "spec": {}, "status": {}}


def test_cleanup_refuses_while_a_sandbox_runs(host):
    kube, playbook = FakeKube(pods=[SANDBOX, OTHER]), MagicMock()
    with pytest.raises(ep.InstallerError, match="refusing cleanup: 1 K7 sandbox pod.*default/sb"):
        ep.cleanup(OK, kube, "n1", False, playbook)  # type: ignore[arg-type]
    playbook.assert_not_called()
    assert kube.patches() == []


def test_cleanup_force_deletes_sandboxes_then_cleans(host):
    ep.STATE_FILE.parent.mkdir(parents=True)
    ep.STATE_FILE.write_text("{}")
    kube, playbook = FakeKube(pods=[SANDBOX]), MagicMock()
    ep.cleanup(OK, kube, "n1", True, playbook)  # type: ignore[arg-type]
    assert ("DELETE", "/api/v1/namespaces/default/pods/sb", None) in kube.calls
    assert playbook.call_args.args[0] == "k7-node-cleanup.yaml"
    assert kube.patches()[0]["k7.katakate.org/virt"] is None
    assert not ep.STATE_FILE.exists()


def test_bake_skips_first_boot_and_touches_no_api():
    playbook = MagicMock()
    ep.bake(OK, playbook)
    assert playbook.call_args.kwargs["skip_tags"] == "first-boot"


def test_inventory_groups():
    inv = ep.inventory("n1", server=False)
    assert "[k7_agents]\nn1 ansible_connection=k7_nsenter ansible_become=false" in inv
    assert "[k7_cluster:children]\nk7_servers\nk7_agents" in inv


def test_run_playbook_names_failed_task(monkeypatch):
    out = "TASK [Node prep — a]\nok: [n1]\nTASK [K7d — run install.sh]\nfatal: [n1]: FAILED!\nTASK [x]\n"
    proc = MagicMock(stdout=iter(out.splitlines(keepends=True)), wait=MagicMock(return_value=2))
    with (
        patch.object(ep.subprocess, "Popen", return_value=proc) as popen,
        pytest.raises(ep.InstallerError, match="failed at task 'K7d — run install.sh'"),
    ):
        ep.run_playbook("k7-install-node.yaml", "n1", False, {"k7_distro": "k3s"}, skip_tags="first-boot")
    cmd, env = popen.call_args.args[0], popen.call_args.kwargs["env"]
    assert cmd[-2:] == ["--skip-tags", "first-boot"]
    assert env["ANSIBLE_CONNECTION_PLUGINS"].endswith("node_installer/connection_plugins")


def test_main_usage_and_config_errors(tmp_path, monkeypatch):
    assert ep.main([]) == 2
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps({**OK, "backends": ["nope"]}))
    monkeypatch.setenv("K7_INSTALLER_CONFIG", str(bad))
    assert ep.main(["install"]) == 1


def test_sandbox_pods_skip_finished_and_non_sandbox_pods():
    done = {**SANDBOX, "metadata": {"namespace": "default", "name": "done"}, "status": {"phase": "Succeeded"}}
    failed = {**SANDBOX, "metadata": {"namespace": "default", "name": "failed"}, "status": {"phase": "Failed"}}
    kube = FakeKube(pods=[SANDBOX, OTHER, done, failed])
    assert ep.sandbox_pods(kube, "n1") == ["default/sb"]  # type: ignore[arg-type]
    assert kube.calls[0][1] == "/api/v1/pods?fieldSelector=spec.nodeName%3Dn1"


def test_cleanup_fails_when_sandboxes_never_go_away(host, monkeypatch):
    class Sticky(FakeKube):
        def request(self, method, path, body=None, content_type="application/json"):
            self.calls.append((method, path, body))
            if method == "GET" and path.startswith("/api/v1/pods"):
                return {"items": [SANDBOX]}
            return {"metadata": {"labels": {}}} if method == "GET" else {}

    clock = Clock()
    monkeypatch.setattr(ep.time, "monotonic", clock)
    monkeypatch.setattr(ep.time, "sleep", clock.sleep)
    playbook = MagicMock()
    with pytest.raises(ep.InstallerError, match="still on n1 after 300s"):
        ep.cleanup(OK, Sticky(), "n1", True, playbook)  # type: ignore[arg-type]
    playbook.assert_not_called()


def test_node_event_failure_is_logged_not_raised(capsys):
    class EventsDown(FakeKube):
        def request(self, method, path, body=None, content_type="application/json"):
            if method == "POST":
                raise ep.InstallerError("events forbidden")
            return super().request(method, path, body, content_type)

    ep.node_event(EventsDown(), "n1", "K7NodeReady", "m" * 2000, warning=False)  # type: ignore[arg-type]
    assert "events forbidden" in capsys.readouterr().err


def test_node_event_shape():
    kube = FakeKube()
    ep.node_event(kube, "n1", "K7NodeInstallFailed", "x" * 2000, warning=True)  # type: ignore[arg-type]
    (_, path, ev) = kube.calls[0]
    assert path == "/api/v1/namespaces/default/events"
    assert ev["involvedObject"] == {"kind": "Node", "name": "n1", "uid": "n1", "apiVersion": "v1"}
    assert ev["type"] == "Warning" and len(ev["message"]) == 1024


def test_kube_requires_in_cluster_env(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    with pytest.raises(ep.InstallerError, match="run in a pod"):
        ep.Kube()


def test_kube_rereads_the_rotating_token(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    monkeypatch.setattr(ep, "SA_DIR", tmp_path)
    (tmp_path / "token").write_text("tok-1\n")
    with patch.object(ep.ssl, "create_default_context") as ctx:
        kube = ep.Kube()
    assert ctx.call_args.kwargs["cafile"] == str(tmp_path / "ca.crt")
    seen = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def urlopen(req, context, timeout):
        seen.append((req.full_url, req.get_method(), req.headers["Authorization"], req.data))
        return Resp(b'{"ok": 1}' if len(seen) == 1 else b"")

    with patch.object(ep.urllib.request, "urlopen", side_effect=urlopen):
        assert kube.request("PATCH", "/api/v1/nodes/n1", {"a": 1}) == {"ok": 1}
        (tmp_path / "token").write_text("tok-2\n")
        assert kube.request("GET", "/api/v1/nodes/n1") == {}
    assert seen[0] == ("https://10.0.0.1:443/api/v1/nodes/n1", "PATCH", "Bearer tok-1", b'{"a": 1}')
    assert seen[1][2:] == ("Bearer tok-2", None)


def _main_env(tmp_path, monkeypatch, cfg=OK):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg))
    monkeypatch.setenv("K7_INSTALLER_CONFIG", str(cfg_file))
    monkeypatch.setattr(ep, "READY_FILE", tmp_path / "ready")
    return cfg_file


def test_main_requires_node_name(tmp_path, monkeypatch, capsys):
    _main_env(tmp_path, monkeypatch)
    monkeypatch.delenv("K7_NODE_NAME", raising=False)
    assert ep.main(["install"]) == 1
    assert "K7_NODE_NAME unset" in capsys.readouterr().err


def test_main_bake_needs_no_node_or_api(tmp_path, monkeypatch):
    _main_env(tmp_path, monkeypatch)
    monkeypatch.delenv("K7_NODE_NAME", raising=False)
    with patch.object(ep, "bake") as bake, patch.object(ep, "Kube") as kube:
        assert ep.main(["bake"]) == 0
    bake.assert_called_once_with(OK)
    kube.assert_not_called()


class _StopLoop(Exception):
    pass


@pytest.mark.parametrize(("argv", "force"), [(["cleanup"], False), (["cleanup", "--force"], True)])
def test_main_cleanup_forwards_force_and_marks_ready(tmp_path, monkeypatch, argv, force):
    _main_env(tmp_path, monkeypatch)
    monkeypatch.setenv("K7_NODE_NAME", "n1")
    with (
        patch.object(ep, "Kube"),
        patch.object(ep, "cleanup") as cleanup,
        patch.object(ep.signal, "signal"),
        patch.object(ep.time, "sleep", side_effect=_StopLoop),
        pytest.raises(_StopLoop),
    ):
        ep.main(argv)
    assert cleanup.call_args.kwargs == {"force": force}
    assert ep.READY_FILE.read_text() == f"cleanup {__version__}\n"


def test_main_mode_from_env(tmp_path, monkeypatch):
    _main_env(tmp_path, monkeypatch)
    monkeypatch.setenv("K7_INSTALLER_MODE", "bake")
    with patch.object(ep, "bake") as bake:
        assert ep.main([]) == 0
    bake.assert_called_once()


def test_main_install_reexecs_when_the_config_changes(tmp_path, monkeypatch):
    cfg_file = _main_env(tmp_path, monkeypatch)
    monkeypatch.setenv("K7_NODE_NAME", "n1")
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 2:
            cfg_file.write_text(json.dumps({**OK, "backends": ["kfd"]}))

    with (
        patch.object(ep, "Kube"),
        patch.object(ep, "install") as install,
        patch.object(ep.signal, "signal"),
        patch.object(ep.time, "sleep", side_effect=sleep),
        patch.object(ep.os, "execv", side_effect=_StopLoop) as execv,
        pytest.raises(_StopLoop),
    ):
        ep.main(["install"])
    install.assert_called_once()
    assert sleeps == [ep.CONFIG_POLL_S] * 2  # unchanged config: no re-run on the first poll
    assert execv.call_args.args[1][-1] == "install"
    assert not ep.READY_FILE.exists()


def test_main_cleanup_never_reexecs_on_config_change(tmp_path, monkeypatch):
    cfg_file = _main_env(tmp_path, monkeypatch)
    monkeypatch.setenv("K7_NODE_NAME", "n1")
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        cfg_file.write_text(json.dumps({**OK, "backends": ["kfd"]}))
        if len(sleeps) == 3:
            raise _StopLoop

    with (
        patch.object(ep, "Kube"),
        patch.object(ep, "cleanup"),
        patch.object(ep.signal, "signal"),
        patch.object(ep.time, "sleep", side_effect=sleep),
        patch.object(ep.os, "execv") as execv,
        pytest.raises(_StopLoop),
    ):
        ep.main(["cleanup"])
    execv.assert_not_called()


def test_connection_plugin_runs_in_host_namespaces():
    from k7.deploy.node_installer.connection_plugins import k7_nsenter

    conn = object.__new__(k7_nsenter.Connection)
    with (
        patch.object(k7_nsenter.LocalConnection, "exec_command", return_value=(0, b"", b"")) as ex,
        patch.object(k7_nsenter.LocalConnection, "put_file") as put,
    ):
        conn.exec_command("/bin/sh -c 'echo hi'")
        conn.put_file("/tmp/a", "/root/.ansible/tmp/b")
    assert ex.call_args.args[0] == "nsenter --target 1 --all --root --wd -- /bin/sh -c 'echo hi'"
    assert put.call_args.args == ("/tmp/a", "/host/root/.ansible/tmp/b")
    assert k7_nsenter.Connection.transport == "k7_nsenter"  # not 'local': no controller cwd hack


# ── manifests ─────────────────────────────────────────────────────


def _docs(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


@pytest.mark.parametrize("path", [MANIFESTS / "daemonset.yaml", MANIFESTS / "cleanup/daemonset.yaml"])
def test_daemonsets_are_opt_in_privileged_hostpid(path):
    (ds,) = _docs(path)
    pod = ds["spec"]["template"]["spec"]
    assert ds["metadata"]["namespace"] == "k7-system"
    assert pod["hostPID"] is True
    assert pod["nodeSelector"] == {"k7.katakate.org/node-pool": "true"}
    assert pod["tolerations"] == []
    (c,) = pod["containers"]
    assert c["securityContext"] == {"privileged": True}
    assert c["image"] == "__K7_NODE_INSTALLER_IMAGE__"  # the release renders the locked digest
    mounts = {m["name"]: m for m in c["volumeMounts"]}
    assert mounts["host-root"]["mountPath"] == "/host" and "subPath" not in mounts["config"]
    vols = {v["name"]: v for v in pod["volumes"]}
    assert vols["host-root"]["hostPath"]["path"] == "/"
    assert pod["serviceAccountName"] == {"install": "k7-node-installer", "cleanup": "k7-node-cleanup"}[c["args"][0]]


def test_rbac_is_minimal_and_admission_policies_pin_own_node():
    roles = {d["metadata"]["name"]: d for d in _docs(MANIFESTS / "rbac.yaml") if d["kind"] == "ClusterRole"}
    assert roles["k7-node-installer"]["rules"] == [
        {"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "patch"]}
    ]
    assert "*" not in json.dumps(roles)
    docs = {(d["kind"], d["metadata"]["name"]): d for d in _docs(MANIFESTS / "admission-policy.yaml")}
    names = {"k7-node-installer-own-node": "UPDATE", "k7-node-cleanup-own-node-pods": "DELETE"}
    assert set(docs) == {
        (k, n) for n in names for k in ("ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding")
    }
    exprs = {}
    for name, op in names.items():
        vap, binding = docs["ValidatingAdmissionPolicy", name], docs["ValidatingAdmissionPolicyBinding", name]
        (rule,) = vap["spec"]["matchConstraints"]["resourceRules"]
        assert vap["spec"]["failurePolicy"] == "Fail" and rule["operations"] == [op]
        assert binding["spec"] == {"policyName": name, "validationActions": ["Deny"]}
        exprs[name] = " ".join(v["expression"] for v in vap["spec"]["validations"])
        assert "authentication.kubernetes.io/node-name" in exprs[name]
    node = exprs["k7-node-installer-own-node"]
    assert "k7.katakate.org/" in node and "object.spec == oldObject.spec" in node
    for field in ("annotations", "finalizers", "ownerReferences"):
        assert f"object.metadata.{field} == oldObject.metadata.{field}" in node, field
    pods = exprs["k7-node-cleanup-own-node-pods"]
    assert "== oldObject.spec.nodeName" in pods
    rcs = re.search(r"runtimeClassName in \[([^\]]*)\]", pods)
    assert rcs and {s.strip(" '") for s in rcs.group(1).split(",")} == ep.SANDBOX_RUNTIME_CLASSES
    cfg = json.loads(_docs(MANIFESTS / "configmap.yaml")[0]["data"]["config.json"])
    ep.validate_config(cfg)


def test_runtime_classes_select_their_backend_label():
    text = (DEPLOY_DIR / "roles/k7_cluster/tasks/nodes.yml").read_text()
    for rc, backend in {"kata": "kata-firecracker-devmapper", "kata-qemu": "kata-qemu-longhorn", "k7": "k7d"}.items():
        assert (
            f'name: {rc}\n    handler: {rc}\n    scheduling:\n      nodeSelector:\n        k7.katakate.org/backend-{backend}: "true"'
            in text
        )
    assert (
        'handler: k7-fc\n    scheduling:\n      nodeSelector:\n        k7.katakate.org/backend-k7d-fc: "true"' in text
    )


# ── offline role + baked artifacts ────────────────────────────────


def _tasks():
    for f in sorted(ROLE_TASKS.glob("*.yml")):
        for t in yaml.safe_load(f.read_text()) or []:
            yield f.name, t


def test_installer_mode_never_downloads():
    """Every download in k7_node_prep is skipped when the installer sets its vars."""
    offline_guards = ("k7_artifact_dir | length == 0", "not (k7_node_installer | bool)", "k7_local_image_build | bool")
    assert not {"k7_api_image", "k7_api_remote_install", "k7_node_installer_image"} & set(ep.extra_vars(OK))
    downloads = 0
    for name, t in _tasks():
        body = json.dumps({k: v for k, v in t.items() if k not in ("name", "when")})
        if not re.search(r"ansible\.builtin\.(get_url|uri)\b|curl ", body):
            continue
        downloads += 1
        when = t.get("when", [])
        when = [when] if isinstance(when, str) else when
        assert any(g in w for w in when for g in offline_guards), f"{name}: {t['name']}"
    assert downloads >= 5


def test_installer_mode_leaves_host_firewall_alone():
    """The CNI owns netfilter on a running cluster: no nft flush, no iptables
    inserts, no switch of the iptables backend."""
    touching = 0
    for name, t in _tasks():
        body = json.dumps({k: v for k, v in t.items() if k not in ("name", "when")})
        if not re.search(r"nft flush|ansible\.builtin\.iptables|update-alternatives --set ip6?tables", body):
            continue
        touching += 1
        assert t.get("when") == "not (k7_node_installer | bool)", f"{name}: {t['name']}"
    assert touching == 5


def test_installer_restarts_containerd_only_on_change_and_waits():
    tasks = {t["name"]: t for name, t in _tasks() if name == "containerd.yml"}
    restart = tasks["Restart the containerd service to apply runtime handlers (async)"]
    assert restart["when"] == "not (k7_node_installer | bool) or k7_containerd_config.changed"
    wait = tasks["Wait for the containerd service restart to finish (installer)"]
    assert wait["ansible.builtin.async_status"]["jid"] == "{{ k7_containerd_restart.ansible_job_id }}"
    assert "k7_node_installer | bool" in wait["when"]


def _artifacts() -> dict[str, tuple[str, str]]:
    rows = [line.split() for line in (INSTALLER / "artifacts.txt").read_text().splitlines()]
    return {r[0]: (r[1], r[2]) for r in rows if r and not r[0].startswith("#")}


def test_artifacts_match_playbook_and_k7d_fc_pins():
    v = yaml.safe_load((DEPLOY_DIR / "k7-install-node.yaml").read_text())[0]["vars"]
    pins = dict(line.split("=", 1) for line in (DEPLOY_DIR / "k7d-fc/pins.env").read_text().splitlines() if "=" in line)
    expected = {
        f"cni-plugins-linux-amd64-{v['cni_plugins_version']}.tgz",
        f"firecracker-{v['firecracker_version']}-x86_64.tgz",
        f"firecracker-{pins['FIRECRACKER_VERSION']}-x86_64.tgz",
        f"kata-static-{v['kata_version']}-amd64.tar.zst",
        f"k7d-v{v['k7d_version']}-x86_64-linux.tar.gz",
    }
    arts = _artifacts()
    assert set(arts) == expected
    for file, (sha, url) in arts.items():
        assert re.fullmatch(r"[0-9a-f]{64}", sha) and url.startswith("https://") and url.endswith("/" + file)
    assert arts[f"cni-plugins-linux-amd64-{v['cni_plugins_version']}.tgz"][0] == v["cni_plugins_sha256"]
    assert arts[f"firecracker-{v['firecracker_version']}-x86_64.tgz"][0] == v["firecracker_sha256"]
    assert arts[f"kata-static-{v['kata_version']}-amd64.tar.zst"][0] == v["kata_static_sha256"]
    assert arts[f"k7d-v{v['k7d_version']}-x86_64-linux.tar.gz"][0] == v["k7d_sha256s"][v["k7d_version"]]
    assert arts[f"firecracker-{pins['FIRECRACKER_VERSION']}-x86_64.tgz"] == (
        pins["FIRECRACKER_TGZ_SHA256"],
        pins["FIRECRACKER_RELEASE_URL"],
    )


def test_image_is_pinned_and_has_no_runtime_downloader():
    dockerfile = (INSTALLER / "Dockerfile").read_text()
    bases = re.findall(r"^FROM (\S+)", dockerfile, re.M)
    assert len(bases) == 2 and len(set(bases)) == 1, bases
    assert re.fullmatch(r"python:[0-9.]+-slim-bookworm@sha256:[0-9a-f]{64}", bases[0])
    runtime = dockerfile.rsplit(f"FROM {bases[0]}\n", 1)[1]
    assert "curl" not in runtime and "fetch-artifacts" not in runtime
    reqs = [line for line in (INSTALLER / "requirements.txt").read_text().splitlines() if not line.startswith("#")]
    assert reqs and all(re.fullmatch(r"[a-z0-9-]+==[0-9.]+", r) for r in reqs)
