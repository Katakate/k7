"""Playbook turns off Kata's guest seccomp bypass."""

from pathlib import Path

PLAYBOOK = Path(__file__).resolve().parents[2] / "src/k7/deploy/k7-install-node.yaml"


def test_playbook_disables_guest_seccomp_bypass_on_both_kata_configs():
    text = PLAYBOOK.read_text()
    assert "path: /opt/kata/share/defaults/kata-containers/configuration-qemu.toml" in text
    assert "path: /opt/kata/share/defaults/kata-containers/configuration-fc.toml" in text
    assert "regexp: '^#?\\s*disable_guest_seccomp\\s*='" in text
    assert text.count("line: 'disable_guest_seccomp = false'") == 2
    assert "line: 'disable_guest_seccomp = true'" not in text
    assert "qemu_guest_seccomp.rc != 0" in text
    assert "fc_guest_seccomp.rc != 0" in text
    assert "Refusing to leave Kata guest seccomp bypassed" in text
    qemu = text.split("Assert disable_guest_seccomp exists in kata-qemu config", 1)[1]
    qemu = qemu.split("Assert disable_guest_seccomp exists in kata-fc config", 1)[0]
    fc = text.split("Assert disable_guest_seccomp exists in kata-fc config", 1)[1]
    fc = fc.split("Create containerd shim symlink", 1)[0]
    assert "when: k7_has_longhorn" in qemu
    assert "when: k7_has_devmapper" in fc
    assert "k7_has_longhorn" not in fc.split("when:", 1)[1]


def test_playbook_does_not_regress_virtiofsd_thread_pool():
    """CHALLENGES.md #10: single-threaded virtiofsd wedges the kata agent."""
    text = PLAYBOOK.read_text()
    assert 'virtio_fs_extra_args = ["--thread-pool-size=16", "--announce-submounts"]' in text
