"""Supply chain: pinned images, checksummed downloads, SHA-pinned CI."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from k7.cli.k7 import app
from k7.deploy import images
from tests.unit.conftest import DEPLOY_DIR, FAKE_API_IMAGE, FAKE_INSTALLER_IMAGE, install_tree_text

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "src/k7/deploy/manifests/k7-api"
INSTALL_TEXT = install_tree_text()
TASKS = yaml.safe_load((DEPLOY_DIR / "k7-install-node.yaml").read_text())[0]["tasks"] + [
    t for f in sorted((DEPLOY_DIR / "roles").glob("*/tasks/*.yml")) for t in yaml.safe_load(f.read_text()) or []
]
runner = CliRunner()
REAL_LOAD_LOCK = images.load_lock


def _image_lines() -> list[tuple[str, str]]:
    return [
        (f.name, m.group(1))
        for f in sorted(MANIFESTS.parent.rglob("*.yaml"))
        for m in re.finditer(r"^\s*image:\s*(\S+)", f.read_text(), re.M)
    ]


def test_every_manifest_image_is_a_placeholder_covered_by_the_lock():
    found = _image_lines()
    assert {ref for _, ref in found} == set(images.PLACEHOLDERS)
    for name, ref in found:
        assert ref in images.PLACEHOLDERS, f"{name}: image {ref!r} bypasses images.lock.yaml"
    lock = {key: FAKE_API_IMAGE for key in images.PLACEHOLDERS.values()}
    assert set(images.parse_lock(yaml.safe_dump(lock))) == set(images.PLACEHOLDERS.values())


def test_release_builds_signs_and_locks_every_image():
    job = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]["image"]
    legs = {leg["name"]: leg["dockerfile"] for leg in job["strategy"]["matrix"]["include"]}
    assert set(legs) == set(images.PLACEHOLDERS.values())
    for dockerfile in legs.values():
        froms = re.findall(r"^FROM (\S+)", (ROOT / dockerfile).read_text(), re.M)
        assert froms and all(re.search(r"@sha256:[0-9a-f]{64}$", f) for f in froms), dockerfile
    steps = json.dumps(job["steps"])
    assert "cosign sign --yes" in steps and "trivy-action" in steps and "attest-build-provenance" in steps


def test_node_installer_bundles_pin_the_locked_digest(tmp_path):
    ref = "ghcr.io/katakate/k7-node-installer@sha256:" + "b" * 64
    src = DEPLOY_DIR / "manifests/k7-node-installer"
    out = images.render_node_installer_bundles(src, tmp_path, {"k7-api": FAKE_API_IMAGE, "k7-node-installer": ref})
    kinds = {}
    for f in out:
        docs = [d for d in yaml.safe_load_all(f.read_text()) if d]
        kinds[f.name] = sorted(d["kind"] for d in docs)
        assert [
            c["image"] for d in docs if d["kind"] == "DaemonSet" for c in d["spec"]["template"]["spec"]["containers"]
        ] == [ref]
    assert kinds["k7-node-cleanup.yaml"] == ["DaemonSet"]
    assert kinds["k7-node-installer.yaml"].count("ValidatingAdmissionPolicy") == 2
    assert {"Namespace", "ConfigMap", "DaemonSet", "ClusterRole"} <= set(kinds["k7-node-installer.yaml"])
    with pytest.raises(KeyError):
        images.render_node_installer_bundles(src, tmp_path, {"k7-api": FAKE_API_IMAGE})


def test_manifests_never_hardcode_never_pull_or_local_tag():
    for f in MANIFESTS.rglob("*.yaml"):
        text = f.read_text()
        assert "imagePullPolicy: Never" not in text, f.name
        assert "k7-api:local" not in text, f.name


@pytest.mark.parametrize("api_image", ["local", FAKE_API_IMAGE])
def test_render_manifests_leaves_no_placeholder(tmp_path, api_image):
    images.render_manifests(MANIFESTS, tmp_path, api_image, "/run/k3s/containerd/containerd.sock", "development", "{}")
    rendered = "".join(f.read_text() for f in tmp_path.rglob("*.yaml"))
    assert "__K7_" not in rendered
    ref, policy = images.api_image_ref_and_policy(api_image)
    assert rendered.count(f"image: {ref}\n") == 3
    assert rendered.count(f"imagePullPolicy: {policy}\n") == 3
    if api_image != "local":
        assert "Never" not in rendered and "k7-api:local" not in rendered
    (container,) = yaml.safe_load((tmp_path / "deployment.yaml").read_text())["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert container["image"] == ref
    assert (env["K7_API_MODE"], env["K7_API_TRANSPORT"]) == ("development", "insecure-http")
    assert (
        env["K7_API_ENABLE_REMOTE_INSTALL"],
        env["K7_NODE_INSTALLER_IMAGE"],
        env["K7_NODE_INSTALLER_NAMESPACE"],
    ) == (
        "0",
        "",
        "",
    )


@pytest.mark.parametrize(
    "text", ["", "k7-api: ghcr.io/katakate/k7-api:0.4.0\n", "k7-api: ghcr.io/x@sha256:abc\n", "- a\n"]
)
def test_parse_lock_rejects_anything_not_pinned_by_digest(text):
    with pytest.raises(ValueError):
        images.parse_lock(text)


def test_resolve_api_image_without_a_packaged_lock_fails_loud(monkeypatch):
    monkeypatch.setattr(images, "load_lock", lambda: None)
    with pytest.raises(LookupError, match="--api-image local"):
        images.resolve_api_image(None)
    assert images.resolve_api_image("local") == "local"
    assert images.resolve_api_image(FAKE_API_IMAGE) == FAKE_API_IMAGE
    with pytest.raises(ValueError):
        images.resolve_api_image("ghcr.io/katakate/k7-api:latest")
    with pytest.raises(LookupError, match="--node-installer-image local"):
        images.resolve_image(images.NODE_INSTALLER_IMAGE_KEY, None)
    with pytest.raises(ValueError, match="--node-installer-image"):
        images.resolve_image(images.NODE_INSTALLER_IMAGE_KEY, "k7-node-installer:local")


def test_resolve_node_installer_image_reads_its_own_lock_entry(monkeypatch):
    assert images.resolve_image(images.NODE_INSTALLER_IMAGE_KEY, None) == FAKE_INSTALLER_IMAGE
    monkeypatch.setattr(images, "load_lock", lambda: {"k7-api": FAKE_API_IMAGE})
    with pytest.raises(LookupError, match="no 'k7-node-installer' entry"):
        images.resolve_image(images.NODE_INSTALLER_IMAGE_KEY, None)


def test_source_tree_ships_no_lock_file():
    assert not (ROOT / "src/k7/deploy" / images.LOCK_FILE).exists()
    assert "src/k7/deploy/images.lock.yaml" in (ROOT / ".gitignore").read_text()


def _install(args: list[str]) -> tuple[object, dict]:
    seen: dict = {}

    def _capture(*_a, **kw):
        seen.update(kw)
        return SimpleNamespace(success=True, message="ok", error="")

    with (
        patch("k7.cli.k7.K7Core") as core_cls,
        patch("k7.cli.k7._build_default_inventory", return_value="[k7_servers]\nhost ansible_host=1.2.3.4"),
    ):
        core_cls.return_value.install_node = _capture
        result = runner.invoke(app, ["install", "--backend", "k7d", "--api-mode", "development", *args, "host"])
    return result, seen.get("extra_vars", {})


def test_install_deploys_the_locked_digest_without_a_checkout():
    result, ev = _install([])
    assert result.exit_code == 0, result.output
    assert ev["k7_api_image"] == FAKE_API_IMAGE
    assert "k7_repo_root" not in ev
    assert (Path(ev["k7_deploy_dir"]) / "manifests/k7-api/deployment.yaml").is_file()
    assert f"Deployed k7-api image: {FAKE_API_IMAGE}" in result.output


def test_install_api_image_local_passes_the_cwd_and_warns_unsigned():
    result, ev = _install(["--api-image", "local"])
    assert result.exit_code == 0, result.output
    assert ev["k7_api_image"] == "local"
    assert ev["k7_repo_root"]
    assert "unsigned" in result.output


def test_install_without_lock_fails_before_ansible(monkeypatch):
    monkeypatch.setattr(images, "load_lock", lambda: None)
    result, ev = _install([])
    assert result.exit_code == 1
    assert "--api-image local" in result.output
    assert ev == {}
    result, ev = _install(["--no-api"])
    assert result.exit_code == 0, result.output
    assert ev["k7_api_image"] == ""


def test_install_rejects_a_tag_ref():
    result, ev = _install(["--api-image", "ghcr.io/katakate/k7-api:0.4.0"])
    assert result.exit_code != 0
    assert ev == {}


def _walk(tasks):
    for t in tasks:
        yield t
        for key in ("block", "rescue", "always"):
            yield from _walk(t.get(key, []))


def test_every_get_url_is_checksummed():
    get_urls = [t for t in _walk(TASKS) if "ansible.builtin.get_url" in t]
    assert len(get_urls) >= 8
    for t in get_urls:
        assert str(t["ansible.builtin.get_url"].get("checksum", "")).startswith("sha256:"), t["name"]


def test_every_shell_download_is_sha256_verified():
    for t in _walk(TASKS):
        body = t.get("ansible.builtin.shell")
        script = body if isinstance(body, str) else (body or {}).get("cmd", "")
        if re.search(r"\bcurl\b[^\n]*-o", script):
            assert "sha256sum -c" in script, t["name"]
        assert not re.search(r"\|\s*(sh|bash)\b", script), t["name"]


def test_k3s_is_pinned_and_installed_from_the_checksummed_script():
    assert "https://get.k3s.io" not in INSTALL_TEXT
    assert "get.docker.com" not in INSTALL_TEXT
    k3s = [t for t in TASKS if t["name"].startswith("Install K3s on")]
    assert len(k3s) == 3
    for t in k3s:
        assert "INSTALL_K3S_VERSION='{{ k3s_version }}'" in t["ansible.builtin.shell"]
        assert "sh /tmp/k3s-install.sh" in t["ansible.builtin.shell"]


def test_docker_is_only_installed_or_called_for_local_image_builds():
    for t in _walk(TASKS):
        cmd = t.get("ansible.builtin.command") or t.get("ansible.builtin.shell") or ""
        cmd = cmd if isinstance(cmd, str) else str(cmd)
        unit = str(t.get("ansible.builtin.systemd", {}).get("name", "")) + str(t.get("loop", ""))
        touches_docker = "docker " in cmd.split("#")[0] or "docker" in unit
        if touches_docker or "docker-ce=" in str(t.get("ansible.builtin.apt", "")):
            assert "k7_local_image_build" in str(t.get("when")), t["name"]


def test_bootstrap_installs_the_playbooks_pinned_docker():
    text = (ROOT / "utils/bootstrap.sh").read_text()
    assert "get.docker.com" not in text and not re.search(r"\|\s*(sh|bash)\b", text)
    v = yaml.safe_load((DEPLOY_DIR / "k7-install-node.yaml").read_text())[0]["vars"]
    for var in ("docker_apt_key_sha256", "docker_ce_version", "docker_containerd_version", "docker_buildx_version"):
        assert f'{var.upper()}="{v[var]}"' in text, var
    assert "sha256sum -c -" in text and "systemctl restart docker.socket docker.service" in text


def test_dist_upgrade_holds_the_k7_package():
    (block,) = [t for t in TASKS if t["name"] == "Upgrade all packages with the k7 package held"]
    hold, upgrade = block["block"]
    assert hold["ansible.builtin.command"] == "apt-mark hold k7" and upgrade["ansible.builtin.apt"]["upgrade"] == "dist"
    assert block["always"][0]["ansible.builtin.command"] == "apt-mark unhold k7"
    assert hold["when"] == block["always"][0]["when"] == "k7_pkg_hold | bool"
    assert block["vars"]["k7_pkg_hold"] == "{{ k7_pkg_status.stdout == 'install ok installed' }}"
    assert [t for t in _walk(TASKS) if "upgrade" in (t.get("ansible.builtin.apt") or {})] == [upgrade]


def test_pinned_docker_is_held_against_the_dist_upgrade():
    (install,) = [t for t in _walk(TASKS) if t["name"] == "Docker (local image build) — install pinned Docker"]
    (hold,) = [t for t in _walk(TASKS) if t["name"] == "Docker (local image build) — hold the pinned packages"]
    pinned = [n.split("=")[0] for n in install["ansible.builtin.apt"]["name"]]
    assert hold["loop"] == pinned and hold["ansible.builtin.dpkg_selections"]["selection"] == "hold"
    assert install["ansible.builtin.apt"]["allow_change_held_packages"] is True
    (reset,) = [t for t in _walk(TASKS) if t["name"].startswith("Docker (local image build) — reset the start limit")]
    assert install["register"] == "k7_docker_pkgs" and "k7_docker_pkgs is changed" in reset["when"]


def test_local_docker_restarts_once_per_run():
    """docker.service StartLimitBurst=3. Restarting docker.socket restarts the
    service too, so a second restart in the same run makes the next install
    within 60s hit the limit."""
    restarts = []
    for t in _walk(TASKS):
        unit = t.get("ansible.builtin.systemd") or {}
        if not isinstance(unit, dict) or unit.get("state") != "restarted":
            continue
        names = [unit["name"]] if unit.get("name") else []
        names += [str(item) for item in t.get("loop") or []]
        if any(name in ("docker", "docker.service", "docker.socket") for name in names):
            restarts.append((t["name"], names))
    assert restarts == [
        (
            "Docker (local image build) — restart once to recreate iptables chains (after flushing nft)",
            ["docker.service"],
        )
    ]
    (clear,) = [t for t in _walk(TASKS) if "failed docker.socket" in t["name"]]
    assert clear["ansible.builtin.command"] == "systemctl reset-failed docker.socket"
    assert "k7_docker_pkgs is changed" not in clear["when"]
    (socket,) = [t for t in _walk(TASKS) if t["name"].startswith("Docker (local image build) — enable docker.socket")]
    assert socket["ansible.builtin.systemd"]["name"] == "docker.socket"
    assert socket["ansible.builtin.systemd"]["state"] == "started"
    assert socket["ansible.builtin.systemd"]["enabled"] is True


def test_playbook_does_not_need_a_checkout_outside_local_mode():
    for t in _walk(TASKS):
        if "k7_repo_root" in yaml.safe_dump(t):
            assert "k7_local_image_build" in str(t.get("when")), t["name"]


def test_onefile_binary_embeds_every_py_the_playbook_copies():
    scripts = re.findall(r"\{\{ k7_deploy_dir \}\}/(\S+\.py)\"", INSTALL_TEXT)
    assert scripts
    dockerfile = (ROOT / "src/k7/cli/Dockerfile.cli").read_text()
    for s in scripts:
        assert f"--include-data-files=k7/deploy/{s}=k7/deploy/{s}" in dockerfile, s


def test_every_workflow_action_is_pinned_by_sha():
    uses = [
        (wf.name, m.group(1))
        for wf in (ROOT / ".github/workflows").glob("*.yml")
        for m in re.finditer(r"uses:\s*(\S+)", wf.read_text())
    ]
    assert uses
    for name, ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), f"{name}: {ref}"


def test_each_action_has_one_pin_across_workflows():
    pins: dict[str, set[str]] = {}
    for wf in (ROOT / ".github/workflows").glob("*.yml"):
        for m in re.finditer(r"uses:\s*([\w.-]+/[\w./-]+)@([0-9a-f]{40})", wf.read_text()):
            pins.setdefault(m.group(1), set()).add(m.group(2))
    assert pins
    assert {action: shas for action, shas in pins.items() if len(shas) > 1} == {}


def test_every_dockerfile_base_is_digest_pinned_and_tracked_by_dependabot():
    """Dependabot reads only literal FROM lines (an ARG-held base is invisible to it)."""
    files = sorted(p for p in (ROOT / "src").rglob("Dockerfile*") if p.is_file())
    assert files
    for f in files:
        text = f.read_text()
        stages = set(re.findall(r"^FROM \S+ AS (\S+)", text, re.M | re.I))
        refs = re.findall(r"^FROM (?:--platform=\S+ )?(\S+)", text, re.M)
        assert refs, f
        for ref in refs:
            assert ref in stages or re.search(r"^[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}$", ref), f"{f}: {ref}"
    updates = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())["updates"]
    tracked = {d for u in updates if u["package-ecosystem"] == "docker" for d in u.get("directories", [])}
    tracked |= {u["directory"] for u in updates if u["package-ecosystem"] == "docker" and "directory" in u}
    assert {f"/{f.parent.relative_to(ROOT)}" for f in files} <= tracked


def test_workflows_default_to_read_only_token():
    for wf in (ROOT / ".github/workflows").glob("*.yml"):
        assert yaml.safe_load(wf.read_text())["permissions"] == {"contents": "read"}, wf.name


def test_crictl_matches_the_k3s_minor_and_is_checksummed():
    text = (ROOT / "src/k7/api/Dockerfile.api").read_text()
    version = re.search(r"^ARG CRICTL_VERSION=(v\d+\.\d+\.\d+)\s*$", text, re.M)
    digest = re.search(r"^ARG CRICTL_SHA256=([0-9a-f]{64})\s*$", text, re.M)
    assert version and digest
    play = yaml.safe_load((ROOT / "src/k7/deploy/k7-install-node.yaml").read_text())
    k3s = play[0]["vars"]["k3s_version"]
    assert version.group(1).rsplit(".", 1)[0] == "v" + ".".join(k3s.removeprefix("v").split("+", 1)[0].split(".")[:2])


def test_dockerfile_base_images_are_pinned_by_digest():
    froms = re.findall(r"^FROM\s+(\S+)", (ROOT / "src/k7/api/Dockerfile.api").read_text(), re.M)
    assert froms
    for ref in froms:
        assert re.search(r"@sha256:[0-9a-f]{64}$", ref), ref


def test_api_image_installs_the_locked_dependency_set():
    text = (ROOT / "src/k7/api/Dockerfile.api").read_text()
    assert "requirements.txt" not in text
    assert not (ROOT / "src/k7/api/requirements.txt").exists()
    assert "uv.lock" in text
    assert text.count("uv sync --locked --no-dev") == 2
    assert "--no-editable" in text
    # Same uv release the workflows pin, and not the floating python tag.
    assert "ghcr.io/astral-sh/uv:0.12.23@" in text
    assert "uv:python" not in text


def test_release_workflow_builds_the_api_dockerfile_from_the_repo_root():
    job = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]["image"]
    step = next(s for s in job["steps"] if s.get("id") == "build")
    assert step["with"]["context"] == "."
    assert step["with"]["file"] == "${{ matrix.dockerfile }}"
    legs = {leg["name"]: leg["dockerfile"] for leg in job["strategy"]["matrix"]["include"]}
    assert legs["k7-api"] == "src/k7/api/Dockerfile.api"


def test_local_image_builds_use_the_release_dockerfiles_from_the_repo_root():
    builds = [t for t in _walk(TASKS) if "docker build" in str(t.get("ansible.builtin.command", ""))]
    assert len(builds) == 1
    assert builds[0]["ansible.builtin.command"] == (
        "docker build --network=host -f {{ item.dockerfile }} -t {{ item.tag }} ."
    )
    assert builds[0]["loop"] == "{{ k7_local_images }}" and "k7_local_image_build" in builds[0]["when"]
    (fact,) = [t for t in TASKS if t["name"] == "Set the local image builds"]
    expr = fact["ansible.builtin.set_fact"]["k7_local_images"]
    job = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]["image"]
    legs = {leg["name"]: leg["dockerfile"] for leg in job["strategy"]["matrix"]["include"]}
    for key, tag in (
        (images.API_IMAGE_KEY, images.LOCAL_API_IMAGE_REF),
        (images.NODE_INSTALLER_IMAGE_KEY, images.LOCAL_NODE_INSTALLER_IMAGE_REF),
    ):
        assert f"{{'tag': '{tag}', 'dockerfile': '{legs[key]}'}}" in expr, key
    # Only what is asked for is built: the installer image only with remote install on.
    assert "k7_api_image_local | bool" in expr and "k7_node_installer_local | bool" in expr
    (refs,) = [t for t in TASKS if t["name"] == "Set k7-api image ref and pull policy"]
    assert "k7_api_remote_install | bool and" in refs["ansible.builtin.set_fact"]["k7_node_installer_local"]


def test_local_image_tarballs_are_removed_everywhere():
    local = yaml.safe_load((DEPLOY_DIR / "roles/k7_cluster/tasks/local_images.yml").read_text())
    tars = {t["name"]: t for t in local if ".tar" in yaml.safe_dump(t)}
    removed = [t for t in tars.values() if (t.get("ansible.builtin.file") or {}).get("state") == "absent"]
    assert {t.get("delegate_to") for t in removed} == {None, "localhost"}


def test_single_node_never_round_trips_the_image_tarballs_through_the_controller():
    local = yaml.safe_load((DEPLOY_DIR / "roles/k7_cluster/tasks/local_images.yml").read_text())
    controller = [t for t in local if "ansible.builtin.fetch" in t or t.get("delegate_to") == "localhost"]
    assert len(controller) == 2
    assert all(t["when"].endswith("and ansible_play_hosts_all | length > 1") for t in controller)
    (copy,) = [t for t in local if "ansible.builtin.copy" in t]
    assert copy["when"].endswith("and inventory_hostname != k7_first_master")


def _packaged_lock(monkeypatch, tmp_path: Path, text: str | None) -> None:
    if text is not None:
        (tmp_path / images.LOCK_FILE).write_text(text)
    monkeypatch.setattr(images.resources, "files", lambda pkg: tmp_path)
    monkeypatch.setattr(images, "load_lock", REAL_LOAD_LOCK)


def test_packaged_lock_is_parsed_and_resolves_the_api_image(monkeypatch, tmp_path):
    ref = "ghcr.io/katakate/k7-node-installer@sha256:" + "c" * 64
    _packaged_lock(monkeypatch, tmp_path, yaml.safe_dump({"k7-api": FAKE_API_IMAGE, "k7-node-installer": ref}))
    assert images.load_lock() == {"k7-api": FAKE_API_IMAGE, "k7-node-installer": ref}
    assert images.resolve_api_image(None) == FAKE_API_IMAGE


def test_absent_packaged_lock_loads_as_none(monkeypatch, tmp_path):
    _packaged_lock(monkeypatch, tmp_path, None)
    assert images.load_lock() is None


def test_lock_without_the_api_entry_fails_loud(monkeypatch, tmp_path):
    ref = "ghcr.io/katakate/k7-node-installer@sha256:" + "c" * 64
    _packaged_lock(monkeypatch, tmp_path, yaml.safe_dump({"k7-node-installer": ref}))
    with pytest.raises(LookupError, match="no 'k7-api' entry"):
        images.resolve_api_image(None)


def test_render_manifests_refuses_an_unknown_placeholder(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "extra.yaml").write_text("image: __K7_API_IMAGE__\nfoo: __K7_SOMETHING_NEW__\n")
    with pytest.raises(ValueError, match="extra.yaml: unrendered __K7_ placeholder"):
        images.render_manifests(
            src, tmp_path / "dst", FAKE_API_IMAGE, "/run/containerd/containerd.sock", "development", "{}"
        )


def test_installer_bundle_without_the_installer_image_is_refused(tmp_path):
    src = tmp_path / "src"
    (src / "cleanup").mkdir(parents=True)
    (src / "a.yaml").write_text("kind: ConfigMap\n")
    (src / "cleanup" / "b.yaml").write_text("kind: DaemonSet\n")
    ref = "ghcr.io/katakate/k7-node-installer@sha256:" + "c" * 64
    with pytest.raises(ValueError, match="no installer image"):
        images.render_node_installer_bundles(src, tmp_path, {"k7-node-installer": ref})


def test_every_trivy_waiver_has_a_reason_and_review_date():
    lines = (ROOT / ".trivyignore").read_text().splitlines()
    for i, line in enumerate(lines):
        if line.strip() and not line.startswith("#"):
            above = lines[i - 1] if i else ""
            assert above.startswith("#") and re.search(r"Review \d{4}-\d{2}-\d{2}", above), line


def test_dependabot_tracks_actions_and_the_api_base_image():
    updates = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())["updates"]
    dirs = {(u["package-ecosystem"], d) for u in updates for d in u.get("directories") or [u["directory"]]}
    assert ("github-actions", "/") in dirs
    assert ("docker", "/src/k7/api") in dirs
    for _, d in dirs:
        assert (ROOT / d.lstrip("/")).is_dir(), d


def test_publish_ships_ci_but_not_the_hardware_workflow():
    script = ROOT / "utils/publish-to-upstream.sh"
    if not script.exists():
        pytest.skip("release tooling is not part of this tree")
    text = script.read_text()
    curated = re.search(r"for rel in \\\n(.*?)\ndo", text, re.S)
    assert curated and {".github", ".trivyignore"} <= set(curated.group(1).replace("\\", " ").split())
    assert 'rm -f "$STAGING/.github/workflows/hardware-e2e.yml"' in text
    gate = re.search(r"^for bad in (.*); do$", text, re.M)
    assert gate and ".github/workflows/hardware-e2e.yml" in gate.group(1).split()
