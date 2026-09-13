"""Render TLS-flavored k7-api manifests and the Caddyfile.

The static ``manifests/k7-api/{deployment,service}.yaml`` stay the HTTP
NodePort (``--api-insecure-http``). When TLS is on, the playbook copies
those files and this module overwrites them with a Caddy sidecar on
``:8443``. The k7-api container and its HTTP probes are unchanged.
"""

from __future__ import annotations

import argparse
from pathlib import Path

# linux/amd64 digest for caddy:2.10.2 (Docker Hub, 2026-02-11). The
# playbook pulls CADDY_PIN; the pod spec uses CADDY_IMAGE (the tag).
# kubelet imagePullPolicy=Never does not resolve tag@digest to a
# locally imported tag (measured: ErrImageNeverPull).
CADDY_IMAGE = "caddy:2.10.2"
CADDY_DIGEST = "sha256:d8c17a862962def15cde69863a3a463f25a2664942eafd7bdbf050e9c3116b83"
CADDY_PIN = f"{CADDY_IMAGE}@{CADDY_DIGEST}"

_CADDY_CONTAINER_FILES = f"""        - name: caddy
          image: {CADDY_IMAGE}
          imagePullPolicy: Never
          securityContext:
            runAsUser: 0
            runAsGroup: 0
            allowPrivilegeEscalation: false
          ports:
            - containerPort: 8443
              protocol: TCP
          volumeMounts:
            - name: caddy-data
              mountPath: /data
            - name: caddyfile
              mountPath: /etc/caddy/Caddyfile
              subPath: Caddyfile
            - name: tls-certs
              mountPath: /certs
              readOnly: true
          readinessProbe:
            tcpSocket:
              port: 8443
            initialDelaySeconds: 3
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 3
          livenessProbe:
            tcpSocket:
              port: 8443
            initialDelaySeconds: 10
            periodSeconds: 30
            timeoutSeconds: 3
            failureThreshold: 3
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 500m
              memory: 128Mi
"""

_CADDY_CONTAINER_ACME = f"""        - name: caddy
          image: {CADDY_IMAGE}
          imagePullPolicy: Never
          securityContext:
            runAsUser: 0
            runAsGroup: 0
            allowPrivilegeEscalation: false
          ports:
            - containerPort: 8443
              protocol: TCP
            - containerPort: 80
              hostPort: 80
              protocol: TCP
          volumeMounts:
            - name: caddy-data
              mountPath: /data
            - name: caddyfile
              mountPath: /etc/caddy/Caddyfile
              subPath: Caddyfile
          readinessProbe:
            tcpSocket:
              port: 8443
            initialDelaySeconds: 5
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 12
          livenessProbe:
            tcpSocket:
              port: 8443
            initialDelaySeconds: 15
            periodSeconds: 30
            timeoutSeconds: 3
            failureThreshold: 3
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 500m
              memory: 128Mi
"""

_API_TLS_HIDE_MOUNT = """            - name: tls-hide
              mountPath: /etc/k7/tls
"""

_VOLUMES_FILES = """        - name: caddy-data
          hostPath:
            path: /etc/k7/caddy
            type: DirectoryOrCreate
        - name: caddyfile
          hostPath:
            path: /etc/k7/caddy
            type: Directory
        - name: tls-certs
          secret:
            secretName: k7-api-tls
        - name: tls-hide
          emptyDir: {}
"""

_VOLUMES_ACME = """        - name: caddy-data
          hostPath:
            path: /etc/k7/caddy
            type: DirectoryOrCreate
        - name: caddyfile
          hostPath:
            path: /etc/k7/caddy
            type: Directory
        - name: tls-hide
          emptyDir: {}
"""


def render_deployment(http_yaml: str, *, acme: bool) -> str:
    """Insert the Caddy sidecar and hide ``/etc/k7/tls`` from the API container."""
    if "name: caddy" in http_yaml:
        raise ValueError("deployment.yaml already has a caddy container")
    hide_at = "            - name: k3s-containerd\n              mountPath: /run/k3s/containerd\n"
    if hide_at not in http_yaml:
        raise ValueError("deployment.yaml is missing the k3s-containerd mount; cannot hide /etc/k7/tls")
    yaml = http_yaml.replace(hide_at, hide_at + _API_TLS_HIDE_MOUNT, 1)
    volumes_at = "      volumes:\n"
    if volumes_at not in yaml:
        raise ValueError("deployment.yaml is missing the volumes: block")
    sidecar = _CADDY_CONTAINER_ACME if acme else _CADDY_CONTAINER_FILES
    yaml = yaml.replace(volumes_at, sidecar + volumes_at, 1)
    extra = _VOLUMES_ACME if acme else _VOLUMES_FILES
    if not yaml.endswith("\n"):
        yaml += "\n"
    return yaml + extra


def render_service(http_yaml: str) -> str:
    if "targetPort: 8000" not in http_yaml:
        raise ValueError("service.yaml is missing targetPort: 8000")
    return http_yaml.replace("targetPort: 8000", "targetPort: 8443", 1)


def render_caddyfile(*, acme: bool, hostname: str = "", staging: bool = False) -> str:
    if acme:
        if not hostname:
            raise ValueError("ACME Caddyfile requires a hostname")
        global_block = "{\n    https_port 8443\n    http_port 80\n"
        if staging:
            global_block += "    acme_ca https://acme-staging-v02.api.letsencrypt.org/directory\n"
        global_block += "}\n"
        # :80 is ACME only — Caddy's automatic HTTPS handles the challenge
        # and does not reverse-proxy the API on HTTP.
        return f"{global_block}\n{hostname} {{\n    reverse_proxy 127.0.0.1:8000\n}}\n"
    return (
        "{\n"
        "    auto_https off\n"
        "}\n"
        "\n"
        ":8443 {\n"
        "    tls /certs/tls.crt /certs/tls.key\n"
        "    reverse_proxy 127.0.0.1:8000\n"
        "}\n"
    )


def write_tls_manifests(
    manifest_dir: Path,
    caddy_dir: Path,
    *,
    acme: bool,
    hostname: str = "",
    staging: bool = False,
) -> None:
    dep = manifest_dir / "deployment.yaml"
    svc = manifest_dir / "service.yaml"
    dep.write_text(render_deployment(dep.read_text(), acme=acme))
    svc.write_text(render_service(svc.read_text()))
    caddy_dir.mkdir(parents=True, exist_ok=True)
    (caddy_dir / "Caddyfile").write_text(render_caddyfile(acme=acme, hostname=hostname, staging=staging))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Render TLS k7-api manifests")
    p.add_argument("command", choices=["write"])
    p.add_argument("--manifest-dir", required=True)
    p.add_argument("--caddy-dir", required=True)
    p.add_argument("--mode", choices=["files", "acme"], required=True)
    p.add_argument("--hostname", default="")
    p.add_argument("--acme-staging", action="store_true")
    args = p.parse_args(argv)
    write_tls_manifests(
        Path(args.manifest_dir),
        Path(args.caddy_dir),
        acme=args.mode == "acme",
        hostname=args.hostname,
        staging=args.acme_staging,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
