"""Unit tests: k7 user-facing Docker pins match the pinned k7d snapshot."""

from pathlib import Path

import pytest

from k7.core.docker import (
    k7d_supports_docker,
    parse_docker_disk,
    pins_from_env_text,
    user_facing_pins,
)

_SNAPSHOT = Path(__file__).parent / "fixtures" / "k7d-guest-docker-pins.env"


def test_user_facing_pins_match_pinned_k7d_snapshot():
    """Fail loud if src/k7/core/docker.py drifts from k7d guest/docker/pins.env."""
    snapshot = pins_from_env_text(_SNAPSHOT.read_text(encoding="utf-8"))
    user = user_facing_pins()
    missing = [k for k in user if k not in snapshot]
    assert not missing, f"snapshot missing pins {missing}"
    drifted = {k: (user[k], snapshot[k]) for k in user if snapshot[k] != user[k]}
    assert not drifted, f"k7 docker.py pins drifted from pinned k7d pins.env: {drifted}"


def test_parse_docker_disk_accepts_gi():
    assert parse_docker_disk("40Gi") == "40Gi"
    assert parse_docker_disk(" 20Gi ") == "20Gi"


def test_parse_docker_disk_rejects_garbage():
    with pytest.raises(ValueError, match="invalid"):
        parse_docker_disk("lots")


def test_k7d_supports_docker_old_recorded_version(tmp_path: Path):
    version_file = tmp_path / "k7d_version"
    version_file.write_text("0.2.1\n")
    assert k7d_supports_docker(str(version_file)) is False


def test_k7d_supports_docker_recorded_06_without_payload(tmp_path: Path):
    """k7-api sees /etc/k7/k7d_version but not the host dockerd payload."""
    version_file = tmp_path / "k7d_version"
    version_file.write_text("0.6.0\n")
    assert k7d_supports_docker(str(version_file)) is True


def test_k7d_supports_docker_missing_version_falls_back_to_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    missing = str(tmp_path / "no-such-version")
    monkeypatch.setattr("k7.core.docker.k7d_docker_payload_present", lambda: False)
    assert k7d_supports_docker(missing) is False
    monkeypatch.setattr("k7.core.docker.k7d_docker_payload_present", lambda: True)
    assert k7d_supports_docker(missing) is True
