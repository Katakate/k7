"""Unit tests for pure CLI utility functions."""

import json
from unittest.mock import MagicMock, patch

import pytest
import typer

from k7.cli.k7 import (
    _build_default_inventory,
    _kubectl_cmd,
    _normalize_backend,
    _parse_backends,
    _read_api_endpoint,
)

# --- _normalize_backend ---


class TestNormalizeBackend:
    def test_alias_kfd(self):
        assert _normalize_backend("kfd") == "kata-firecracker-devmapper"

    def test_alias_kql(self):
        assert _normalize_backend("kql") == "kata-qemu-longhorn"

    def test_deprecated_alias_fd_warns(self, capsys):
        assert _normalize_backend("fd") == "kata-firecracker-devmapper"
        err = capsys.readouterr().err
        assert "deprecated" in err.lower()
        assert "kfd" in err

    def test_deprecated_alias_ql_warns(self, capsys):
        assert _normalize_backend("ql") == "kata-qemu-longhorn"
        err = capsys.readouterr().err
        assert "deprecated" in err.lower()
        assert "kql" in err

    def test_deprecated_full_name_warns(self, capsys):
        assert _normalize_backend("firecracker-devmapper") == "kata-firecracker-devmapper"
        err = capsys.readouterr().err
        assert "deprecated" in err.lower()

    def test_full_name_firecracker(self):
        assert _normalize_backend("kata-firecracker-devmapper") == "kata-firecracker-devmapper"

    def test_full_name_qemu(self):
        assert _normalize_backend("kata-qemu-longhorn") == "kata-qemu-longhorn"

    def test_none_returns_none(self):
        assert _normalize_backend(None) is None

    def test_case_insensitive(self):
        assert _normalize_backend("KFD") == "kata-firecracker-devmapper"
        assert _normalize_backend("Kata-Qemu-Longhorn") == "kata-qemu-longhorn"

    def test_whitespace_stripped(self):
        assert _normalize_backend("  kfd  ") == "kata-firecracker-devmapper"

    def test_empty_string_raises(self):
        with pytest.raises(typer.BadParameter, match="cannot be empty"):
            _normalize_backend("")

    def test_whitespace_only_raises(self):
        with pytest.raises(typer.BadParameter, match="cannot be empty"):
            _normalize_backend("   ")

    def test_unknown_backend_raises(self):
        with pytest.raises(typer.BadParameter, match="Unsupported backend"):
            _normalize_backend("vmware")

    def test_k7d_accepted(self):
        assert _normalize_backend("k7d") == "k7d"

    def test_k7_alias_maps_to_k7d(self):
        assert _normalize_backend("k7") == "k7d"


# --- _kubectl_cmd ---


class TestKubectlCmd:
    def test_uses_k3s_when_available(self):
        with patch("k7.cli.k7.shutil.which", return_value="/usr/local/bin/k3s"):
            assert _kubectl_cmd() == ["k3s", "kubectl"]

    def test_falls_back_to_kubectl(self):
        with patch("k7.cli.k7.shutil.which", return_value=None):
            assert _kubectl_cmd() == ["kubectl"]


# --- _read_api_endpoint ---


_SINGLE_STACK = json.dumps(
    [
        {"address": "10.0.0.1", "type": "InternalIP"},
        {"address": "k7-node", "type": "Hostname"},
    ]
)

_DUAL_STACK = json.dumps(
    [
        {"address": "192.0.2.10", "type": "InternalIP"},
        {"address": "2001:db8::2", "type": "InternalIP"},
        {"address": "k7-node-01", "type": "Hostname"},
    ]
)


class TestReadApiEndpoint:
    def test_returns_endpoint_on_success(self):
        port_proc = MagicMock(returncode=0, stdout="31007")
        node_proc = MagicMock(returncode=0, stdout=_SINGLE_STACK)

        with patch("k7.cli.k7.subprocess.run", side_effect=[port_proc, node_proc]):
            result = _read_api_endpoint(["kubectl"])
            assert result == "http://10.0.0.1:31007"

    def test_picks_ipv4_on_dual_stack(self):
        port_proc = MagicMock(returncode=0, stdout="31007")
        node_proc = MagicMock(returncode=0, stdout=_DUAL_STACK)

        with patch("k7.cli.k7.subprocess.run", side_effect=[port_proc, node_proc]):
            result = _read_api_endpoint(["kubectl"])
            assert result == "http://192.0.2.10:31007"

    def test_returns_none_when_svc_missing(self):
        port_proc = MagicMock(returncode=1, stdout="")

        with patch("k7.cli.k7.subprocess.run", return_value=port_proc):
            assert _read_api_endpoint(["kubectl"]) is None

    def test_falls_back_to_localhost_when_no_node_ip(self):
        port_proc = MagicMock(returncode=0, stdout="31007")
        node_proc = MagicMock(returncode=0, stdout="")

        with patch("k7.cli.k7.subprocess.run", side_effect=[port_proc, node_proc]):
            result = _read_api_endpoint(["kubectl"])
            assert result == "http://localhost:31007"


