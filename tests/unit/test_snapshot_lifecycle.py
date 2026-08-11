"""Unit tests for the Spec 10e snapshot lifecycle (kind inference + gc filters).

These exercise ``K7Core`` directly with mocked Kubernetes custom-objects API
calls, so no cluster is required. They cover:

- ``_infer_snapshot_kind``: annotation takes precedence over name pattern,
  with a graceful fallback for snapshots created before the spec landed.
- ``_snapshot_info_from_object``: end-to-end conversion of a raw K8s dict
  to a typed ``SnapshotInfo``.
- ``gc_snapshots``: dry-run lists without deleting, respects
  ``keep_fork_for``, and leaves pause / named snapshots alone.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from k7.core.core import K7Core
from k7.core.models import (
    SNAPSHOT_KIND_FORK,
    SNAPSHOT_KIND_NAMED,
    SNAPSHOT_KIND_PAUSE,
)


def _make_snap(
    name: str,
    *,
    annotations: dict | None = None,
    namespace: str = "default",
    age_minutes: int = 0,
    ready: bool = True,
    source_pvc: str | None = None,
) -> dict:
    created = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    spec_source_pvc = source_pvc or f"{name.rsplit('-', 1)[0]}-root-lh"
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": annotations or {},
            "creationTimestamp": created,
        },
        "spec": {
            "volumeSnapshotClassName": "longhorn",
            "source": {"persistentVolumeClaimName": spec_source_pvc},
        },
        "status": {"readyToUse": ready, "restoreSize": "10Gi"},
    }


# --- _infer_snapshot_kind --------------------------------------------------


class TestInferSnapshotKind:
    def test_annotation_wins_over_name(self, core: K7Core):
        # Name looks like a fork snapshot, but annotation says "named".
        assert (
            core._infer_snapshot_kind("demo-fork-1700000000", {"k7.io/kind": SNAPSHOT_KIND_NAMED})
            == SNAPSHOT_KIND_NAMED
        )

    def test_pause_annotation(self, core: K7Core):
        assert core._infer_snapshot_kind("whatever", {"k7.io/kind": SNAPSHOT_KIND_PAUSE}) == SNAPSHOT_KIND_PAUSE

    def test_fallback_paused_name(self, core: K7Core):
        # Pre-spec snapshot: no annotation, only the canonical pause name.
        assert core._infer_snapshot_kind("demo-paused-1700000000", None) == SNAPSHOT_KIND_PAUSE
        assert core._infer_snapshot_kind("demo-paused-1700000000", {}) == SNAPSHOT_KIND_PAUSE

    def test_fallback_fork_name(self, core: K7Core):
        assert core._infer_snapshot_kind("demo-fork-1700000000", None) == SNAPSHOT_KIND_FORK

    def test_fallback_named_when_pattern_unknown(self, core: K7Core):
        assert core._infer_snapshot_kind("exp-baseline", None) == SNAPSHOT_KIND_NAMED

    def test_unknown_annotation_falls_back_to_name(self, core: K7Core):
        # Garbage in the annotation must not be trusted blindly.
        assert core._infer_snapshot_kind("demo-fork-1700000000", {"k7.io/kind": "bogus"}) == SNAPSHOT_KIND_FORK


# --- _snapshot_info_from_object --------------------------------------------


class TestSnapshotInfoFromObject:
    def test_full_roundtrip(self, core: K7Core):
        obj = _make_snap(
            "demo-paused-1700000000",
            annotations={
                "k7.io/kind": SNAPSHOT_KIND_PAUSE,
                "k7.io/source-sandbox": "demo",
            },
        )
        info = core._snapshot_info_from_object(obj)
        assert info.name == "demo-paused-1700000000"
        assert info.namespace == "default"
        assert info.kind == SNAPSHOT_KIND_PAUSE
        assert info.source_sandbox == "demo"
        assert info.source_pvc == "demo-paused-root-lh" or info.source_pvc.endswith("-root-lh")
        assert info.ready_to_use is True
        assert info.snapshot_class == "longhorn"


# --- gc_snapshots -----------------------------------------------------------


def _patch_list(core: K7Core, items: list[dict]) -> None:
    """Patch the dynamic K8s client so ``list_snapshots`` returns ``items``."""
    mock_custom = AsyncMock()
    mock_custom.list_namespaced_custom_object.return_value = {"items": items}
    mock_custom.list_cluster_custom_object.return_value = {"items": items}
    core._custom_objects_client = mock_custom
    core._config_loaded = True


class TestGcSnapshots:
    async def test_dry_run_lists_but_does_not_delete(self, core: K7Core):
        items = [
            _make_snap(
                "demo-fork-1700000000",
                annotations={"k7.io/kind": SNAPSHOT_KIND_FORK, "k7.io/source-sandbox": "demo"},
                age_minutes=120,
            ),
        ]
        _patch_list(core, items)

        with patch.object(core, "delete_snapshot", new_callable=AsyncMock) as del_mock:
            result = await core.gc_snapshots(dry_run=True)

        assert result.success is True
        assert len(result.data) == 1
        assert result.data[0]["deleted"] is False
        assert "would delete" in result.message
        del_mock.assert_not_called()

    async def test_respects_keep_fork_for(self, core: K7Core):
        # 5-minute-old fork snapshot + keep_fork_for=10m → must NOT be deleted.
        items = [
            _make_snap(
                "demo-fork-1700000000",
                annotations={"k7.io/kind": SNAPSHOT_KIND_FORK, "k7.io/source-sandbox": "demo"},
                age_minutes=5,
            ),
        ]
        _patch_list(core, items)

        with patch.object(core, "delete_snapshot", new_callable=AsyncMock) as del_mock:
            result = await core.gc_snapshots(keep_fork_for=timedelta(minutes=10))

        assert result.success is True
        assert result.data == []
        del_mock.assert_not_called()

    async def test_does_not_touch_paused_or_named_snapshots(self, core: K7Core):
        # Three snapshots, all old enough. Only the fork one should be touched.
        items = [
            _make_snap(
                "demo-paused-1700000000",
                annotations={"k7.io/kind": SNAPSHOT_KIND_PAUSE, "k7.io/source-sandbox": "demo"},
                age_minutes=120,
            ),
            _make_snap(
                "exp-baseline",
                annotations={"k7.io/kind": SNAPSHOT_KIND_NAMED, "k7.io/source-sandbox": "demo"},
                age_minutes=120,
                source_pvc="demo-root-lh",
            ),
            _make_snap(
                "demo-fork-1700000000",
                annotations={"k7.io/kind": SNAPSHOT_KIND_FORK, "k7.io/source-sandbox": "demo"},
                age_minutes=120,
            ),
        ]
        _patch_list(core, items)

        with patch.object(
            core,
            "delete_snapshot",
            new_callable=AsyncMock,
            return_value=type("R", (), {"success": True, "error": ""})(),
        ) as del_mock:
            result = await core.gc_snapshots()

        assert result.success is True
        # Only the fork snapshot is in the record.
        assert [r["name"] for r in result.data] == ["demo-fork-1700000000"]
        del_mock.assert_called_once()
        # Verify the call was scoped to the fork snapshot, not any other.
        args, kwargs = del_mock.call_args
        called_name = args[0] if args else kwargs.get("name")
        assert called_name == "demo-fork-1700000000"

    async def test_records_delete_failure(self, core: K7Core):
        items = [
            _make_snap(
                "demo-fork-1700000000",
                annotations={"k7.io/kind": SNAPSHOT_KIND_FORK, "k7.io/source-sandbox": "demo"},
                age_minutes=120,
            ),
        ]
        _patch_list(core, items)

        with patch.object(
            core,
            "delete_snapshot",
            new_callable=AsyncMock,
            return_value=type("R", (), {"success": False, "error": "transient k8s error"})(),
        ):
            result = await core.gc_snapshots()

        assert result.success is True  # the sweep itself succeeded
        assert len(result.data) == 1
        assert result.data[0]["deleted"] is False
        assert "transient" in result.data[0]["error"]
