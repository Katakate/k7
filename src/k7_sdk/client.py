from __future__ import annotations

import requests

try:
    import httpx  # optional dependency for async client
except Exception:  # pragma: no cover
    httpx = None


class SandboxProxy:
    """Proxy object for sandbox operations."""

    def __init__(self, name: str, namespace: str, client: Client):
        self.name = name
        self.namespace = namespace
        self._client = client

    def exec(self, code: str) -> dict:
        """Execute code in the sandbox."""
        return self._client._exec_command(self.name, code, self.namespace)

    def delete(self) -> dict:
        """Delete this sandbox."""
        return self._client.delete(self.name, self.namespace)

    def pause(self, snapshot: str | None = None) -> dict:
        """Pause this sandbox (scale to 0), optionally with a VolumeSnapshot.

        Pass ``snapshot="my-snap"`` to create a crash-consistent
        ``VolumeSnapshot`` of the sandbox's root PVC before scaling to 0.
        """
        return self._client.pause(self.name, namespace=self.namespace, snapshot=snapshot)

    def resume(self) -> dict:
        """Resume this sandbox (scale back to 1)."""
        return self._client.resume(self.name, namespace=self.namespace)

    def fork(self, new_name: str, snapshot: str | None = None) -> SandboxProxy:
        """Fork this sandbox into ``new_name``; returns a proxy for the new sandbox.

        Blocks until the cloned PVC is bound (the server-side `fork_sandbox`
        waits for the new pod to schedule before returning).
        """
        return self._client.fork(self.name, new_name, namespace=self.namespace, snapshot=snapshot)

    def snapshot(self, snapshot_name: str) -> dict:
        """Snapshot this sandbox's root PVC without pausing it (kind=named)."""
        return self._client.create_snapshot(self.name, snapshot_name, namespace=self.namespace)

    def logs(self, tail: int = 200, container: str = "sandbox", since: int = 0) -> str:
        """Return a snapshot of this sandbox's pod logs."""
        return self._client.logs(self.name, namespace=self.namespace, container=container, tail=tail, since=since)


