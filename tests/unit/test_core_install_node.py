"""core.install_node leaves no temp files, whether or not ansible-playbook runs."""

import subprocess
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def test_missing_ansible_playbook_leaves_no_temp_files(core, tmp_root, monkeypatch):
    def no_ansible(*_a, **_k):
        raise FileNotFoundError(2, "No such file or directory", "ansible-playbook")

    monkeypatch.setattr(subprocess, "Popen", no_ansible)
    result = core.install_node("- hosts: all\n", "[k7_servers]\n", extra_vars={"k7_distro": "k3s"})
    assert not result.success and "ansible-playbook" in (result.error or "")
    assert list(tmp_root.iterdir()) == []


def test_playbook_gets_its_files_and_they_are_removed_after(core, tmp_root, monkeypatch):
    real_popen, seen = subprocess.Popen, {}

    def fake_popen(cmd, **kwargs):
        seen.update({Path(p).name: Path(p).read_text() for p in (cmd[2], cmd[3], cmd[5].lstrip("@"))})
        return real_popen(["echo", "TASK [x]"], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    result = core.install_node("- hosts: all\n", "[k7_servers]\nh\n", extra_vars={"k7_distro": "k3s"})
    assert result.success, result.error
    assert seen == {
        "inventory.ini": "[k7_servers]\nh\n",
        "playbook.yaml": "- hosts: all\n",
        "extra-vars.yml": "k7_distro: k3s\n",
    }
    assert list(tmp_root.iterdir()) == []
