# Security Policy

## Supported Versions

This project is pre-1.0 (currently 0.0.1) and under active development and security hardening. Breaking changes may occur between minor versions until 1.0.0.

## Reporting a Vulnerability

If you believe you have found a security vulnerability, please email:

- security@katakate.org (preferred)
- Or open a private security advisory via GitHub (Security → Advisories → Report a vulnerability)

Please include:
- A detailed description of the issue and potential impact
- Steps to reproduce or proof-of-concept
- Affected versions/commit SHAs and environment details

We aim to acknowledge reports within 72 hours and provide a remediation plan or mitigation timeline when applicable.

## Scope and Current Model

- Nodes run K3s + Kata + Firecracker; containers run as non-root with restricted capabilities.
- API uses API keys with hashed storage and expiry; file-backed by default.
- Egress network restrictions via Kubernetes NetworkPolicies (IP-based whitelists). When egress lockdown is enabled, DNS resolution is blocked by default.
- All ingress network blocked by default to avoid default K8s pod to pod communications; this doesn't affect kubectl exec / k7 shell into sandboxes which are based on the k8s API. 

Known limitations (pre-0.1.0):

- No rate limiting or abuse protection at API layer yet.
- API key storage is local file; rotate and protect `/etc/k7/api_keys.json`.
- No domain-based egress control (planned via Cilium/FQDN policies).
- Jailer currently ignored by Kata
- Only single-node supported right now, multi-node support high on the roadmap
- We might want to get rid of the compose setup for the API and instead directly deploy the API on the K3s cluster by writing a few manifests. 
- If keeping API out-of-cluster we should rather pass to the API a dedicated RBAC restricted Kube config instead of the admin config.  

## Responsible Disclosure

Do not publicly disclose vulnerabilities before we have had a reasonable time to investigate and release fixes. We appreciate coordinated disclosure and will credit reporters unless anonymity is requested.