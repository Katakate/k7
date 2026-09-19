"""Playbook must fail closed on omitted k7_backends; none is the empty set."""

from pathlib import Path

PLAYBOOK = Path(__file__).resolve().parents[2] / "src/k7/deploy/k7-install-node.yaml"
EXAMPLE = Path(__file__).resolve().parents[2] / "src/k7/deploy/inventory.ini.example"


def test_playbook_has_no_backend_default():
    text = PLAYBOOK.read_text()
    assert "k7_backend_default" not in text
    assert "empty/omitted is not" in text
    assert "k7_backends=none" in text or "or none" in text
    assert "[] if (k7_backends_raw | map('lower') | list == ['none'])" in text
    assert "content: \"{{ k7_backends_list[0] if k7_backends_list | length > 0 else 'none' }}\"" in text


def test_inventory_example_documents_none_on_servers():
    text = EXAMPLE.read_text()
    assert "k7_backends=none" in text
    assert "[k7_servers:vars]" in text
