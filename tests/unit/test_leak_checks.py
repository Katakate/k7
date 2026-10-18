"""The end-of-suite VMM leak check must see the VMM names this install runs."""

from __future__ import annotations

import re
from pathlib import Path

from tests.integration.test_zz_leaks import count_vmms

PINS = Path(__file__).resolve().parents[2] / "src/k7/deploy/k7d-fc/pins.env"


def test_leak_scan_counts_the_versioned_firecracker_kata_runs():
    version = re.search(r"^FIRECRACKER_VERSION=(\S+)$", PINS.read_text(), re.M)
    assert version, f"{PINS} does not pin FIRECRACKER_VERSION"
    comm = f"firecracker-{version.group(1)}"[:15]
    ps = ["COMMAND", comm, "firecracker", "qemu-system-x86", "containerd-shim", "k7d"]
    assert count_vmms(ps) == {"firecracker": 2, "qemu": 1}
    assert count_vmms(["jailer-v1.16.2"]) == {"firecracker": 0, "qemu": 0}
