{{/* Image refs are always digests (values.schema.json enforces the shape). */}}
{{- define "k7.image" -}}
{{ .repository }}@{{ .digest }}
{{- end -}}

{{/* Installer backend names, playbook order; the first is the default backend. */}}
{{- define "k7.backends" -}}
{{- $b := list -}}
{{- if .Values.backends.kfd.enabled }}{{ $b = append $b "kfd" }}{{ end -}}
{{- if .Values.backends.kql.enabled }}{{ $b = append $b "kql" }}{{ end -}}
{{- if .Values.backends.k7d.enabled }}{{ $b = append $b "k7d" }}{{ end -}}
{{- if .Values.backends.k7dFc.enabled }}{{ $b = append $b "k7d-fc" }}{{ end -}}
{{- toJson $b -}}
{{- end -}}

{{- define "k7.defaultBackend" -}}
{{- $canon := dict "kfd" "kata-firecracker-devmapper" "kql" "kata-qemu-longhorn" "k7d" "k7d" "k7d-fc" "k7d-fc" -}}
{{- get $canon (first (include "k7.backends" . | fromJsonArray)) -}}
{{- end -}}

{{- define "k7.hasK7d" -}}
{{- if or .Values.backends.k7d.enabled .Values.backends.k7dFc.enabled }}true{{ end -}}
{{- end -}}

{{/* Same as roles/k7_node_prep/vars/<distro>.yml k7_containerd_socket. */}}
{{- define "k7.containerdSocket" -}}
{{- $sockets := dict "k3s" "/run/k3s/containerd/containerd.sock" "rke2" "/run/k3s/containerd/containerd.sock" "kubeadm" "/run/containerd/containerd.sock" "eks-al2023" "/run/containerd/containerd.sock" -}}
{{- get $sockets .Values.nodeInstaller.distro -}}
{{- end -}}

{{- define "k7.egressMode" -}}
{{- get (dict "cilium" "full" "kubernetes" "cidr-only" "none" "none") .Values.networkPolicy.provider -}}
{{- end -}}

{{- define "k7.tls" -}}
{{- if ne .Values.tls.mode "insecureHTTP" }}true{{ end -}}
{{- end -}}

{{/* Installer config.json: entrypoint.validate_config rejects anything else. */}}
{{- define "k7.installerConfig" -}}
{{- $n := .Values.nodeInstaller -}}
{{- $cfg := dict "distro" $n.distro "backends" (include "k7.backends" . | fromJsonArray) "diskSource" $n.diskSource "allowServerRestart" $n.allowServerRestart "wipeDisks" $n.wipeDisks -}}
{{- if $n.k7dDisksImageSize }}{{ $_ := set $cfg "k7dDisksImageSize" $n.k7dDisksImageSize }}{{ end -}}
{{- if $n.kataThinpoolPvSize }}{{ $_ := set $cfg "kataThinpoolPvSize" $n.kataThinpoolPvSize }}{{ end -}}
{{- toJson $cfg -}}
{{- end -}}

{{/* Pod spec of the install DaemonSet and the pre-delete cleanup DaemonSet. */}}
{{- define "k7.installerPodSpec" -}}
serviceAccountName: {{ .sa }}
hostPID: true
priorityClassName: system-node-critical
nodeSelector:
  k7.katakate.org/node-pool: "true"
{{- with .ctx.Values.sandboxNodes.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
containers:
  - name: {{ .name }}
    image: {{ include "k7.image" .ctx.Values.images.nodeInstaller }}
    imagePullPolicy: IfNotPresent
    args: {{ toJson .args }}
    env:
      - name: K7_NODE_NAME
        valueFrom:
          fieldRef:
            fieldPath: spec.nodeName
      - name: K7_INSTALLER_CONFIG
        value: /etc/k7-node-installer/config.json
    securityContext:
      privileged: true
    readinessProbe:
      exec:
        command: ["test", "-f", "/tmp/k7-node-installer-ready"]
      periodSeconds: 10
    volumeMounts:
      - name: host-root
        mountPath: /host
        mountPropagation: HostToContainer
      - name: config
        mountPath: /etc/k7-node-installer
        readOnly: true
    resources:
      requests:
        cpu: 100m
        memory: 256Mi
      limits:
        memory: 1Gi
volumes:
  - name: host-root
    hostPath:
      path: /
      type: Directory
  - name: config
    configMap:
      name: k7-node-installer
{{- end -}}

{{/* python -m k7.api.chart_hooks <command> as a hook Job (backoffLimit 0: fail loud, once). */}}
{{- define "k7.hookJob" -}}
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ .name }}
  namespace: {{ .ctx.Release.Namespace }}
  annotations:
    helm.sh/hook: {{ .hook }}
    helm.sh/hook-weight: {{ .weight | quote }}
    helm.sh/hook-delete-policy: before-hook-creation,hook-succeeded
