from pathlib import Path

import pytest
import yaml

from k7.core.models import ExecResult, OperationResult, SandboxConfig, SandboxInfo


class TestSandboxConfigFromDict:
    def test_minimal_fields(self):
        cfg = SandboxConfig.from_dict({"name": "test", "image": "alpine:latest"})
        assert cfg.name == "test"
        assert cfg.image == "alpine:latest"
        assert cfg.namespace == "default"
        assert cfg.limits == {}

    def test_all_fields(self):
        data = {
            "name": "full",
            "image": "ubuntu:22.04",
            "namespace": "prod",
            "runtime_class_name": "kata-qemu",
            "root_disk_size": "20Gi",
            "backend": "kata-qemu-longhorn",
            "env_file": ".env",
            "egress_whitelist": ["10.0.0.0/8"],
            "limits": {"cpu": "2", "memory": "4Gi"},
            "before_script": "apt update",
            "entrypoint": ["/bin/sh"],
            "cmd": ["-c", "echo hi"],
            "pod_non_root": True,
            "container_non_root": True,
            "cap_drop": ["ALL"],
            "cap_add": ["NET_ADMIN"],
        }
        cfg = SandboxConfig.from_dict(data)
        assert cfg.backend == "kata-qemu-longhorn"
        assert cfg.egress_whitelist == ["10.0.0.0/8"]
        assert cfg.limits == {"cpu": "2", "memory": "4Gi"}
        assert cfg.pod_non_root is True
        assert cfg.cap_add == ["NET_ADMIN"]

    def test_ignores_unknown_keys(self):
        cfg = SandboxConfig.from_dict({"name": "x", "image": "img", "unknown_field": 42, "another": "val"})
        assert cfg.name == "x"
        assert not hasattr(cfg, "unknown_field")

    def test_none_input(self):
        with pytest.raises(TypeError):
            SandboxConfig.from_dict(None)

    def test_limits_default_to_empty_dict(self):
        cfg = SandboxConfig(name="t", image="i", limits=None)
        assert cfg.limits == {}


class TestSandboxConfigFromYaml:
    def test_round_trip_via_file(self, tmp_path: Path):
        data = {"name": "yaml-test", "image": "alpine:latest", "namespace": "ci"}
        yaml_file = tmp_path / "sandbox.yaml"
        yaml_file.write_text(yaml.dump(data))

        cfg = SandboxConfig.from_yaml(str(yaml_file))
        assert cfg.name == "yaml-test"
        assert cfg.namespace == "ci"

    def test_missing_file(self):
        with pytest.raises(FileNotFoundError):
            SandboxConfig.from_yaml("/nonexistent/path.yaml")


class TestSandboxConfigToDict:
    def test_round_trip(self):
        original = SandboxConfig(
            name="rt",
            image="img",
            namespace="ns",
            limits={"cpu": "1"},
            before_script="echo hi",
        )
        d = original.to_dict()
        assert d["name"] == "rt"
        assert d["limits"] == {"cpu": "1"}
        assert isinstance(d, dict)

    def test_from_dict_to_dict_preserves_data(self):
        data = {"name": "a", "image": "b", "limits": {"memory": "2Gi"}}
        cfg = SandboxConfig.from_dict(data)
        result = cfg.to_dict()
        assert result["name"] == "a"
        assert result["limits"] == {"memory": "2Gi"}


class TestOtherModels:
    def test_sandbox_info_to_dict(self):
        info = SandboxInfo(
            name="s1",
            namespace="default",
            status="Running",
            ready="1/1",
            restarts=0,
            age="5m",
            image="alpine",
            backend="kata-firecracker-devmapper",
        )
        d = info.to_dict()
        assert d["name"] == "s1"
        assert d["backend"] == "kata-firecracker-devmapper"

    def test_exec_result_to_dict(self):
        r = ExecResult(exit_code=0, stdout="hello", stderr="", duration_ms=123)
        d = r.to_dict()
        assert d["exit_code"] == 0
        assert d["stdout"] == "hello"

    def test_operation_result_to_dict(self):
        r = OperationResult(success=True, message="done", data={"key": "val"})
        d = r.to_dict()
        assert d["success"] is True
        assert d["data"] == {"key": "val"}
