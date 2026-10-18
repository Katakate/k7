#!/usr/bin/env bash
# Target A as code: one RKE2 server with Cilium, Longhorn and OpenEBS LVM,
# then the k7 chart from this checkout. Runs as root on a freshly reset
# Ubuntu 24.04 node (OS on nvme0n1, nvme1n1 left raw for kfd). rke2-hetzner.md
# explains each step; this script is what runs them.
#
#   rke2-node.sh all            every step in order
#   rke2-node.sh <step>...      packages rke2 cilium longhorn openebs tls images chart test
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
CHART="$REPO/deploy/helm/k7"
export KUBECONFIG=/etc/rancher/rke2/rke2.yaml
export PATH="/var/lib/rancher/rke2/bin:$PATH"
STEPS=(packages rke2 cilium longhorn openebs tls images chart test)

RKE2_VERSION=v1.36.5+rke2r1
RKE2_INSTALL_SH_SHA256=42983c86d1da64a92061d83afb57630cedd69241989f1b0673f3db6c3d92ee6b
CILIUM_CLI_SHA256=aa25cd2542614051573008ed18adb7d697fd78e4b0345db83d1650cdaf4492de
LONGHORN_YAML_SHA256=9dcd2472b009e468424fc708802e4a3a0e1e0880e44add6035ec2426d312994a
HELM_TGZ_SHA256=fc307327959aa38ed8f9f7e66d45492bb022a66c3e5da6063958254b9767d179
DOCKER_GPG_SHA256=1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570

fetch() { # url dest sha256
    curl -fsSL -o "$2" "$1"
    echo "$3  $2" | sha256sum -c -
}
node_ip() { ip -4 -o route get 1.1.1.1 | awk '{for(i=1;i<NF;i++) if ($i=="src") print $(i+1)}'; }
wait_for() { # seconds command...
    local deadline=$((SECONDS + $1)) && shift
    until "$@" >/dev/null 2>&1; do
        (( SECONDS < deadline )) || { echo "timed out waiting for: $*" >&2; return 1; }
        sleep 5
    done
}
digest() { docker buildx imagetools inspect "localhost:5000/$1:test" --format '{{json .Manifest.Digest}}' | tr -d '"'; }

step_packages() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    # Longhorn needs iSCSI and NFS clients on every node.
    apt-get install -y -qq open-iscsi nfs-common git curl jq
    systemctl enable --now iscsid
}

step_rke2() {
    mkdir -p /etc/rancher/rke2
    cat >/etc/rancher/rke2/config.yaml <<'EOF'
cni: none
disable-kube-proxy: true
disable: [rke2-ingress-nginx, rke2-traefik]
write-kubeconfig-mode: "0600"
node-label:
  - k7.katakate.org/node-pool=true
EOF
    cat >/etc/rancher/rke2/registries.yaml <<'EOF'
mirrors:
  "localhost:5000":
    endpoint: ["http://localhost:5000"]
EOF
    fetch "https://raw.githubusercontent.com/rancher/rke2/${RKE2_VERSION}/install.sh" /tmp/rke2-install.sh "$RKE2_INSTALL_SH_SHA256"
    INSTALL_RKE2_VERSION="$RKE2_VERSION" sh /tmp/rke2-install.sh
    systemctl enable --now rke2-server
    # Host-side tooling (`k7 --core`, the integration suite) runs kubectl and crictl from PATH.
    ln -sf /var/lib/rancher/rke2/bin/kubectl /usr/local/bin/kubectl
    ln -sf /var/lib/rancher/rke2/bin/crictl /usr/local/bin/crictl
}

step_cilium() {
    fetch https://github.com/cilium/cilium-cli/releases/download/v0.19.2/cilium-linux-amd64.tar.gz /tmp/cilium-cli.tgz \
        "$CILIUM_CLI_SHA256"
    tar -xzf /tmp/cilium-cli.tgz -C /usr/local/bin cilium
    # Same flags as roles/k7_cluster/tasks/cilium.yml; no kube-proxy, so the apiserver is this node.
    cilium install --version 1.19.2 \
        --set kubeProxyReplacement=true \
        --set k8sServiceHost="$(node_ip)" \
        --set k8sServicePort=6443 \
        --set ipam.mode=kubernetes \
        --set operator.replicas=1 \
        --set MTU=1450 \
        --set socketLB.hostNamespaceOnly=true \
        --set dnsProxy.minTtl=3600 \
        --set dnsProxy.dnsRejectResponseCode=nameError
    cilium status --wait --wait-duration 10m
    kubectl wait --for=condition=Ready node --all --timeout=600s
}

step_longhorn() {
    fetch https://raw.githubusercontent.com/longhorn/longhorn/v1.10.1/deploy/longhorn.yaml /tmp/longhorn.yaml \
        "$LONGHORN_YAML_SHA256"
    sed -i 's/numberOfReplicas: "3"/numberOfReplicas: "1"/' /tmp/longhorn.yaml
    kubectl apply -f /tmp/longhorn.yaml
    wait_for 600 kubectl get crd volumesnapshotclasses.snapshot.storage.k8s.io
    wait_for 600 kubectl -n kube-system get deployment rke2-snapshot-controller
    kubectl -n kube-system rollout status deployment/rke2-snapshot-controller --timeout=300s
    kubectl -n longhorn-system rollout status daemonset/longhorn-manager --timeout=600s
    wait_for 600 kubectl get storageclass longhorn
    kubectl apply -f - <<'EOF'
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshotClass
metadata:
  name: longhorn
driver: driver.longhorn.io
deletionPolicy: Delete
parameters:
  type: snap
EOF
}