spec:
  backoffLimit: 0
  activeDeadlineSeconds: {{ add .ctx.Values.hookTimeoutSeconds 120 }}
  template:
    spec:
      serviceAccountName: k7-hook
      restartPolicy: Never
      tolerations:
        - key: node-role.kubernetes.io/control-plane
          operator: Exists
          effect: NoSchedule
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: hook
          image: {{ include "k7.image" .ctx.Values.images.api }}
          imagePullPolicy: IfNotPresent
          command: ["python", "-m", "k7.api.chart_hooks", {{ .command | quote }}]
          env:
            - name: K7_HOOK_PROVIDER
              value: {{ .ctx.Values.networkPolicy.provider }}
            - name: K7_HOOK_BACKENDS
              value: {{ include "k7.backends" .ctx | fromJsonArray | join "," }}
            - name: K7_HOOK_TIMEOUT_S
              value: {{ .ctx.Values.hookTimeoutSeconds | quote }}
            - name: K7_HOOK_REMOTE_INSTALL
              value: {{ ternary "1" "0" .ctx.Values.api.remoteInstall | quote }}
            {{- include "k7.coreEnv" .ctx | nindent 12 }}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
{{- end -}}

{{/* Refusals the schema cannot express. Included by every template. */}}
{{- define "k7.validate" -}}
{{- $np := .Values.networkPolicy -}}
{{- if and (ne $np.provider "cilium") (not $np.acknowledgeNoPlatformDeny) -}}
{{- fail (printf "networkPolicy.provider=%s has no sandbox→platform deny (a Cilium CiliumClusterwideNetworkPolicy): sandboxes can reach the node, the apiserver and cloud metadata. Set networkPolicy.acknowledgeNoPlatformDeny=true to accept that, or use provider=cilium." $np.provider) -}}
{{- end -}}
{{- if and (eq $np.provider "none") (not $np.acknowledgeNoEgressPolicy) -}}
{{- fail "networkPolicy.provider=none enforces no egress policy: every create with an egress field returns 400 and sandboxes reach whatever their node reaches. Set networkPolicy.acknowledgeNoEgressPolicy=true to accept that." -}}
{{- end -}}
{{- if and $np.apiAllowCIDRs (ne $np.provider "cilium") -}}
{{- fail "networkPolicy.apiAllowCIDRs needs networkPolicy.provider=cilium (a CiliumNetworkPolicy)." -}}
{{- end -}}
{{- if and $np.apiAllowCIDRs (eq .Values.api.service.type "ClusterIP") -}}
{{- fail "networkPolicy.apiAllowCIDRs restricts external clients: it needs api.service.type NodePort or LoadBalancer" -}}
{{- end -}}
{{- if and (eq .Values.api.mode "production") (eq .Values.tls.mode "insecureHTTP") -}}
{{- fail "api.mode=production refuses tls.mode=insecureHTTP (API keys would cross the network in clear)" -}}
{{- end -}}
{{- if and (eq .Values.api.mode "production") .Values.api.remoteInstall -}}
{{- fail "api.mode=production refuses api.remoteInstall=true (k7-api would exit at startup): set api.mode=development or api.remoteInstall=false" -}}
{{- end -}}
{{- if eq (include "k7.backends" .) "[]" -}}
{{- fail "enable at least one of backends.kfd, backends.kql, backends.k7d, backends.k7dFc" -}}
{{- end -}}
{{- if and .Values.backends.kql.enabled (not (and .Values.backends.kql.storageClass .Values.backends.kql.volumeSnapshotClass)) -}}
{{- fail "backends.kql needs backends.kql.storageClass and backends.kql.volumeSnapshotClass (the chart never installs a CSI driver)" -}}
{{- end -}}
{{- if and .Values.backends.kfd.enabled (eq .Values.nodeInstaller.diskSource "loopback") -}}
{{- fail "backends.kfd needs an LVM thin pool: nodeInstaller.diskSource=loopback provisions none" -}}
{{- end -}}
{{- if and (eq .Values.tls.mode "certManager") (not .Values.tls.certManager.issuerRef.name) -}}
{{- fail "tls.mode=certManager needs tls.certManager.issuerRef.name" -}}
{{- end -}}
{{- if and (eq .Values.tls.mode "certManager") (not (or .Values.tls.certManager.dnsNames .Values.tls.certManager.ipAddresses)) -}}
{{- fail "tls.mode=certManager needs tls.certManager.dnsNames or tls.certManager.ipAddresses" -}}
{{- end -}}
{{- if and (ne .Values.api.service.type "NodePort") .Values.api.service.nodePort -}}
{{- fail "api.service.nodePort is only valid with api.service.type=NodePort" -}}
{{- end -}}
{{- $oidc := .Values.auth.oidc -}}
{{- $set := list $oidc.issuer $oidc.audience $oidc.clientId $oidc.groupsClaim $oidc.usernameClaim | compact | len -}}
{{- if and (ne $set 0) (ne $set 5) -}}
{{- fail "auth.oidc needs all of issuer, audience, clientId, groupsClaim, usernameClaim (or none)" -}}
{{- end -}}
{{- $o := .Values.observability -}}
{{- if and $o.metrics (not (and $o.scraperSelector $o.scraperSelector.namespace $o.scraperSelector.podLabels)) -}}
{{- fail "observability.metrics=true needs observability.scraperSelector.namespace and .podLabels: the only pods that may reach port 9464 (or set observability.metrics=false)" -}}
{{- end -}}
{{- if and $o.metrics (eq $np.provider "none") -}}
{{- fail "networkPolicy.provider=none cannot keep port 9464 scraper-only; set observability.metrics=false or use provider cilium or kubernetes" -}}
{{- end -}}
{{- $api := .Capabilities.APIVersions -}}
{{- if and $o.serviceMonitor (not (and ($api.Has "monitoring.coreos.com/v1/ServiceMonitor") ($api.Has "monitoring.coreos.com/v1/PodMonitor"))) -}}
{{- fail "observability.serviceMonitor=true but the cluster has no monitoring.coreos.com/v1 ServiceMonitor and PodMonitor CRDs (install the Prometheus Operator first, or set it false)" -}}
{{- end -}}
{{- if and $o.prometheusRule (not ($api.Has "monitoring.coreos.com/v1/PrometheusRule")) -}}
{{- fail "observability.prometheusRule=true but the cluster has no monitoring.coreos.com/v1 PrometheusRule CRD (install the Prometheus Operator first, or set it false)" -}}
{{- end -}}
{{- if and (or $o.serviceMonitor $o.prometheusRule) (not $o.metrics) -}}
{{- fail "observability.serviceMonitor and observability.prometheusRule need observability.metrics=true" -}}
{{- end -}}
{{- end -}}

