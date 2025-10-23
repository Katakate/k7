import os
import tempfile
import subprocess
import time
import re
import sys
from typing import Optional, List, Dict, Callable
from pathlib import Path
import shutil
import yaml
from datetime import datetime
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream
from .models import SandboxConfig, SandboxInfo, ExecResult, OperationResult
from importlib import resources


class K7Core:
    """Core business logic for sandbox management"""

    def __init__(self, kubeconfig_path: Optional[str] = None):
        self.kubeconfig_path = kubeconfig_path
        self._apps_v1_client = None
        self._core_v1_client = None
        self._networking_v1_client = None
        self._metrics_client = None
        self._config_loaded = False

    def _load_k3s_config(self):
        """Load k3s kubeconfig with fallback to standard locations."""
        if self._config_loaded:
            return

        k3s_config_path = self.kubeconfig_path or "/etc/rancher/k3s/k3s.yaml"

        try:
            if os.path.exists(k3s_config_path):
                config.load_kube_config(config_file=k3s_config_path)
            else:
                config.load_kube_config()
        except config.ConfigException:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                raise Exception("Could not load Kubernetes config")

        self._config_loaded = True

    def _get_apps_v1_client(self):
        """Get or create AppsV1Api client instance."""
        if self._apps_v1_client is None:
            self._load_k3s_config()
            self._apps_v1_client = client.AppsV1Api()
        return self._apps_v1_client

    def _get_core_v1_client(self):
        """Get or create CoreV1Api client instance."""
        if self._core_v1_client is None:
            self._load_k3s_config()
            self._core_v1_client = client.CoreV1Api()
        return self._core_v1_client

    def _get_networking_v1_client(self):
        """Get or create NetworkingV1Api client instance."""
        if self._networking_v1_client is None:
            self._load_k3s_config()
            self._networking_v1_client = client.NetworkingV1Api()
        return self._networking_v1_client

    def _get_metrics_client(self):
        """Get or create CustomObjectsApi client instance."""
        if self._metrics_client is None:
            self._load_k3s_config()
            self._metrics_client = client.CustomObjectsApi()
        return self._metrics_client

    def _get_embedded_playbook(self) -> str:
        """Get embedded Ansible playbook content."""
        try:
            return (
                resources.files("k7.deploy")
                .joinpath("k7-install-node.yaml")
                .read_text()
            )
        except Exception:
            return ""

    def _materialize_embedded_package_root(self) -> Path:
        """Ensure the embedded k7 package exists at a stable on-disk path.

        Returns the directory path containing the `k7/` package tree that Docker builds can use
        as context (without relying on ephemeral temp dirs).
        """
        # Allow override via env (user-writable dir), default to FHS path
        base_dir = Path(os.getenv("K7_EMBEDDED_ROOT", "/var/lib/k7/embedded"))
        pkg_dir = base_dir / "k7"
        try:
            base_dir.mkdir(parents=True, exist_ok=True)
            # Extract current installed/bundled k7 to the stable path
            k7_root = resources.files("k7")
            with resources.as_file(k7_root) as src_root:
                src_root_path = Path(str(src_root))
                if src_root_path.exists():
                    # Copy tree (refresh) to ensure it matches the bundled content
                    if pkg_dir.exists():
                        # Keep existing but refresh files (dirs_exist_ok requires Python 3.8+)
                        shutil.rmtree(pkg_dir)
                    shutil.copytree(src_root_path, pkg_dir, dirs_exist_ok=False)
        except Exception:
            # Best effort; if something fails, continue with whatever exists
            pass
        return pkg_dir

    def _get_embedded_docker_compose(self) -> str:
        """Get absolute path to embedded docker-compose.yml, or empty string if missing."""
        try:
            base_dir = Path(os.getenv("K7_EMBEDDED_ROOT", "/var/lib/k7/embedded"))
            pkg_dir = self._materialize_embedded_package_root()
            compose_path = pkg_dir / "api" / "docker-compose.yml"
            dockerfile_path = pkg_dir / "api" / "Dockerfile.api"
            if compose_path.exists() and dockerfile_path.exists():
                # Ensure build context points at the embedded base dir (not repo-relative)
                try:
                    txt = compose_path.read_text()
                    desired = f"context: {base_dir}"
                    if "context: ../.." in txt and desired not in txt:
                        txt = txt.replace("context: ../..", desired)
                    # Ensure runtime sees the embedded code under /app/k7 (read-only)
                    embed_mount = f"      - {base_dir}/k7:/app/k7:ro"
                    if embed_mount not in txt:
                        # Insert after the /etc/k7 mount if present, else at the start of volumes
                        if "      - /etc/k7:/etc/k7\n" in txt:
                            txt = txt.replace(
                                "      - /etc/k7:/etc/k7\n",
                                "      - /etc/k7:/etc/k7\n" + embed_mount + "\n",
                            )
                        elif "    volumes:\n" in txt:
                            txt = txt.replace(
                                "    volumes:\n",
                                "    volumes:\n" + embed_mount + "\n",
                            )
                    compose_path.write_text(txt)
                except Exception:
                    pass
                return str(compose_path)
        except Exception:
            pass
        return ""

    def _get_embedded_dockerfile_api(self) -> str:
        """Get absolute path to embedded Dockerfile.api, or empty string if missing."""
        try:
            pkg_dir = self._materialize_embedded_package_root()
            dockerfile_path = pkg_dir / "api" / "Dockerfile.api"
            if dockerfile_path.exists():
                return str(dockerfile_path)
        except Exception:
            pass
        return ""

    def _get_embedded_inventory(self, hosts: List[str]) -> str:
        """Generate Ansible inventory from host list."""
        inventory_lines = ["[k7_nodes]"]
        for host in hosts:
            inventory_lines.append(
                f"{host} ansible_user=root ansible_ssh_private_key_file=~/.ssh/id_rsa"
            )
        return "\n".join(inventory_lines)

    def _parse_resource_value(self, value: str) -> int:
        """Parse Kubernetes resource value to numeric form."""
        if not value:
            return 0

        value = value.strip().lower()
        if value.endswith("m"):
            return int(value[:-1])
        elif value.endswith("gi"):
            return int(value[:-2]) * 1024
        elif value.endswith("mi"):
            return int(value[:-2])
        elif value.endswith("ki"):
            return int(value[:-2]) // 1024
        else:
            try:
                return int(value)
            except ValueError:
                return 0

    def _validate_limits(self, limits: Dict[str, str]) -> bool:
        """Validate resource limits."""
        if not limits:
            return True

        for key, value in limits.items():
            if key in ["cpu", "memory", "ephemeral-storage"]:
                if self._parse_resource_value(value) <= 0:
                    return False
        return True

    def _count_playbook_tasks(self, playbook_content: str) -> int:
        """Count tasks in Ansible playbook."""
        try:
            playbook = yaml.safe_load(playbook_content)
            if isinstance(playbook, list) and len(playbook) > 0:
                return len(playbook[0].get("tasks", []))
        except Exception:
            pass
        return 0

    def _get_kata_sandboxes(self, namespace: Optional[str] = None) -> List:
        """Get all Kata sandboxes (deployments with kata runtime)."""
        apps_v1 = self._get_apps_v1_client()

        if namespace:
            deployments = apps_v1.list_namespaced_deployment(namespace=namespace)
        else:
            deployments = apps_v1.list_deployment_for_all_namespaces()

        kata_deployments = []
        for deployment in deployments.items:
            if deployment.spec.template.spec.runtime_class_name == "kata" or (
                deployment.metadata.labels
                and deployment.metadata.labels.get("runtime") == "kata"
            ):
                kata_deployments.append(deployment)

        return kata_deployments

    def _delete_sandbox_resources(self, name: str, namespace: str) -> OperationResult:
        """Delete all resources associated with a sandbox."""
        apps_v1 = self._get_apps_v1_client()
        v1 = self._get_core_v1_client()
        networking_v1 = self._get_networking_v1_client()

        errors = []

        try:
            apps_v1.delete_namespaced_deployment(name=name, namespace=namespace)
        except ApiException as e:
            if e.status != 404:
                errors.append(f"deployment: {e}")

        try:
            v1.delete_namespaced_secret(name=f"{name}-env", namespace=namespace)
        except ApiException as e:
            if e.status != 404:
                errors.append(f"secret: {e}")

        try:
            networking_v1.delete_namespaced_network_policy(
                name=f"{name}-netpol", namespace=namespace
            )
        except ApiException as e:
            if e.status != 404:
                errors.append(f"network policy: {e}")

        try:
            networking_v1.delete_namespaced_network_policy(
                name=f"{name}-deny-ingress", namespace=namespace
            )
        except ApiException as e:
            if e.status != 404:
                errors.append(f"network policy deny-ingress: {e}")

        if errors:
            return OperationResult(success=False, error="; ".join(errors))

        return OperationResult(
            success=True, message=f"Sandbox {name} deleted successfully"
        )

    def install_node(
        self,
        playbook_content: Optional[str] = None,
        inventory_content: Optional[str] = None,
        verbose: bool = False,
        progress_callback: Optional[Callable[[Dict], None]] = None,
        stream_output: bool = False,
    ) -> OperationResult:
        """Install K7 on target nodes using Ansible."""
        try:
            if not playbook_content:
                playbook_content = self._get_embedded_playbook()

            if not playbook_content:
                return OperationResult(
                    success=False, error="No playbook content available"
                )

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            ) as playbook_file:
                playbook_file.write(playbook_content)
                playbook_path = playbook_file.name

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".ini", delete=False
            ) as inventory_file:
                inventory_file.write(
                    inventory_content
                    or "[k7_nodes]\nlocalhost ansible_connection=local ansible_user=root"
                )
                inventory_path = inventory_file.name

            total_tasks = self._count_playbook_tasks(playbook_content)
            if progress_callback:
                try:
                    progress_callback({"type": "total", "total_tasks": total_tasks})
                except Exception:
                    pass

            cmd = ["ansible-playbook", "-i", inventory_path, playbook_path]
            if verbose:
                cmd.append("-v")

            # Stream output to allow progress parsing
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )

            current_index = 0
            task_pattern = re.compile(r"^TASK \[(.*?)\]")
            ansi_escape = re.compile(r"\x1b\[[0-9;]*[mK]")
            combined_output = []
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    combined_output.append(line)
                    if stream_output:
                        try:
                            # Print the line as-is to stdout in verbose mode
                            sys.stdout.write(line)
                            sys.stdout.flush()
                        except Exception:
                            pass
                    # Strip common ANSI escape sequences before matching
                    clean_line = ansi_escape.sub("", line)
                    match = task_pattern.search(clean_line)
                    if match:
                        current_index += 1
                        if progress_callback:
                            try:
                                progress_callback(
                                    {
                                        "type": "task_start",
                                        "name": match.group(1),
                                        "index": current_index,
                                        "total": total_tasks,
                                    }
                                )
                            except Exception:
                                pass
                process.wait()
            finally:
                try:
                    os.unlink(playbook_path)
                except Exception:
                    pass
                try:
                    os.unlink(inventory_path)
                except Exception:
                    pass

            if process.returncode == 0:
                return OperationResult(
                    success=True, message="Installation completed successfully"
                )
            else:
                # Include tail of output for easier debugging
                tail = "".join(combined_output[-50:]) if combined_output else ""
                return OperationResult(
                    success=False,
                    error=f"Installation failed (code {process.returncode}). Output tail:\n{tail}",
                )

        except Exception as e:
            return OperationResult(success=False, error=str(e))

    def create_sandbox(
        self,
        config: SandboxConfig,
        progress_callback: Optional[Callable[[Dict], None]] = None,
    ) -> OperationResult:
        """Create a new sandbox with the given configuration."""
        try:

            def _emit(event: Dict):
                if progress_callback:
                    try:
                        progress_callback(event)
                    except Exception:
                        pass

            if not self._validate_limits(config.limits):
                return OperationResult(success=False, error="Invalid resource limits")

            apps_v1 = self._get_apps_v1_client()
            v1 = self._get_core_v1_client()
            networking_v1 = self._get_networking_v1_client()

            _emit({"stage": "provisioning", "status": "start"})

            if config.env_file and os.path.exists(config.env_file):
                with open(config.env_file, "r") as f:
                    env_content = f.read()

                # Parse env file lines into individual key/value string_data entries
                env_vars: Dict[str, str] = {}
                for line in env_content.splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    key, value = stripped.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key:
                        env_vars[key] = value

                if not env_vars:
                    return OperationResult(
                        success=False,
                        error="env_file is empty or invalid; no variables parsed",
                    )

                secret = client.V1Secret(
                    metadata=client.V1ObjectMeta(
                        name=f"{config.name}-env", namespace=config.namespace
                    ),
                    string_data=env_vars,
                )

                try:
                    v1.create_namespaced_secret(namespace=config.namespace, body=secret)
                except ApiException as e:
                    if e.status != 409:
                        return OperationResult(
                            success=False, error=f"Failed to create secret: {e}"
                        )

            # Build main container command with optional before_script that runs inside the main container
            before_done_file = "/tmp/k7_before_done"
            if config.before_script:
                # Ensure failures halt startup; mark completion to drive readiness
                script_block = config.before_script.strip()
                main_cmd = (
                    f"set -euo pipefail; rm -f {before_done_file}; "
                    f"{script_block}; "
                    f"touch {before_done_file}; exec sleep 365d"
                )
            else:
                main_cmd = "sleep 365d"

            # Build container security context based on config
            # Default capability policy: drop ALL, optionally add back caps via cap_add
            drop_caps: Optional[List[str]]
            add_caps: Optional[List[str]]
            if getattr(config, "cap_drop", None) is None:
                drop_caps = ["ALL"]
            else:
                drop_caps = [c.upper() for c in (config.cap_drop or [])]
            add_caps = [c.upper() for c in (getattr(config, "cap_add", None) or [])] or None

            container_sec_ctx = client.V1SecurityContext(
                allow_privilege_escalation=False,
                run_as_non_root=True if getattr(config, "container_non_root", False) else None,
                run_as_user=65532 if getattr(config, "container_non_root", False) else None,
                seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
                capabilities=client.V1Capabilities(
                    drop=drop_caps if drop_caps else None,
                    add=add_caps,
                ),
            )

            container = client.V1Container(
                name="sandbox",
                image=config.image,
                command=["/bin/sh", "-c", main_cmd],
                resources=client.V1ResourceRequirements(
                    limits=config.limits if config.limits else None,
                    requests=config.limits if config.limits else None,
                ),
                security_context=container_sec_ctx,
            )

            if config.env_file:
                container.env_from = [
                    client.V1EnvFromSource(
                        secret_ref=client.V1SecretEnvSource(name=f"{config.name}-env")
                    )
                ]

            # Harden container: handled above via container_sec_ctx (configurable)

            # Readiness probe flips to Ready only after before_script completes
            if config.before_script:
                container.readiness_probe = client.V1Probe(
                    _exec=client.V1ExecAction(
                        command=["/bin/sh", "-c", f"test -f {before_done_file}"]
                    ),
                    initial_delay_seconds=1,
                    period_seconds=2,
                    timeout_seconds=2,
                    failure_threshold=30,
                )
            else:
                # Immediately Ready when no before_script
                container.readiness_probe = client.V1Probe(
                    _exec=client.V1ExecAction(command=["/bin/sh", "-c", "true"]),
                    initial_delay_seconds=0,
                    period_seconds=2,
                )

            runtime_class = getattr(config, "runtime_class_name", None) or "kata"

            pod_sec_ctx = None
            if getattr(config, "pod_non_root", False):
                pod_sec_ctx = client.V1PodSecurityContext(
                    run_as_non_root=True,
                    run_as_user=65532,
                    run_as_group=65532,
                    fs_group=65532,
                )

            pod_spec = client.V1PodSpec(
                containers=[container],
                runtime_class_name=runtime_class,
                restart_policy="Always",
                security_context=pod_sec_ctx,
            )

            deployment = client.V1Deployment(
                metadata=client.V1ObjectMeta(
                    name=config.name,
                    namespace=config.namespace,
                    labels={"app": config.name, "runtime": "kata", "katakate.org/sandbox": config.name},
                ),
                spec=client.V1DeploymentSpec(
                    replicas=1,
                    selector=client.V1LabelSelector(match_labels={"app": config.name}),
                    template=client.V1PodTemplateSpec(
                        metadata=client.V1ObjectMeta(labels={"app": config.name, "katakate.org/sandbox": config.name}),
                        spec=pod_spec,
                    ),
                ),
            )

            try:
                apps_v1.create_namespaced_deployment(
                    namespace=config.namespace, body=deployment
                )
            except ApiException as e:
                if e.status == 409:
                    return OperationResult(
                        success=False, error=f"Sandbox {config.name} already exists"
                    )
                return OperationResult(
                    success=False, error=f"Failed to create deployment: {e}"
                )
            _emit({"stage": "provisioning", "status": "done"})

            # Always emit before_script lifecycle if a script is present
            if config.before_script:
                _emit(
                    {
                        "stage": "before_script",
                        "status": "waiting",
                        "script": config.before_script,
                    }
                )
                try:
                    timeout_seconds = 300
                    start_time = time.time()
                    while time.time() - start_time < timeout_seconds:
                        pods = v1.list_namespaced_pod(
                            namespace=config.namespace,
                            label_selector=f"app={config.name}",
                        )
                        if pods.items:
                            pod = pods.items[0]
                            conds = pod.status.conditions or []
                            if any(
                                getattr(c, "type", None) == "Ready"
                                and getattr(c, "status", None) == "True"
                                for c in conds
                            ):
                                break
                        time.sleep(2)
                except Exception:
                    pass
                _emit({"stage": "before_script", "status": "done"})
            else:
                _emit({"stage": "before_script", "status": "skipped"})

            # Apply egress policy after before_script completes if whitelist specified
            if config.egress_whitelist is not None:
                _emit({"stage": "network_lockdown", "status": "applying"})
                egress_rules = []
                for cidr in config.egress_whitelist:
                    egress_rules.append(
                        client.V1NetworkPolicyEgressRule(
                            to=[
                                client.V1NetworkPolicyPeer(
                                    ip_block=client.V1IPBlock(cidr=cidr)
                                )
                            ]
                        )
                    )

                

                network_policy = client.V1NetworkPolicy(
                    metadata=client.V1ObjectMeta(
                        name=f"{config.name}-netpol", namespace=config.namespace
                    ),
                    spec=client.V1NetworkPolicySpec(
                        pod_selector=client.V1LabelSelector(
                            match_labels={"katakate.org/sandbox": config.name}
                        ),
                        policy_types=["Egress"],
                        egress=egress_rules,
                    ),
                )

                try:
                    networking_v1.create_namespaced_network_policy(
                        namespace=config.namespace, body=network_policy
                    )
                except ApiException as e:
                    if e.status != 409:
                        return OperationResult(
                            success=False, error=f"Failed to create network policy: {e}"
                        )
                _emit({"stage": "network_lockdown", "status": "done"})
            else:
                _emit({"stage": "network_lockdown", "status": "skipped"})

            # Hardcoded deny-all ingress to block inter-VM communication
            try:
                ingress_np = client.V1NetworkPolicy(
                    metadata=client.V1ObjectMeta(
                        name=f"{config.name}-deny-ingress",
                        namespace=config.namespace,
                    ),
                    spec=client.V1NetworkPolicySpec(
                        pod_selector=client.V1LabelSelector(
                            match_labels={"katakate.org/sandbox": config.name}
                        ),
                        policy_types=["Ingress"],
                        ingress=[],
                    ),
                )
                networking_v1.create_namespaced_network_policy(
                    namespace=config.namespace, body=ingress_np
                )
            except ApiException as e:
                status = getattr(e, "status", None)
                if status == 409:
                    try:
                        _emit(
                            {
                                "stage": "network_lockdown",
                                "status": "exists",
                                "policy": f"{config.name}-deny-ingress",
                            }
                        )
                    except Exception:
                        pass
                    # idempotent success
                else:
                    try:
                        _emit(
                            {
                                "stage": "network_lockdown",
                                "status": "error",
                                "error": f"Ingress deny policy error: {e}",
                            }
                        )
                    except Exception:
                        pass
                    return OperationResult(
                        success=False,
                        error=f"Failed to create ingress deny policy: {e}",
                    )

            _emit(
                {
                    "stage": "complete",
                    "status": "success",
                    "message": f"Sandbox {config.name} created successfully",
                }
            )
            return OperationResult(
                success=True, message=f"Sandbox {config.name} created successfully"
            )

        except Exception as e:
            try:
                _emit({"stage": "error", "error": str(e)})
            except Exception:
                pass
            return OperationResult(success=False, error=str(e))

    def list_sandboxes(self, namespace: Optional[str] = None) -> List[SandboxInfo]:
        """List all sandboxes."""
        try:
            v1 = self._get_core_v1_client()
            sandboxes = self._get_kata_sandboxes(namespace)

            sandbox_list = []
            for deployment in sandboxes:
                name = deployment.metadata.name
                ns = deployment.metadata.namespace

                try:
                    pods = v1.list_namespaced_pod(
                        namespace=ns, label_selector=f"app={name}"
                    )
                    if pods.items:
                        pod = pods.items[0]
                        status = pod.status.phase or "Unknown"
                        ready = (
                            "True"
                            if pod.status.conditions
                            and any(
                                c.type == "Ready" and c.status == "True"
                                for c in pod.status.conditions
                            )
                            else "False"
                        )
                        restarts = sum(
                            cs.restart_count
                            for cs in pod.status.container_statuses or []
                        )
                        age = str(
                            datetime.now()
                            - pod.metadata.creation_timestamp.replace(tzinfo=None)
                        )
                        image = (
                            pod.spec.containers[0].image
                            if pod.spec.containers
                            else "Unknown"
                        )
                    else:
                        status = "No Pods"
                        ready = "False"
                        restarts = 0
                        age = "Unknown"
                        image = "Unknown"
                except Exception:
                    status = "Error"
                    ready = "False"
                    restarts = 0
                    age = "Unknown"
                    image = "Unknown"

                sandbox_list.append(
                    SandboxInfo(
                        name=name,
                        namespace=ns,
                        status=status,
                        ready=ready,
                        restarts=restarts,
                        age=age,
                        image=image,
                    )
                )

            return sandbox_list

        except Exception:
            return []

    def delete_sandbox(self, name: str, namespace: str = "default") -> OperationResult:
        """Delete a sandbox."""
        return self._delete_sandbox_resources(name, namespace)

    def delete_all_sandboxes(self, namespace: str = "default") -> OperationResult:
        """Delete all sandboxes in a namespace."""
        try:
            sandboxes = self._get_kata_sandboxes(namespace)
            results = []

            for deployment in sandboxes:
                result = self._delete_sandbox_resources(
                    deployment.metadata.name, namespace
                )
                results.append(
                    {
                        "name": deployment.metadata.name,
                        "success": result.success,
                        "error": result.error if not result.success else None,
                    }
                )

            failed = [r for r in results if not r["success"]]
            if failed:
                return OperationResult(
                    success=False,
                    error=f"Failed to delete {len(failed)} sandboxes",
                    data=results,
                )

            return OperationResult(
                success=True, message=f"Deleted {len(results)} sandboxes", data=results
            )

        except Exception as e:
            return OperationResult(success=False, error=str(e))

    def exec_command(
        self, sandbox_name: str, command: str, namespace: str = "default"
    ) -> ExecResult:
        """Execute a command in a sandbox and return the result."""
        start_time = time.time()

        try:
            apps_v1 = self._get_apps_v1_client()
            v1 = self._get_core_v1_client()

            try:
                apps_v1.read_namespaced_deployment(
                    name=sandbox_name, namespace=namespace
                )
            except ApiException as e:
                if e.status == 404:
                    raise Exception(f"Sandbox {sandbox_name} not found")
                raise Exception(f"Failed to get deployment: {e}")

            pods = v1.list_namespaced_pod(
                namespace=namespace, label_selector=f"app={sandbox_name}"
            )

            if not pods.items:
                raise Exception(f"No pods found for sandbox {sandbox_name}")

            pod = pods.items[0]
            if pod.status.phase != "Running":
                raise Exception(f"Pod is not running (status: {pod.status.phase})")

            pod_name = pod.metadata.name

            exec_command = ["/bin/sh", "-c", command]
            resp = stream(
                v1.connect_get_namespaced_pod_exec,
                pod_name,
                namespace,
                command=exec_command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )

            stdout_data = ""
            stderr_data = ""

            while resp.is_open():
                resp.update(timeout=1)
                if resp.peek_stdout():
                    stdout_data += resp.read_stdout()
                if resp.peek_stderr():
                    stderr_data += resp.read_stderr()

            exit_code = 0 if resp.returncode is None else resp.returncode

            duration_ms = int((time.time() - start_time) * 1000)

            return ExecResult(
                exit_code=exit_code,
                stdout=stdout_data,
                stderr=stderr_data,
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            return ExecResult(
                exit_code=1, stdout="", stderr=str(e), duration_ms=duration_ms
            )

    def get_sandbox_metrics(self, namespace: Optional[str] = None) -> List[Dict]:
        """Get resource usage metrics for sandboxes."""
        try:
            metrics_api = self._get_metrics_client()
            v1 = self._get_core_v1_client()
            sandboxes = self._get_kata_sandboxes(namespace)

            metrics_list = []
            for deployment in sandboxes:
                sb_name = deployment.metadata.name
                sb_namespace = deployment.metadata.namespace

                try:
                    pods = v1.list_namespaced_pod(
                        namespace=sb_namespace, label_selector=f"app={sb_name}"
                    )
                    if not pods.items:
                        continue

                    pod = pods.items[0]
                    if pod.status.phase != "Running":
                        continue

                    pod_name = pod.metadata.name

                    # Get metrics using the correct generic method
                    metrics = metrics_api.get_namespaced_custom_object(
                        group="metrics.k8s.io",
                        version="v1beta1",
                        namespace=sb_namespace,
                        plural="pods",
                        name=pod_name,
                    )

                    if "containers" in metrics and metrics["containers"]:
                        usage = metrics["containers"][0].get("usage", {})

                        metrics_list.append(
                            {
                                "name": sb_name,
                                "namespace": sb_namespace,
                                "cpu_usage": usage.get("cpu", "0n"),
                                "memory_usage": usage.get("memory", "0Ki"),
                            }
                        )

                except Exception:
                    continue

            return metrics_list

        except Exception:
            return []
