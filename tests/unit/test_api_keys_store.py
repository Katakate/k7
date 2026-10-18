"""k7_api_keys: kubectl Secret wrapper, RBAC for the key store, playbook ordering."""

import base64
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from k7.deploy.k7_api_keys import Kubectl, main

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "src/k7/deploy/manifests/k7-api"
PLAYBOOK = (ROOT / "src/k7/deploy/roles/k7_cluster/tasks/api.yml").read_text()


def _done(stdout: str = "", stderr: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout, stderr)


def test_read_decodes_and_maps_notfound_to_none():
    kc = Kubectl(["kubectl"], "kube-system")
    payload = json.dumps({"data": {"abc": base64.b64encode(b'{"name":"n"}').decode()}})
    with patch("subprocess.run", return_value=_done(payload)) as run:
        assert kc.read("k7-api-keys") == {"abc": '{"name":"n"}'}
    assert run.call_args.args[0] == ["kubectl", "get", "secret", "k7-api-keys", "-o", "json", "-n", "kube-system"]
    with patch("subprocess.run", return_value=_done(stderr='Error from server (NotFound): secrets "x"', rc=1)):
        assert kc.read("x") is None


def test_read_other_errors_are_loud():
    with (
        patch("subprocess.run", return_value=_done(stderr="Forbidden", rc=1)),
        pytest.raises(RuntimeError, match="Forbidden"),
    ):
        Kubectl(["kubectl"], "kube-system").read("k7-api-keys")


def test_patch_is_json_merge_with_null_delete():
    with patch("subprocess.run", return_value=_done()) as run:
        Kubectl(["k3s", "kubectl"], "sys").patch("k7-api-keys", {"a": "v", "b": None})
    argv = run.call_args.args[0]
    assert argv[:6] == ["k3s", "kubectl", "patch", "secret", "k7-api-keys", "--type"]
    body = json.loads(argv[argv.index("-p") + 1])
    assert body == {"data": {"a": base64.b64encode(b"v").decode(), "b": None}}
    assert argv[-2:] == ["-n", "sys"]


def test_main_migrate_entrypoint(tmp_path: Path, capsys):
    with patch("k7.deploy.k7_api_keys.migrate", return_value="ok") as migrate:
        assert (
            main(["migrate", "--file", str(tmp_path / "k.json"), "--kubectl", "k3s kubectl", "--namespace", "ns1"]) == 0
        )
    kc, path = migrate.call_args.args
    assert kc.argv == ["k3s", "kubectl"] and kc.namespace == "ns1" and path == tmp_path / "k.json"
    assert capsys.readouterr().out.strip() == "ok"


def test_clusterrole_cannot_read_secrets_cluster_wide():
    role = (MANIFESTS / "clusterrole.yaml").read_text()
    secrets_rule = role.split('resources: ["secrets"]', 1)[1].split("\n", 2)[1]
    assert secrets_rule.strip() == 'verbs: ["create", "delete"]'
    assert role.count('"secrets"') == 1


def test_key_store_role_is_namespaced_and_named():
    role = (MANIFESTS / "role.yaml").read_text()
    assert "kind: Role\n" in role and "kind: RoleBinding" in role
    assert 'resourceNames: ["k7-api-keys"]\n    verbs: ["get", "patch"]' in role
    assert 'resourceNames: ["k7-api-keys-usage"]\n    verbs: ["get", "patch"]' in role
    secret_rules = role.split("rules:", 1)[1].split("coordination.k8s.io", 1)[0]
    assert "create" not in secret_rules


def test_deployment_is_ha_and_reads_secret_store():
    dep = (MANIFESTS / "deployment.yaml").read_text()
    assert "replicas: 2" in dep
    assert "nodeSelector" not in dep
    assert "name: K7_API_KEY_STORE\n              value: secret" in dep
    assert "path: /etc/k7\n" not in dep
    assert "minAvailable: 1" in (MANIFESTS / "pdb.yaml").read_text()


def test_playbook_migrates_and_publishes_before_apply():
    apply_at = PLAYBOOK.index("- name: Apply K7 API manifests")
    for task in (
        "- name: Migrate API keys into the k7-api-keys Secret",
        "- name: Publish agent token and host config to kube-system",
        "- name: Publish the Caddyfile ConfigMap",
        "- name: Publish the k7-api OIDC settings (ConfigMap k7-oidc)",
        "- name: Delete the k7-oidc ConfigMap when OIDC is not configured",
        "- name: Render the OIDC config hash into the k7-api Deployment",
    ):
        assert PLAYBOOK.index(task) < apply_at, task


def test_deployment_reads_optional_oidc_configmap_and_rolls_on_change():
    dep = (MANIFESTS / "deployment.yaml").read_text()
    assert "envFrom:\n            - configMapRef:\n                name: k7-oidc\n                optional: true" in dep
    assert 'k7.katakate.org/oidc-config: "__K7_OIDC_CONFIG_HASH__"' in dep
    assert "'-> role' in k7_api_keys_migrate.stdout" in PLAYBOOK


def test_clusterrole_manages_tenants_for_admin_routes():
    role = (MANIFESTS / "clusterrole.yaml").read_text()
    rule = role.split('apiGroups: ["k7.katakate.org"]', 1)[1].split("\n", 3)
    assert rule[1].strip() == 'resources: ["tenants"]'
    assert rule[2].strip() == 'verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]'


@pytest.mark.parametrize(
    ("rc", "stderr", "match"),
    [
        (1, 'Error from server (NotFound): tenants.k7.katakate.org "acme" not found', "does not exist"),
        (1, 'error: the server doesn\'t have a resource type "tenants"', "not installed"),
        (1, "Forbidden", "Forbidden"),
    ],
)
def test_require_tenant_errors_are_loud(rc, stderr, match):
    kc = Kubectl(["kubectl"], "kube-system")
    with patch("subprocess.run", return_value=_done(stderr=stderr, rc=rc)), pytest.raises(RuntimeError, match=match):
        kc.require_tenant("acme")
    with patch("subprocess.run", return_value=_done('{"spec": {"namespaces": ["a", "b"]}}')) as run:
        assert kc.require_tenant("acme") == ["a", "b"]
    assert run.call_args.args[0][:4] == ["kubectl", "get", "tenants.k7.katakate.org", "acme"]
