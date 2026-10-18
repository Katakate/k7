"""``k7 install --dns-upstream``: forwarded as k7_dns_upstreams, refused before Ansible when invalid."""

import pytest

from tests.unit.test_cli_install_api_allowlist import _extra_vars, _install


def test_absent_flag_leaves_the_inventory_in_charge():
    result, install = _install([])
    assert result.exit_code == 0, result.output
    assert "k7_dns_upstreams" not in _extra_vars(install)


def test_repeatable_flag_is_forwarded_comma_joined():
    result, install = _install(["--dns-upstream", "1.1.1.1", "--dns-upstream", " 9.9.9.9 "])
    assert result.exit_code == 0, result.output
    assert _extra_vars(install)["k7_dns_upstreams"] == "1.1.1.1,9.9.9.9"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (["1.1.1.256"], "is not an IP address"),
        (["dns.example.com"], "is not an IP address"),
        (["2a01:4ff:ff00::add:1"], "is IPv6"),
        (["127.0.0.53"], "back at itself"),
        (["0.0.0.0"], "back at itself"),
        (["1.1.1.1", "9.9.9.9", "8.8.8.8", "8.8.4.4"], "at most 3"),
    ],
)
def test_invalid_upstreams_fail_before_ansible(values: list[str], message: str):
    args = [a for v in values for a in ("--dns-upstream", v)]
    result, install = _install(args)
    assert result.exit_code != 0
    assert message in result.output
    install.assert_not_called()
