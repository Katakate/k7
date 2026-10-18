"""kfd delete must signal Firecracker, not the jailer --daemonize parent."""

import subprocess
from pathlib import Path

from tests.integration.test_zz_leaks import _is_firecracker_comm
from tests.unit.conftest import install_tree_text

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "src/k7/deploy/kata/k7-kata-jailer.sh"


def test_comm_match_sees_versioned_firecracker_name():
    """Linux comm is 15 bytes: firecracker-v1.16.2 shows up as firecracker-v1."""
    assert _is_firecracker_comm("firecracker")
    assert _is_firecracker_comm("firecracker-v1.")
    assert _is_firecracker_comm("firecracker-v1.16.2")
    assert not _is_firecracker_comm("jailer-v1.16.2")
    assert not _is_firecracker_comm("qemu-system-x86")


def test_wrapper_drops_daemonize_and_execs_jailer(tmp_path: Path):
    recorded = tmp_path / "argv"
    fake = tmp_path / "jailer"
    fake.write_text(f"#!/bin/bash\nprintf '%s\\n' \"$0\" \"$@\" > '{recorded}'\n")
    fake.chmod(0o755)

    script = WRAPPER.read_text()
    assert not any(
        line.strip().startswith("sleep") or "retry" in line
        for line in script.splitlines()
        if not line.strip().startswith("#")
    )
    wrapped = tmp_path / "k7-kata-jailer"
    wrapped.write_text(script.replace("/usr/local/bin/jailer", str(fake)))
    wrapped.chmod(0o755)

    argv = [
        "--id",
        "abc",
        "--exec-file",
        "/usr/local/bin/firecracker",
        "--uid",
        "0",
        "--gid",
        "0",
        "--daemonize",
        "--",
        "--config-file",
        "/fcConfig.json",
    ]
    subprocess.run([str(wrapped), *argv], check=True)
    lines = recorded.read_text().splitlines()
    assert lines[0] == str(fake)
    assert "--daemonize" not in lines
    assert lines[1:] == [a for a in argv if a != "--daemonize"]


def test_wrapper_fails_loud_when_jailer_missing(tmp_path: Path):
    script = WRAPPER.read_text().replace("/usr/local/bin/jailer", str(tmp_path / "missing-jailer"))
    wrapped = tmp_path / "k7-kata-jailer"
    wrapped.write_text(script)
    wrapped.chmod(0o755)
    result = subprocess.run([str(wrapped), "--id", "abc"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "not executable" in result.stderr


def test_playbook_points_kata_at_nondemonizing_jailer():
    text = install_tree_text()
    assert 'src: "{{ k7_deploy_dir }}/kata/k7-kata-jailer.sh"' in text
    assert "dest: /usr/local/bin/k7-kata-jailer" in text
    assert "{key: 'jailer_path', name: 'k7-kata-jailer'}" in text
    assert 'valid_jailer_paths = ["/usr/local/bin/k7-kata-jailer"]' in text
    assert "{key: 'jailer_path', name: 'jailer'}" not in text
    assert "pgrep -x firecracker" not in text
    assert "firecracker|firecracker-*)" in text
    assert "/usr/local/bin/k7-kata-jailer" in (ROOT / "src/k7/deploy/roles/k7_node_prep/tasks/cleanup.yml").read_text()


def _reaper_script() -> str:
    text = (ROOT / "src/k7/deploy/roles/k7_node_prep/tasks/runtimes.yml").read_text()
    start = text.index("      #!/bin/bash\n      # Reap orphaned kata VMM processes.\n")
    end = text.index("  when: k7_has_devmapper or k7_has_longhorn\n  tags: ['kata', 'reaper']", start)
    body = text[start:end]
    lines = [line[6:] if line.startswith("      ") else line for line in body.splitlines()]
    return "\n".join(lines) + "\n"


def test_reaper_script_is_valid_bash():
    script = _reaper_script()
    assert "pgrep -x firecracker" not in script
    assert '[[ ! "$id" =~ ^[0-9a-f]{32}$ ]]' in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


def test_reaper_leaves_k7d_fc_firecracker_alone(tmp_path: Path):
    """k7d-fc --id is vm-* / fcf-*, not Kata's 32 hex chars. No kata shim exists for those, so the reaper must skip them."""
    proc = tmp_path / "proc"
    killed = tmp_path / "killed"
    bins = tmp_path / "bin"
    bins.mkdir()
    (bins / "pgrep").write_text("#!/bin/bash\nexit 1\n")
    (bins / "pgrep").chmod(0o755)

    def add(pid: str, comm: str, args: list[str]) -> None:
        d = proc / pid
        d.mkdir(parents=True)
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")

    kata_id = "46f3df654adf5423e033d29456b298ef"
    add("101", "firecracker-v1.", ["/usr/local/bin/firecracker", "--id", "vm-22528-172"])
    add("102", "firecracker-v1.", ["/usr/local/bin/firecracker", "--id", "fcf-22528-24"])
    add("103", "firecracker-v1.", ["/usr/local/bin/firecracker", "--id", kata_id])
    add("104", "firecracker", ["/usr/local/bin/firecracker", "--id", "not-kata"])

    script = _reaper_script().replace("/proc/", f"{proc}/").replace('kill -9 "$pid"', f'echo "$pid" >> "{killed}"')
    subprocess.run(["bash", "-c", script], check=True, env={"PATH": f"{bins}:/usr/bin:/bin"})
    assert killed.read_text().split() == ["103"]
