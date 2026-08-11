"""Unit tests for pure-logic methods on K7Core (no k8s required)."""

import pytest

from k7.core.core import K7Core, _classify_egress_entries

# --- _memory_limit_to_mib ---


class TestMemoryLimitToMib:
    def test_ki(self, core: K7Core):
        assert core._memory_limit_to_mib("1024Ki") == 1

    def test_ki_rounds_up(self, core: K7Core):
        assert core._memory_limit_to_mib("1Ki") == 1  # 1/1024 -> ceil -> 1

    def test_mi(self, core: K7Core):
        assert core._memory_limit_to_mib("512Mi") == 512

    def test_gi(self, core: K7Core):
        assert core._memory_limit_to_mib("2Gi") == 2048

    def test_ti(self, core: K7Core):
        assert core._memory_limit_to_mib("1Ti") == 1024 * 1024

    def test_fractional_gi(self, core: K7Core):
        assert core._memory_limit_to_mib("1.5Gi") == 1536

    def test_empty_string_raises(self, core: K7Core):
        with pytest.raises(ValueError, match="non-empty string"):
            core._memory_limit_to_mib("")

    def test_whitespace_only_raises(self, core: K7Core):
        with pytest.raises(ValueError, match="non-empty string"):
            core._memory_limit_to_mib("   ")

    def test_unsupported_format_raises(self, core: K7Core):
        with pytest.raises(ValueError, match="Unsupported memory format"):
            core._memory_limit_to_mib("500m")

    def test_plain_number_raises(self, core: K7Core):
        with pytest.raises(ValueError, match="Unsupported memory format"):
            core._memory_limit_to_mib("1024")

    def test_non_string_raises(self, core: K7Core):
        with pytest.raises(ValueError, match="non-empty string"):
            core._memory_limit_to_mib(None)  # type: ignore[arg-type]


# --- _parse_resource_value ---


class TestParseResourceValue:
    def test_millicores(self, core: K7Core):
        assert core._parse_resource_value("500m") == 500

    def test_gi(self, core: K7Core):
        assert core._parse_resource_value("4Gi") == 4096

    def test_mi(self, core: K7Core):
        assert core._parse_resource_value("256Mi") == 256

    def test_ki(self, core: K7Core):
        assert core._parse_resource_value("1024Ki") == 1

    def test_plain_integer(self, core: K7Core):
        assert core._parse_resource_value("2") == 2

    def test_empty_string(self, core: K7Core):
        assert core._parse_resource_value("") == 0

    def test_invalid_string(self, core: K7Core):
        assert core._parse_resource_value("abc") == 0

    def test_whitespace_stripped(self, core: K7Core):
        assert core._parse_resource_value("  500m  ") == 500


# --- _validate_limits ---


class TestValidateLimits:
    def test_valid_limits(self, core: K7Core):
        assert core._validate_limits({"cpu": "500m", "memory": "1Gi"}) is True

    def test_empty_dict_valid(self, core: K7Core):
        assert core._validate_limits({}) is True

    def test_none_valid(self, core: K7Core):
        assert core._validate_limits(None) is True  # type: ignore[arg-type]

    def test_zero_cpu_invalid(self, core: K7Core):
        assert core._validate_limits({"cpu": "0m"}) is False

    def test_invalid_value_invalid(self, core: K7Core):
        assert core._validate_limits({"cpu": "abc"}) is False

    def test_unknown_key_ignored(self, core: K7Core):
        assert core._validate_limits({"custom-thing": "abc"}) is True


# --- _root_pvc_name ---


class TestRootPvcName:
    def test_naming_convention(self, core: K7Core):
        assert core._root_pvc_name("my-sandbox") == "my-sandbox-root-lh"

    def test_different_name(self, core: K7Core):
        assert core._root_pvc_name("test-123") == "test-123-root-lh"


# --- _count_playbook_tasks ---


class TestCountPlaybookTasks:
    def test_valid_playbook(self, core: K7Core):
        content = """
- name: Test playbook
  hosts: all
  tasks:
    - name: Task 1
      command: echo 1
    - name: Task 2
      command: echo 2
    - name: Task 3
      command: echo 3
"""
        assert core._count_playbook_tasks(content) == 3

    def test_empty_string(self, core: K7Core):
        assert core._count_playbook_tasks("") == 0

    def test_malformed_yaml(self, core: K7Core):
        assert core._count_playbook_tasks("{{invalid yaml: [") == 0

    def test_no_tasks_key(self, core: K7Core):
        content = """
- name: No tasks
  hosts: all
"""
        assert core._count_playbook_tasks(content) == 0

    def test_not_a_list(self, core: K7Core):
        assert core._count_playbook_tasks("key: value") == 0


# --- _normalize_image_argv ---


