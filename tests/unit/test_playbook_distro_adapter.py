"""Role split, distro adapter contract, containerd render modes, K3s pin."""

import itertools
import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import jinja2
import pytest
import yaml
from typer.testing import CliRunner

from k7.cli.k7 import app
from k7.core.core import K7Core
from k7.core.models import OperationResult
from tests.unit.conftest import DEPLOY_DIR, install_tree_text

ROLE = DEPLOY_DIR / "roles" / "k7_node_prep"
GOLDEN = Path(__file__).parent / "fixtures" / "containerd-k3s-template"
DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
K3S = yaml.safe_load((ROLE / "vars" / "k3s.yml").read_text())
REPO_SRC = Path(__file__).resolve().parents[2] / "src" / "k7"

# (devmapper, longhorn, k7d, k7d_fc); k7d_fc implies k7d.
COMBOS = [c for c in itertools.product((0, 1), repeat=4) if not (c[3] and not c[2])]


def _render(mode: str, combo: tuple[int, ...]) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROLE / "templates"),
        trim_blocks=True,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
    )
    flags = dict(
        zip(("k7_has_devmapper", "k7_has_longhorn", "k7_has_k7d", "k7_has_k7d_fc"), map(bool, combo), strict=True)
    )
    return env.get_template(f"containerd-{mode}.toml.j2").render(**flags, **K3S)


def test_goldens_cover_every_combo():
    assert sorted(p.stem for p in GOLDEN.glob("*.tmpl")) == sorted("-".join(map(str, c)) for c in COMBOS)


@pytest.mark.parametrize("combo", COMBOS, ids=lambda c: "-".join(map(str, c)))
def test_k3s_template_is_byte_identical_to_pre_split(combo):
    golden = (GOLDEN / f"{'-'.join(map(str, combo))}.tmpl").read_text()
    assert _render("k3s-template", combo) == golden


@pytest.mark.parametrize("combo", COMBOS, ids=lambda c: "-".join(map(str, c)))
def test_drop_in_has_same_runtimes_without_go_template(combo):
    drop_in = _render("drop-in", combo)
    assert "{{" not in drop_in and "# version = 3" not in drop_in
    assert re.search(r"(?m)^version = 3$", drop_in)
    body = _render("k3s-template", combo).split('{{ template "base" . }}\n', 1)[1]
    assert drop_in.endswith(body)


def test_every_adapter_defines_exactly_the_contract_keys():
    files = {p.stem: yaml.safe_load(p.read_text()) for p in (ROLE / "vars").glob("*.yml")}
    assert set(files) == set(DEFAULTS["k7_adapter_distros"])
    for distro, keys in files.items():
        assert set(keys) == set(DEFAULTS["k7_adapter_keys"]), distro
        assert keys["k7_containerd_config_mode"] in ("k3s-template", "drop-in"), distro


def test_k3s_version_is_pinned_and_passed_to_every_installer():
    play = yaml.safe_load((DEPLOY_DIR / "k7-install-node.yaml").read_text())[0]
    assert re.fullmatch(r"v1\.\d+\.\d+\+k3s\d+", play["vars"]["k3s_version"])
    install = (DEPLOY_DIR / "roles" / "k7_k3s" / "tasks" / "install.yml").read_text()
    assert install.count("sh /tmp/k3s-install.sh") == install.count("INSTALL_K3S_VERSION='{{ k3s_version }}'") == 3


def test_no_k3s_socket_path_in_core_or_api():
    for sub in ("core", "api"):
        for f in (REPO_SRC / sub).rglob("*.py"):
            assert "/run/k3s" not in f.read_text(), f
    for name in ("deployment.yaml", "agent-daemonset.yaml"):
        assert "/run/k3s" not in (DEPLOY_DIR / "manifests" / "k7-api" / name).read_text()
    # k7-api has no host access; only the agent mounts the socket.
    text = (DEPLOY_DIR / "manifests" / "k7-api" / "agent-daemonset.yaml").read_text()
    assert "__K7_CONTAINERD_SOCKET_DIR__" in text and "__K7_CONTAINERD_SOCKET__" in text


