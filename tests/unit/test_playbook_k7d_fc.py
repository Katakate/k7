"""k7d-fc vendored files must come from the packaged deploy dir, not playbook_dir."""

from pathlib import Path

from tests.unit.conftest import install_tree_text

PLAYBOOK = Path(__file__).resolve().parents[2] / "src/k7/deploy/k7-install-node.yaml"


def test_playbook_k7d_fc_files_come_from_deploy_dir_not_tempfile_playbook_dir():
    """The CLI writes the playbook to /tmp; playbook_dir-relative src fails."""
    text = install_tree_text()
    stage = text.split("K7d-fc — stage install-firecracker.sh and pins", 1)[1]
    stage = stage.split("K7d-fc — install pinned Firecracker", 1)[0]
    assert '"{{ k7_deploy_dir }}/k7d-fc/{{ item.src }}"' in stage
    assert "k7_repo_root" not in stage
    assert "src: k7d-fc/install-firecracker.sh" not in stage
    assert "src: k7d-fc/pins.env" not in stage