{{/* K7_LOG_* for every k7 pod; K7_METRICS_PORT for k7-api and k7-agent. */}}
{{- define "k7.logEnv" -}}
- name: K7_LOG_FORMAT
  value: json
- name: K7_LOG_LEVEL
  value: {{ .Values.observability.logLevel }}
{{- end -}}

{{- define "k7.observabilityEnv" -}}
{{ include "k7.logEnv" . }}
- name: K7_METRICS_PORT
  value: {{ if .Values.observability.metrics }}"9464"{{ else }}"off"{{ end }}
{{- end -}}

{{/* Pod annotations for annotation-based scraping (the port stays scraper-only). */}}
{{- define "k7.scrapeAnnotations" -}}
{{- if .Values.observability.metrics }}
prometheus.io/scrape: "true"
prometheus.io/port: "9464"
prometheus.io/path: /metrics
{{- end }}
{{- end -}}

{{/* The /metrics container port. */}}
{{- define "k7.metricsPort" -}}
{{- if .Values.observability.metrics }}
- name: metrics
  containerPort: 9464
  protocol: TCP
{{- end }}
{{- end -}}

{{/* ConfigMap k7-oidc data (K7_OIDC_* env of k7-api); empty when OIDC is off. */}}
{{- define "k7.oidcData" -}}
{{- with .Values.auth.oidc -}}
{{- if .issuer -}}
K7_OIDC_ISSUER: {{ .issuer | quote }}
K7_OIDC_AUDIENCE: {{ .audience | quote }}
K7_OIDC_CLIENT_ID: {{ .clientId | quote }}
K7_OIDC_GROUPS_CLAIM: {{ .groupsClaim | quote }}
K7_OIDC_USERNAME_CLAIM: {{ .usernameClaim | quote }}
K7_OIDC_JWKS_TTL_SECONDS: {{ .jwksTtlSeconds | quote }}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* ConfigMap k7-metering's metering.json; k7-api and k7-agent validate it at start. */}}
{{- define "k7.meteringJson" -}}
{{- $m := .Values.metering -}}
{{- $interval := $m.sampleInterval -}}
{{- if kindIs "invalid" $interval -}}
{{- if eq .Values.api.mode "production" -}}
{{- fail "api.mode=production needs an explicit metering.sampleInterval (5-60 s)" -}}
{{- end -}}
{{- $interval = 15 -}}
{{- end -}}
{{- dict "sampleInterval" $interval "storageInterval" $m.storageInterval "spool" $m.spool "requireDelivery" $m.requireDelivery "prometheusUrl" $m.prometheusUrl "sinks" $m.sinks | toJson -}}
{{- end -}}

{{/* Metering config and cluster id env for k7-api and k7-agent. */}}
{{- define "k7.meteringEnv" -}}
- name: K7_METERING_CONFIG
  value: /etc/k7-metering/config/metering.json
- name: K7_CLUSTER_ID
  valueFrom:
    configMapKeyRef:
      name: k7-cluster-id
      key: id
{{- end -}}

{{/* k7-api / snapshot-gc / helm test pods share this env. */}}
{{- define "k7.coreEnv" -}}
- name: K7_SYSTEM_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
- name: K7_EGRESS_MODE
  value: {{ include "k7.egressMode" . }}
{{- if .Values.backends.kql.enabled }}
- name: K7_KQL_STORAGE_CLASS
  value: {{ .Values.backends.kql.storageClass | quote }}
- name: K7_KQL_SNAPSHOT_CLASS
  value: {{ .Values.backends.kql.volumeSnapshotClass | quote }}
{{- end }}
{{- end -}}