# --- _build_default_inventory ---


class TestBuildDefaultInventory:
    def test_localhost_default_server(self):
        inv = _build_default_inventory(
            hosts=None,
            role="server",
            backends=["kata-firecracker-devmapper"],
            disk=None,
            longhorn_extra_disk=None,
        )
        assert "[k7_servers]" in inv
        assert "localhost ansible_connection=local" in inv
        assert "k7_backends=kata-firecracker-devmapper" in inv
        assert "[k7_agents]" in inv
        assert "[k7_cluster:children]" in inv

    def test_localhost_both_backends_default(self):
        # `k7 install` with no flags installs both backends by default.
        inv = _build_default_inventory(
            hosts=None,
            role="server",
            backends=["kata-firecracker-devmapper", "kata-qemu-longhorn"],
            disk=None,
            longhorn_extra_disk=None,
        )
        assert "k7_backends=kata-firecracker-devmapper,kata-qemu-longhorn" in inv

    def test_disk_propagated_for_devmapper(self):
        inv = _build_default_inventory(
            hosts=None,
            role="server",
            backends=["kata-firecracker-devmapper"],
            disk="/dev/nvme1n1",
            longhorn_extra_disk=None,
        )
        assert "k7_devmapper_disk=/dev/nvme1n1" in inv

    def test_disk_ignored_for_qemu_longhorn(self):
        inv = _build_default_inventory(
            hosts=None,
            role="server",
            backends=["kata-qemu-longhorn"],
            disk="/dev/nvme1n1",
            longhorn_extra_disk="/mnt/lh",
        )
        assert "k7_devmapper_disk" not in inv
        assert "longhorn_extra_disk=/mnt/lh" in inv

    def test_both_backends_propagate_their_disks(self):
        inv = _build_default_inventory(
            hosts=None,
            role="server",
            backends=["kata-firecracker-devmapper", "kata-qemu-longhorn"],
            disk="/dev/nvme1n1",
            longhorn_extra_disk="/mnt/lh",
        )
        assert "k7_backends=kata-firecracker-devmapper,kata-qemu-longhorn" in inv
        assert "k7_devmapper_disk=/dev/nvme1n1" in inv
        assert "longhorn_extra_disk=/mnt/lh" in inv

    def test_remote_hosts_as_server(self):
        inv = _build_default_inventory(
            hosts=["1.2.3.4", "5.6.7.8"],
            role="server",
            backends=["kata-firecracker-devmapper"],
            disk=None,
            longhorn_extra_disk=None,
        )
        assert "1.2.3.4 ansible_user=root" in inv
        assert "5.6.7.8 ansible_user=root" in inv

    def test_remote_hosts_as_agent(self):
        inv = _build_default_inventory(
            hosts=["agent1"],
            role="agent",
            backends=["kata-qemu-longhorn"],
            disk=None,
            longhorn_extra_disk=None,
        )
        # agent1 must land in [k7_agents], not [k7_servers]
        servers_section, agents_section = inv.split("[k7_agents]", 1)
        assert "agent1" not in servers_section
        assert "agent1" in agents_section


# --- _parse_backends ---


class TestParseBackends:
    def test_default_pair(self):
        assert _parse_backends("kata-firecracker-devmapper,kata-qemu-longhorn") == [
            "kata-firecracker-devmapper",
            "kata-qemu-longhorn",
        ]

    def test_aliases(self):
        assert _parse_backends("kfd,kql") == ["kata-firecracker-devmapper", "kata-qemu-longhorn"]

    def test_deprecated_aliases(self):
        assert _parse_backends("fd,ql") == ["kata-firecracker-devmapper", "kata-qemu-longhorn"]

    def test_single_value(self):
        assert _parse_backends("kql") == ["kata-qemu-longhorn"]

    def test_dedupes_and_preserves_order(self):
        assert _parse_backends("kql, kfd, kata-qemu-longhorn") == [
            "kata-qemu-longhorn",
            "kata-firecracker-devmapper",
        ]

    def test_strips_whitespace_and_empty(self):
        assert _parse_backends(" kfd , , kql ") == [
            "kata-firecracker-devmapper",
            "kata-qemu-longhorn",
        ]

    def test_rejects_unknown(self):
        with pytest.raises(typer.BadParameter, match="Unsupported backend"):
            _parse_backends("kfd,vmware")

    def test_rejects_empty(self):
        with pytest.raises(typer.BadParameter, match="cannot be empty"):
            _parse_backends("")
        with pytest.raises(typer.BadParameter, match="cannot be empty"):
            _parse_backends(" , , ")
