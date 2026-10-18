"""Node prep raises the per-user inotify instance limit that every pod shim draws on."""

import yaml

from tests.unit.conftest import DEPLOY_DIR

HOST = DEPLOY_DIR / "roles" / "k7_node_prep" / "tasks" / "host.yml"


def test_inotify_instance_limit_is_persisted_and_applied():
    tasks = {t["name"]: t for t in yaml.safe_load(HOST.read_text())}
    conf = tasks["Raise the per-user inotify instance limit"]["ansible.builtin.copy"]
    assert conf["dest"] == "/etc/sysctl.d/90-k7-inotify.conf"
    assert conf["content"] == "fs.inotify.max_user_instances = 8192\n"
    apply = tasks["Apply the inotify instance limit now"]
    assert apply["ansible.builtin.command"] == f"sysctl -p {conf['dest']}"
    assert "when" not in apply
    # Not gated on k7_node_installer: an existing cluster's nodes need the limit as much.
    assert "k7_node_installer" not in str(tasks["Raise the per-user inotify instance limit"].get("when", ""))
