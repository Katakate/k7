# Threat model

What K7 defends, against whom, and where the boundaries are.

## Identity and authorization

**Boundary.** k7-api authenticates every request (API key or OIDC JWT)
and authorizes it against one permission table (`PERMISSIONS` in
`src/k7/api/authz.py`). K7 roles are application roles, **not Kubernetes
RBAC**: the k7-api ServiceAccount is the only Kubernetes actor, so a K7
`admin` is bounded by that ServiceAccount's RBAC, and Kubernetes
credentials (server-node kubeconfig, cloud IAM) bypass K7 roles entirely.

**Assets.** API keys (hashed in Secret `k7-api-keys`), role bindings
(ConfigMap `k7-role-bindings`), the OIDC trust settings (ConfigMap
`k7-oidc`), and the tenants' sandboxes.

| Threat | Mitigation | Residual risk |
|--------|------------|---------------|
| Tenant user reaches another tenant | tenant roles resolve the namespace's `Tenant` at request time; 403 names the tenant; a namespace claimed twice is a 500, never a guess | admins and operators are cluster-wide by design |
| Privilege escalation through the API | key minting, revocation and bindings are `admin` only; a key or token cannot grant itself more; integration tests try it per role | a compromised `admin` is full K7 control |
| Forged or foreign JWT | JWKS signature check; RS256/ES256 only and matched to the key type (`none`, HS256 and key confusion are 401); `iss`, `aud`, `exp` (60 s leeway) and `sub` required | an IdP compromise is a K7 compromise for bound groups |
| Stolen JWT | short IdP lifetimes (1 h or less recommended); `deny-subjects` applies on the next request on every replica | the token works until `exp` for subjects not denied |
| Stolen refresh token | never sent to k7-api; cached 0600 on the user's machine; `k7 logout` revokes at the IdP | a stolen laptop until revocation |
| Retired or revoked IdP signing key | JWKS cache TTL (default 1 h, `--oidc-jwks-ttl` / `auth.oidc.jwksTtlSeconds`); on expiry a failed refresh is 503 and the old keys are not used | a removed key stays trusted until the TTL |
| Unverified email used as the username | when the username claim is `email`, `email_verified` must be boolean true (otherwise 401) | other username claims are not checked for it |
| Unknown-`kid` flood to exhaust the IdP | JWKS refetch at most every 10 s per replica; a failed refresh stays 503 for that window | none known |
| IdP outage | API keys keep working; a cached JWKS validates tokens until its TTL, then OIDC is 503; offline break-glass `admin` key; server-node bootstrap path | humans without keys are locked out; after the JWKS TTL, OIDC users are too until refresh succeeds |
| Lock-out by a bad binding edit | a write leaving no `admin` binding while no unexpired `admin` key exists is refused | an operator who deletes every admin key *and* binding through kubectl |
| Claim-shape confusion (names vs ids, overage) | configurable claims; the 403 for an unbound subject echoes the groups seen | Entra ID overage is not followed |
| Silent drift as routes are added | a route missing from `PERMISSIONS` answers 500 and fails a unit test | none known |
| Secrets in logs | audit lines carry subject, role, tenant and issuer; never tokens, emails or groups | IdP-side logs are out of scope |
| Legacy keys | `k7 api keys migrate` maps `admin: true` → `admin`, scoped → `user`, unscoped → `admin` + `unscoped_legacy` (flagged); keys without a role are refused | `unscoped_legacy` keys stay cluster-wide until re-issued |

**Out of scope.** Root on a K3s server (cluster-admin kubeconfig and
etcd); anyone who can read Secrets or write ConfigMaps in the system
namespace (reads key hashes, edits bindings and `k7-oidc`); the IdP
itself.
