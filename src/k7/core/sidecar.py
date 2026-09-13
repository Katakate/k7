"""Generic sidecar framework: registry + spec dataclass.

Adding a new sidecar type requires only a new entry in SIDECAR_REGISTRY.
All injection logic in core.py is driven by SidecarSpec fields — no
type-specific branching.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SidecarSpec:
    image: str
    socket_mount: str  # directory containing the socket (shared via emptyDir)
    socket_name: str  # socket filename within socket_mount
    data_path: str  # where the daemon stores persistent data
    pvc_subdir: str  # subdirectory on the Longhorn PVC for ql backend
    readiness_cmd: list[str]
    privileged: bool
    env: dict[str, str] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)


SIDECAR_REGISTRY: dict[str, SidecarSpec] = {
    # Deprecated as the user-facing Docker path (``--docker``). Nothing
    # routes Docker through this entry on any
    # backend. Kept so a real CRI sidecar still injects, and so
    # ``k7 fork`` on k7d can still reject it loudly (test_k7d_sidecar_fork_rejected_loudly).
    "docker": SidecarSpec(
        image="docker:27.5-dind",
        socket_mount="/var/run",
        socket_name="docker.sock",
        data_path="/var/lib/docker",
        pvc_subdir="docker",
        readiness_cmd=["docker", "info"],
        privileged=True,
        env={"DOCKER_TLS_CERTDIR": ""},
        args=["--tls=false"],
    ),
}