class TestNormalizeImageArgv:
    def test_none_returns_empty(self, core: K7Core):
        assert core._normalize_image_argv(None) == []

    def test_string_returns_list(self, core: K7Core):
        assert core._normalize_image_argv("/bin/sh") == ["/bin/sh"]

    def test_empty_string_returns_empty(self, core: K7Core):
        assert core._normalize_image_argv("") == []

    def test_list_returns_stringified(self, core: K7Core):
        assert core._normalize_image_argv(["/bin/sh", "-c"]) == ["/bin/sh", "-c"]

    def test_list_filters_none(self, core: K7Core):
        assert core._normalize_image_argv(["/bin/sh", None, "-c"]) == ["/bin/sh", "-c"]

    def test_list_with_ints(self, core: K7Core):
        assert core._normalize_image_argv([1, 2]) == ["1", "2"]

    def test_other_type_returns_empty(self, core: K7Core):
        assert core._normalize_image_argv(42) == []


# --- _extract_entrypoint_cmd ---


class TestExtractEntrypointCmd:
    def test_nested_info_config(self, core: K7Core):
        data = {"info": {"config": {"Entrypoint": ["/init"], "Cmd": ["run"]}}}
        assert core._extract_entrypoint_cmd(data) == (["/init"], ["run"])

    def test_flat_config(self, core: K7Core):
        data = {"config": {"Entrypoint": ["/start"], "Cmd": ["serve"]}}
        assert core._extract_entrypoint_cmd(data) == (["/start"], ["serve"])

    def test_info_config_takes_priority(self, core: K7Core):
        data = {
            "info": {"config": {"Entrypoint": ["/first"]}},
            "config": {"Entrypoint": ["/second"]},
        }
        ep, cmd = core._extract_entrypoint_cmd(data)
        assert ep == ["/first"]

    def test_missing_keys_return_empty(self, core: K7Core):
        assert core._extract_entrypoint_cmd({}) == ([], [])

    def test_non_dict_returns_empty(self, core: K7Core):
        assert core._extract_entrypoint_cmd("not a dict") == ([], [])

    def test_none_entrypoint(self, core: K7Core):
        data = {"config": {"Entrypoint": None, "Cmd": ["/bin/sh"]}}
        assert core._extract_entrypoint_cmd(data) == ([], ["/bin/sh"])


# --- _compute_image_argv ---


class TestComputeImageArgv:
    def test_no_overrides(self, core: K7Core):
        result = core._compute_image_argv(["/ep"], ["arg"], None, None)
        assert result == ["/ep", "arg"]

    def test_override_entrypoint_only(self, core: K7Core):
        result = core._compute_image_argv(["/ep"], ["img-cmd"], ["/new-ep"], None)
        assert result == ["/new-ep", "img-cmd"]

    def test_override_cmd_only(self, core: K7Core):
        result = core._compute_image_argv(["/ep"], ["img-cmd"], None, ["new-cmd"])
        assert result == ["/ep", "new-cmd"]

    def test_both_overrides(self, core: K7Core):
        result = core._compute_image_argv(["/ep"], ["img-cmd"], ["/x"], ["y"])
        assert result == ["/x", "y"]

    def test_empty_image_defaults(self, core: K7Core):
        result = core._compute_image_argv([], [], None, None)
        assert result == []


# --- _classify_egress_entries ---


class TestClassifyEgressEntries:
    def test_empty_list(self):
        assert _classify_egress_entries([]) == ([], [])

    def test_only_cidrs(self):
        cidrs, fqdns = _classify_egress_entries(["10.0.0.0/8", "192.168.1.0/24"])
        assert cidrs == ["10.0.0.0/8", "192.168.1.0/24"]
        assert fqdns == []

    def test_bare_ipv4_gets_slash_32(self):
        cidrs, fqdns = _classify_egress_entries(["1.2.3.4"])
        assert cidrs == ["1.2.3.4/32"]
        assert fqdns == []

    def test_bare_ipv6_gets_slash_128(self):
        cidrs, fqdns = _classify_egress_entries(["2001:db8::1"])
        assert cidrs == ["2001:db8::1/128"]
        assert fqdns == []

    def test_only_fqdns(self):
        cidrs, fqdns = _classify_egress_entries(["api.openai.com", "huggingface.co"])
        assert cidrs == []
        assert fqdns == ["api.openai.com", "huggingface.co"]

    def test_mixed(self):
        cidrs, fqdns = _classify_egress_entries(["10.0.0.0/8", "api.openai.com", "1.2.3.4", "*.huggingface.co"])
        assert cidrs == ["10.0.0.0/8", "1.2.3.4/32"]
        assert fqdns == ["api.openai.com", "*.huggingface.co"]

    def test_strips_whitespace_and_ignores_empty(self):
        cidrs, fqdns = _classify_egress_entries(["  ", "  api.openai.com  "])
        assert cidrs == []
        assert fqdns == ["api.openai.com"]

    def test_non_strict_cidr_is_normalized(self):
        cidrs, fqdns = _classify_egress_entries(["10.0.0.5/8"])
        assert cidrs == ["10.0.0.0/8"]
        assert fqdns == []