class Client:
    """K7 Python SDK Client."""

    def __init__(self, endpoint: str, api_key: str, verify_ssl: bool = True):
        self.base_url = endpoint.rstrip("/")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": api_key})
        self.session.verify = verify_ssl

    def _unwrap(self, response) -> dict:
        data = response.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    def create(self, sandbox_config: dict) -> SandboxProxy:
        """Create a new sandbox and return a proxy object."""
        response = self.session.post(f"{self.base_url}/api/v1/sandboxes", json=sandbox_config)
        response.raise_for_status()

        name = sandbox_config.get("name")
        namespace = sandbox_config.get("namespace", "default")

        return SandboxProxy(name, namespace, self)

    def list(self, namespace: str | None = None) -> list[dict]:
        """List all sandboxes."""
        params = {"namespace": namespace} if namespace else {}
        response = self.session.get(f"{self.base_url}/api/v1/sandboxes", params=params)
        response.raise_for_status()
        return self._unwrap(response)

    def delete(self, name: str, namespace: str = "default") -> dict:
        """Delete a sandbox."""
        response = self.session.delete(f"{self.base_url}/api/v1/sandboxes/{name}", params={"namespace": namespace})
        response.raise_for_status()
        return self._unwrap(response)

    def delete_all(self, namespace: str = "default") -> dict:
        """Delete all sandboxes in a namespace."""
        response = self.session.delete(f"{self.base_url}/api/v1/sandboxes", params={"namespace": namespace})
        response.raise_for_status()
        return self._unwrap(response)

    def install(
        self,
        playbook: str | None = None,
        inventory: str | None = None,
        verbose: bool = False,
    ) -> dict:
        """Install K7 on target hosts."""
        response = self.session.post(
            f"{self.base_url}/api/v1/install",
            json={"playbook": playbook, "inventory": inventory, "verbose": verbose},
        )
        response.raise_for_status()
        return self._unwrap(response)

    def get_metrics(self, namespace: str | None = None) -> dict:
        """Get resource usage metrics for sandboxes."""
        params = {"namespace": namespace} if namespace else {}
        response = self.session.get(f"{self.base_url}/api/v1/sandboxes/metrics", params=params)
        response.raise_for_status()
        return self._unwrap(response)

    def nodes_storage(self) -> dict:
        """Per-node storage-pool utilization (kfd thin-pool + k7d disks).

        Returns a map of node name → ``{kata_thinpool, k7d_disks}`` (or
        ``{error: ...}`` when that node's agent is unreachable).
        """
        response = self.session.get(f"{self.base_url}/api/v1/nodes/storage", timeout=120)
        response.raise_for_status()
        return self._unwrap(response)

    def pause(
        self,
        name: str,
        namespace: str = "default",
        snapshot: str | None = None,
    ) -> dict:
        """Pause a sandbox (scale to 0), optionally taking a Longhorn VolumeSnapshot.

        ``snapshot``, when set, names a crash-consistent VolumeSnapshot taken
        of the sandbox's root PVC. The PVC name and ``VolumeSnapshotClass``
        are derived server-side (kata-qemu-longhorn convention + the playbook's
        ``longhorn`` class).
        """
        body: dict = {"namespace": namespace}
        if snapshot is not None:
            body["snapshot"] = snapshot
        response = self.session.post(f"{self.base_url}/api/v1/sandboxes/{name}/pause", json=body, timeout=120)
        response.raise_for_status()
        return self._unwrap(response)

    def resume(self, name: str, namespace: str = "default") -> dict:
        """Resume a paused sandbox (scale back to 1)."""
        response = self.session.post(
            f"{self.base_url}/api/v1/sandboxes/{name}/resume",
            json={"namespace": namespace},
            timeout=30,
        )
        response.raise_for_status()
        return self._unwrap(response)

    def fork(
        self,
        source: str,
        new_name: str,
        namespace: str = "default",
        snapshot: str | None = None,
    ) -> SandboxProxy:
        """Fork ``source`` into a new sandbox ``new_name``; returns a proxy for it.

        Blocks until the cloned PVC is bound. Today this takes ~45s for
        kata-qemu-longhorn; the HTTP request stays open for the duration.
        """
        body: dict = {"new_name": new_name, "namespace": namespace}
        if snapshot is not None:
            body["snapshot"] = snapshot
        response = self.session.post(f"{self.base_url}/api/v1/sandboxes/{source}/fork", json=body, timeout=600)
        response.raise_for_status()
        self._unwrap(response)
        return SandboxProxy(new_name, namespace, self)

    # ------------------------------------------------------------------
    # Spec 10e: VolumeSnapshot CRUD + GC.
    # ------------------------------------------------------------------

    def list_snapshots(
        self,
        namespace: str = "default",
        all_namespaces: bool = False,
        sandbox: str | None = None,
        kind: str | None = None,
    ) -> list[dict]:
        params: dict = {"namespace": namespace, "all_namespaces": str(all_namespaces).lower()}
        if sandbox is not None:
            params["sandbox"] = sandbox
        if kind is not None:
            params["kind"] = kind
        response = self.session.get(f"{self.base_url}/api/v1/snapshots", params=params, timeout=30)
        response.raise_for_status()
        return self._unwrap(response)

    def get_snapshot(self, name: str, namespace: str = "default") -> dict | None:
        response = self.session.get(
            f"{self.base_url}/api/v1/snapshots/{name}", params={"namespace": namespace}, timeout=15
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return self._unwrap(response)

    def create_snapshot(self, sandbox: str, snapshot_name: str, namespace: str = "default") -> dict:
        response = self.session.post(
            f"{self.base_url}/api/v1/sandboxes/{sandbox}/snapshot",
            json={"snapshot_name": snapshot_name, "namespace": namespace},
            timeout=120,
        )
        response.raise_for_status()
        return self._unwrap(response)

    def delete_snapshot(self, name: str, namespace: str = "default") -> dict:
        response = self.session.delete(
            f"{self.base_url}/api/v1/snapshots/{name}", params={"namespace": namespace}, timeout=60
        )
        response.raise_for_status()
        return self._unwrap(response)

    def gc_snapshots(
        self,
        namespace: str = "default",
        all_namespaces: bool = False,
        keep_fork_for: str = "10m",
        dry_run: bool = False,
    ) -> dict:
        body: dict = {
            "namespace": namespace,
            "all_namespaces": all_namespaces,
            "keep_fork_for": keep_fork_for,
            "dry_run": dry_run,
        }
        response = self.session.post(f"{self.base_url}/api/v1/snapshots/gc", json=body, timeout=120)
        response.raise_for_status()
        return self._unwrap(response)

    def restore(
        self,
        snapshot_name: str,
        new_sandbox_name: str,
        namespace: str = "default",
        overrides: dict | None = None,
        keep_snapshot: bool = True,
    ) -> SandboxProxy:
        """Restore a brand-new sandbox from a standalone VolumeSnapshot (Spec 10f).

        ``overrides`` is a JSON-serialisable dict matching ``SandboxConfigOverrides``
        on the server (keys: ``image``, ``backend``, ``root_disk_size``, ``sidecar``,
        ``limits``, ``entrypoint``, ``cmd``, ``before_script``). Pass at minimum
        ``{"image": "..."}`` if the snapshot was created before Spec 10f and
        therefore lacks the ``k7.io/source-image`` annotation.

        Returns a ``SandboxProxy`` for the new sandbox. The server-side restore
        waits for the cloned PVC to be Bound and the Deployment to be Ready
        before responding, so the proxy is safe to ``exec`` against immediately.
        """
        body: dict = {
            "new_sandbox_name": new_sandbox_name,
            "namespace": namespace,
            "keep_snapshot": keep_snapshot,
        }
        if overrides:
            body["overrides"] = overrides
        response = self.session.post(
            f"{self.base_url}/api/v1/snapshots/{snapshot_name}/restore",
            json=body,
            timeout=600,
        )
        response.raise_for_status()
        self._unwrap(response)
        return SandboxProxy(new_sandbox_name, namespace, self)

    def exec(self, name: str, command: str, namespace: str = "default") -> dict:
        """Execute a shell command in a sandbox; returns ``{stdout, stderr, exit_code, duration_ms}``."""
        return self._exec_command(name, command, namespace)

    def logs(
        self,
        name: str,
        namespace: str = "default",
        container: str = "sandbox",
        tail: int = 200,
        since: int = 0,
    ) -> str:
        """Return a snapshot of the sandbox pod's logs (no streaming yet).

        For interactive follow today, use ``k7 --core logs --follow`` on
        the node. Streaming support is a separate spec.
        """
        params: dict = {"namespace": namespace, "container": container, "tail": tail}
        if since > 0:
            params["since"] = since
        response = self.session.get(
            f"{self.base_url}/api/v1/sandboxes/{name}/logs",
            params=params,
            timeout=60,
        )
        response.raise_for_status()
        data = self._unwrap(response)
        if isinstance(data, dict):
            return str(data.get("logs", ""))
        return str(data)

    def _exec_command(self, name: str, command: str, namespace: str) -> dict:
        """Internal method to execute command in sandbox."""
        response = self.session.post(
            f"{self.base_url}/api/v1/sandboxes/{name}/exec",
            json={"command": command},
            params={"namespace": namespace},
        )
        response.raise_for_status()
        return self._unwrap(response)


class AsyncClient:
    """K7 Python SDK Async Client."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        verify_ssl: bool = True,
        timeout: float = 30.0,
    ):
        if httpx is None:
            raise RuntimeError("httpx is required for AsyncClient. Install with `pip install httpx`.")
        self.base_url = endpoint.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-API-Key": api_key},
            verify=verify_ssl,
            timeout=timeout,
        )

    async def create(self, sandbox_config: dict) -> dict:
        r = await self._client.post("/api/v1/sandboxes", json=sandbox_config)
        r.raise_for_status()
        return r.json()

    async def list(self, namespace: str | None = None) -> list[dict]:
        params = {"namespace": namespace} if namespace else {}
        r = await self._client.get("/api/v1/sandboxes", params=params)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    async def delete(self, name: str, namespace: str = "default") -> dict:
        r = await self._client.delete(f"/api/v1/sandboxes/{name}", params={"namespace": namespace})
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    async def delete_all(self, namespace: str = "default") -> dict:
        r = await self._client.delete("/api/v1/sandboxes", params={"namespace": namespace})
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    async def logs(
        self,
        name: str,
        namespace: str = "default",
        container: str = "sandbox",
        tail: int = 200,
        since: int = 0,
    ) -> str:
        params: dict = {"namespace": namespace, "container": container, "tail": tail}
        if since > 0:
            params["since"] = since
        r = await self._client.get(f"/api/v1/sandboxes/{name}/logs", params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        unwrapped = data["data"] if isinstance(data, dict) and "data" in data else data
        if isinstance(unwrapped, dict):
            return str(unwrapped.get("logs", ""))
        return str(unwrapped)

    async def exec(self, name: str, command: str, namespace: str = "default") -> dict:
        r = await self._client.post(
            f"/api/v1/sandboxes/{name}/exec",
            json={"command": command},
            params={"namespace": namespace},
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    async def get_metrics(self, namespace: str | None = None) -> dict:
        params = {"namespace": namespace} if namespace else {}
        r = await self._client.get("/api/v1/sandboxes/metrics", params=params)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    async def nodes_storage(self) -> dict:
        """Per-node storage-pool utilization (kfd thin-pool + k7d disks)."""
        r = await self._client.get("/api/v1/nodes/storage", timeout=120)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    async def pause(
        self,
        name: str,
        namespace: str = "default",
        snapshot: str | None = None,
    ) -> dict:
        body: dict = {"namespace": namespace}
        if snapshot is not None:
            body["snapshot"] = snapshot
        r = await self._client.post(f"/api/v1/sandboxes/{name}/pause", json=body, timeout=120)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    async def resume(self, name: str, namespace: str = "default") -> dict:
        r = await self._client.post(
            f"/api/v1/sandboxes/{name}/resume",
            json={"namespace": namespace},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    async def fork(
        self,
        source: str,
        new_name: str,
        namespace: str = "default",
        snapshot: str | None = None,
    ) -> dict:
        body: dict = {"new_name": new_name, "namespace": namespace}
        if snapshot is not None:
            body["snapshot"] = snapshot
        r = await self._client.post(f"/api/v1/sandboxes/{source}/fork", json=body, timeout=600)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    async def list_snapshots(
        self,
        namespace: str = "default",
        all_namespaces: bool = False,
        sandbox: str | None = None,
        kind: str | None = None,
    ) -> list[dict]:
        params: dict = {"namespace": namespace, "all_namespaces": str(all_namespaces).lower()}
        if sandbox is not None:
            params["sandbox"] = sandbox
        if kind is not None:
            params["kind"] = kind
        r = await self._client.get("/api/v1/snapshots", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data["data"] if isinstance(data, dict) and "data" in data else data

    async def get_snapshot(self, name: str, namespace: str = "default") -> dict | None:
        r = await self._client.get(f"/api/v1/snapshots/{name}", params={"namespace": namespace}, timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        return data["data"] if isinstance(data, dict) and "data" in data else data

    async def create_snapshot(self, sandbox: str, snapshot_name: str, namespace: str = "default") -> dict:
        r = await self._client.post(
            f"/api/v1/sandboxes/{sandbox}/snapshot",
            json={"snapshot_name": snapshot_name, "namespace": namespace},
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        return data["data"] if isinstance(data, dict) and "data" in data else data

    async def delete_snapshot(self, name: str, namespace: str = "default") -> dict:
        r = await self._client.delete(f"/api/v1/snapshots/{name}", params={"namespace": namespace}, timeout=60)
        r.raise_for_status()
        data = r.json()
        return data["data"] if isinstance(data, dict) and "data" in data else data

    async def gc_snapshots(
        self,
        namespace: str = "default",
        all_namespaces: bool = False,
        keep_fork_for: str = "10m",
        dry_run: bool = False,
    ) -> dict:
        body: dict = {
            "namespace": namespace,
            "all_namespaces": all_namespaces,
            "keep_fork_for": keep_fork_for,
            "dry_run": dry_run,
        }
        r = await self._client.post("/api/v1/snapshots/gc", json=body, timeout=120)
        r.raise_for_status()
        data = r.json()
        return data["data"] if isinstance(data, dict) and "data" in data else data

    async def restore(
        self,
        snapshot_name: str,
        new_sandbox_name: str,
        namespace: str = "default",
        overrides: dict | None = None,
        keep_snapshot: bool = True,
    ) -> dict:
        body: dict = {
            "new_sandbox_name": new_sandbox_name,
            "namespace": namespace,
            "keep_snapshot": keep_snapshot,
        }
        if overrides:
            body["overrides"] = overrides
        r = await self._client.post(
            f"/api/v1/snapshots/{snapshot_name}/restore",
            json=body,
            timeout=600,
        )
        r.raise_for_status()
        data = r.json()
        return data["data"] if isinstance(data, dict) and "data" in data else data

    async def aclose(self):
        await self._client.aclose()