def test_node_prep_role_never_calls_kubectl():
    for f in (ROLE / "tasks").glob("*.yml"):
        code = "\n".join(line for line in f.read_text().splitlines() if not line.lstrip().startswith("#"))
        assert "kubectl" not in code, f


def test_loopback_with_kfd_is_rejected_by_playbook():
    assert "Fail if loopback disk source is combined with kfd" in install_tree_text()


async def test_cri_sandbox_id_requires_containerd_socket(core, tmp_path):
    with patch.dict(os.environ, {}, clear=False), patch("k7.core.core.CONTAINERD_SOCKET_FILE", tmp_path / "missing"):
        os.environ.pop("K7_CONTAINERD_SOCKET", None)
        with pytest.raises(RuntimeError, match="K7_CONTAINERD_SOCKET"):
            await core._k7d_cri_sandbox_id("pod", "ns")


async def test_cri_sandbox_id_reads_socket_file_on_host(core, tmp_path):
    sock_file = tmp_path / "containerd_socket"
    sock_file.write_text("/run/containerd/containerd.sock\n")
    done = MagicMock(returncode=0, stdout="abc\n", stderr="")
    with (
        patch.dict(os.environ, {}, clear=False),
        patch("k7.core.core.CONTAINERD_SOCKET_FILE", sock_file),
        patch("k7.core.core.subprocess.run", return_value=done) as run,
    ):
        os.environ.pop("K7_CONTAINERD_SOCKET", None)
        assert await core._k7d_cri_sandbox_id("pod", "ns") == "abc"
    assert "unix:///run/containerd/containerd.sock" in run.call_args.args[0]


def test_count_playbook_tasks_follows_roles(core: K7Core):
    assert core._count_playbook_tasks((DEPLOY_DIR / "k7-install-node.yaml").read_text()) > 200


def _install(args: list[str]):
    with patch("k7.cli.k7.K7Core") as core_cls:
        install = MagicMock(return_value=OperationResult(success=True, message="ok"))
        core_cls.return_value.install_node = install
        result = CliRunner().invoke(app, ["--core", "install", "--backend", "kql", "--api-mode", "development", *args])
    return result, install


def test_cli_passes_k3s_distro_and_defaults():
    result, install = _install([])
    assert result.exit_code == 0, result.output
    extra = install.call_args.kwargs["extra_vars"]
    assert extra["k7_distro"] == "k3s"
    assert extra.get("k3s_version") is None and extra.get("k7_node_prep_only") is None


def test_cli_k3s_version_and_node_prep_only_forwarded():
    result, install = _install(["--k3s-version", "v1.35.2+k3s1", "--node-prep-only"])
    assert result.exit_code == 0, result.output
    extra = install.call_args.kwargs["extra_vars"]
    assert extra["k3s_version"] == "v1.35.2+k3s1"
    assert extra["k7_node_prep_only"] == "true"


def test_cli_rejects_malformed_k3s_version():
    result, install = _install(["--k3s-version", "latest"])
    assert result.exit_code != 0
    install.assert_not_called()


def _all_tags() -> set[str]:
    files = [DEPLOY_DIR / "k7-install-node.yaml", *sorted((DEPLOY_DIR / "roles").glob("*/tasks/*.yml"))]
    tags: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            t = node.get("tags")
            tags.update([t] if isinstance(t, str) else t or [])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for f in files:
        walk(yaml.safe_load(f.read_text()))
    return tags


def test_tags_the_docs_tell_operators_to_use_exist():
    repo = Path(__file__).resolve().parents[2]
    docs = [repo / "docs/BACKENDS.md", repo / "README.md", *sorted((repo / "docs").glob("**/*.md"))]
    used = {t for d in docs if d.exists() for t in re.findall(r"--tags[ =]([\w,-]+)", d.read_text())}
    used = {t for group in used for t in group.split(",")}
    assert used, "docs no longer mention --tags; drop this test"
    assert used <= _all_tags(), used - _all_tags()
    assert {"always", "k7d-fc", "api", "labels", "kata", "k7d"} <= _all_tags()
