from dataclasses import asdict, dataclass, fields
from typing import Any

import yaml


@dataclass
class SandboxConfig:
    """Data model for sandbox configuration

    For kata-firecracker-devmapper: image is REQUIRED (the container to run)
    For kata-qemu-longhorn: image is REQUIRED (no bare VM mode)
    For k7d: image is REQUIRED (runs in a k7d microVM, runtimeClassName k7)
    """

    name: str
    image: str
    namespace: str = "default"
    runtime_class_name: str | None = None
    root_disk_size: str | None = "10Gi"
    backend: str | None = None  # "kata-firecracker-devmapper", "kata-qemu-longhorn", or "k7d"
    env_file: str | None = None
    egress_whitelist: list[str] | None = None
    limits: dict[str, str] | None = None
    before_script: str = ""
    entrypoint: list[str] | None = None
    cmd: list[str] | None = None
    sidecar: str | None = None  # key into SIDECAR_REGISTRY, or None
    # Security toggles (default off) and capabilities configuration
    pod_non_root: bool = False
    container_non_root: bool = False
    cap_drop: list[str] | None = None  # default behavior handled in core: drop ALL
    cap_add: list[str] | None = None
    # Optional explicit node placement (sets pod's node_name). Used by tests
    # that need to inspect host-side state for a sandbox they just created.
    node_name: str | None = None
    # Note: ingress isolation is enforced by core with a hardcoded NetworkPolicy

    def __post_init__(self):
        if self.limits is None:
            self.limits = {}

    @classmethod
    def from_yaml(cls, yaml_path: str) -> "SandboxConfig":
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        return cls(**data)

    @classmethod
    def from_dict(cls, data: dict) -> "SandboxConfig":
        # Be forward/backward compatible with API by ignoring unknown keys
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in (data or {}).items() if k in allowed}
        return cls(**filtered)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SandboxInfo:
    name: str
    namespace: str
    status: str
    ready: str
    restarts: int
    age: str
    image: str
    backend: str = "unknown"
    # Kubernetes node hosting the sandbox pod ("" while unscheduled). Needed
    # by API/SDK clients to reason about k7d VM-op node locality (spec 18f
    # issue 6): k7d pause/resume/fork must run on the sandbox's node.
    node: str = ""
    error_message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OperationResult:
    success: bool
    message: str = ""
    error: str = ""
    data: Any = None

    def to_dict(self) -> dict:
        return asdict(self)


# Spec 10e: VolumeSnapshot kinds, used both for the ``k7.io/kind`` annotation
# we stamp at creation time and for the heuristic fallback that classifies
# pre-existing snapshots by name pattern.
SNAPSHOT_KIND_PAUSE = "pause"
SNAPSHOT_KIND_FORK = "fork"
SNAPSHOT_KIND_NAMED = "named"


@dataclass
class SandboxConfigOverrides:
    """Optional per-call overrides for ``K7Core.restore_sandbox`` (Spec 10f).

    Every field is optional. When ``None`` the corresponding value is taken
    from the snapshot's ``k7.io/source-*`` annotations (stamped at snapshot
    creation time); the user-supplied override wins when set. ``image`` is
    the only field that has no safe default — restore fails with a clear
    error if neither the annotation nor an override is present.
    """

    image: str | None = None
    backend: str | None = None
    root_disk_size: str | None = None
    sidecar: str | None = None
    limits: dict[str, str] | None = None
    entrypoint: list[str] | None = None
    cmd: list[str] | None = None
    before_script: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class SnapshotInfo:
    """Inspectable view of a Kubernetes ``VolumeSnapshot`` managed by k7.

    Fields mirror what ``k7 snapshot list/inspect`` and the HTTP API surface.
    """

    name: str
    namespace: str
    source_pvc: str
    source_sandbox: str
    kind: str  # one of SNAPSHOT_KIND_PAUSE / _FORK / _NAMED
    ready_to_use: bool
    creation_timestamp: str  # RFC3339 string (kept verbatim from the API)
    age: str  # human-readable, computed from creation_timestamp
    size_bytes: int = 0  # 0 when restoreSize is unknown / not yet set
    snapshot_class: str = "longhorn"

    def to_dict(self) -> dict:
        return asdict(self)