step_openebs() {
    fetch https://get.helm.sh/helm-v3.16.4-linux-amd64.tar.gz /tmp/helm.tgz "$HELM_TGZ_SHA256"
    tar -xzf /tmp/helm.tgz -C /tmp && install -m 0755 /tmp/linux-amd64/helm /usr/local/bin/helm
    helm repo add openebs-lvm https://openebs.github.io/lvm-localpv
    helm upgrade --install openebs-lvm openebs-lvm/lvm-localpv --version 1.10.1 \
        --namespace openebs --create-namespace --values - <<'EOF'
lvmPlugin:
  image: {registry: docker.io, repository: openebs/lvm-driver, tag: "1.10.1"}
lvmNode:
  kubeletDir: "/var/lib/kubelet/"
  nodeSelector:
    k7.katakate.org/backend-kata-firecracker-devmapper: "true"
crds:
  csi:
    volumeSnapshots: {enabled: false}
lvmController:
  snapshotController: {enabled: false}
storageCapacity: false
EOF
    kubectl apply -f - <<'EOF'
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: k7-docker-lvm
provisioner: local.csi.openebs.io
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: false
parameters:
  storage: "lvm"
  volgroup: "kata-vg"
  thinProvision: "yes"
EOF
}

step_tls() {
    kubectl create namespace k7-system --dry-run=client -o yaml | kubectl apply -f -
    kubectl label namespace k7-system pod-security.kubernetes.io/enforce=privileged --overwrite
    install -d -m 0755 /etc/k7/tls
    (
        cd /etc/k7/tls
        openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 30 \
            -subj "/CN=k7 validation CA" -keyout ca.key -out ca.crt
        openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=k7-api" \
            -keyout tls.key -out tls.csr
        openssl x509 -req -in tls.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 30 -out tls.crt \
            -extfile <(printf 'subjectAltName=IP:127.0.0.1,IP:%s,DNS:k7-api.k7-system.svc\n' "$(node_ip)")
        kubectl -n k7-system create secret tls k7-api-tls --cert=tls.crt --key=tls.key \
            --dry-run=client -o yaml | kubectl apply -f -
    )
}

step_images() {
    # shellcheck disable=SC1091
    . /etc/os-release
    fetch https://download.docker.com/linux/ubuntu/gpg /tmp/docker.asc "$DOCKER_GPG_SHA256"
    install -d -m 0755 /etc/apt/keyrings && install -m 0644 /tmp/docker.asc /etc/apt/keyrings/docker.asc
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        >/etc/apt/sources.list.d/docker.list
    install -d /etc/docker
    # No iptables: Docker must not touch Cilium's datapath.
    echo '{"iptables": false, "ip6tables": false, "bridge": "none"}' >/etc/docker/daemon.json
    apt-get update -qq
    apt-get install -y -qq "docker-ce=5:29.8.2-1~ubuntu.24.04~noble" "docker-ce-cli=5:29.8.2-1~ubuntu.24.04~noble" \
        "containerd.io=2.3.6-1~ubuntu.24.04~noble" "docker-buildx-plugin=0.37.1-1~ubuntu.24.04~noble"
    docker inspect k7-test-registry >/dev/null 2>&1 || docker run -d --name k7-test-registry --restart always \
        --network host -e REGISTRY_HTTP_ADDR=127.0.0.1:5000 registry:3.0.0
    docker build --network=host -f "$REPO/src/k7/api/Dockerfile.api" -t localhost:5000/k7-api:test "$REPO"
    docker build --network=host -f "$REPO/src/k7/deploy/node_installer/Dockerfile" \
        -t localhost:5000/k7-node-installer:test "$REPO"
    docker push localhost:5000/k7-api:test
    docker push localhost:5000/k7-node-installer:test
}

step_chart() {
    helm upgrade --install k7 "$CHART" -n k7-system --wait --timeout 30m \
        -f "$CHART/ci/rke2-cilium-values.yaml" \
        --set images.api.digest="$(digest k7-api)" \
        --set images.nodeInstaller.digest="$(digest k7-node-installer)"
    cat >/etc/k7/config <<'EOF'
K7_SYSTEM_NAMESPACE=k7-system
K7_EGRESS_MODE=full
K7_KQL_STORAGE_CLASS=longhorn
K7_KQL_SNAPSHOT_CLASS=longhorn
EOF
    echo "https://127.0.0.1:$(kubectl -n k7-system get svc k7-api -o jsonpath='{.spec.ports[0].nodePort}')" \
        >/etc/k7/api_endpoint
    (umask 077 && kubectl -n k7-system get secret k7-agent-token -o jsonpath='{.data.agent_token}' | base64 -d \
        >/etc/k7/agent_token)
    # helm --wait counts a one-node DaemonSet Ready at 0/1: node prep is done
    # when the installer sets node-ready to the chart version.
    kubectl wait node -l k7.katakate.org/node-pool=true --timeout=30m \
        --for=jsonpath='{.metadata.labels.k7\.katakate\.org/node-ready}'="$(helm get metadata k7 -n k7-system -o json | jq -r .appVersion)"
    kubectl -n k7-system rollout status ds/k7-agent --timeout=10m
}

step_test() {
    helm test k7 -n k7-system --timeout 30m
}

(( $# )) || { echo "usage: $0 all | <step>... (${STEPS[*]})" >&2; exit 2; }
[[ "$*" == all ]] && set -- "${STEPS[@]}"
for s in "$@"; do
    [[ " ${STEPS[*]} " == *" $s "* ]] || { echo "unknown step: $s (${STEPS[*]})" >&2; exit 2; }
done
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
for s in "$@"; do
    echo "==> rke2-node.sh: $s"
    "step_$s"
done
