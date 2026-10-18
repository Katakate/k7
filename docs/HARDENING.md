# Hardening guide

Operator checklist for a production K7 install. `SECURITY.md` explains the
model; this page is what to configure.

## Identity: roles, keys and OIDC

K7 roles are **application roles inside k7-api, not Kubernetes RBAC**.
Nobody gets a Kubernetes identity through K7, and a K7 `admin` cannot
`kubectl` anything. Protect Kubernetes access (the server nodes'
`/etc/rancher/k3s/k3s.yaml`, your cloud IAM) separately.

| Role | Scope | Can |
|------|-------|-----|
| `read-only` | one tenant | list/get sandboxes, metrics and snapshots |
| `user` | one tenant | `read-only` plus create/delete, exec, logs, pause/resume, fork, snapshots |
| `operator` | cluster | read everything, snapshot GC, node storage, node dedication; no exec, no keys |
| `admin` | cluster | everything, including API keys and role bindings |

Checklist:

1. **Migrate keys** after upgrading: `k7 api keys migrate` on a server
   node. Re-issue every key that `k7 list-api-keys` flags
   `unscoped_legacy` with the narrowest real role
   (`k7 generate-api-key NAME --role user --tenant T`).
2. **Give machines tenant keys, give humans OIDC.** Keep `admin` keys to
   automation that needs them, with `--expires-days` set.
3. **Keep one break-glass `admin` key offline.** Keys keep working when
   the IdP is down; the server-node path (`k7 generate-api-key` with the
   node kubeconfig) always works too.
4. **Configure OIDC** with all five settings (or none):

   ```bash
   k7 install ... \
     --oidc-issuer https://idp.example.com/realms/k7 \
     --oidc-audience k7-api \
     --oidc-client-id k7-cli \
     --oidc-groups-claim groups \
     --oidc-username-claim email
   ```

   Chart: `auth.oidc.{issuer,audience,clientId,groupsClaim,usernameClaim}`
   and optional `jwksTtlSeconds` (default 3600, minimum 10; the same as
   `--oidc-jwks-ttl`). The issuer must be `https://` and k7-api must reach
   its discovery document and JWKS. A cached JWKS is trusted only until
   that TTL; if the refresh then fails, OIDC answers 503 and does not keep
   using the expired keys. When the username claim is `email`, tokens
   must carry `email_verified: true`. Register `k7-cli` as a **public**
   client with the device grant enabled (and `http://127.0.0.1` loopback
   redirects for `k7 login --browser`). Re-run `k7 install` with the same
   flags on
   every upgrade: omitting them turns OIDC off.
5. **Short tokens.** Set access-token lifetime to 1 h or less at the IdP.
   A JWT is valid until `exp` (+60 s leeway); `deny-subjects` is the
   immediate stop.
6. **Bind groups, not people**, and review the bindings:

   ```yaml
   # k7 role-bindings apply -f bindings.yaml
   bindings:
     - {group: k7-admins, role: admin}
     - {group: sre, role: operator}
     - {group: acme-devs, role: user, tenant: acme}
     - {group: acme-viewers, role: read-only, tenant: acme}
   deny_subjects: []   # OIDC `sub` or username; blocks on the next request
   ```

   A subject in several groups gets the highest role per tenant plus the
   highest cluster role. Writes that leave no `admin` binding while no
   unexpired `admin` key exists are refused.
7. **Ship the audit log** (k7-api stdout, logger `k7.audit`). Each line
   carries `principal_type`, `subject`, `role`, `tenant` and
   `idp_issuer`; never tokens, emails or groups.

### IdP claim settings

| IdP | `--oidc-audience` | `--oidc-groups-claim` | `--oidc-username-claim` | Notes |
|-----|-------------------|-----------------------|-------------------------|-------|
| Keycloak | client id, or an audience mapper value | `groups` (add the *Group Membership* mapper, full path off) | `preferred_username` or `email` | enable *OAuth 2.0 Device Authorization Grant* on the client |
| Microsoft Entra ID | `api://<app-id>` (expose an API) | `groups` (group **object ids**) | `preferred_username` | issuer `https://login.microsoftonline.com/<tenant>/v2.0`; over ~200 groups the token carries an overage link instead, which K7 does not follow: bind by `user`/`subject` or use app roles |
| Okta | the authorization server audience | `groups` (add a groups claim) | `email` | use a custom authorization server for a stable `aud` |
| Auth0 | the API identifier | a namespaced claim set by an Action, e.g. `https://k7/groups` | `email` | enable the device code grant on the application |
| Dex | the client id | `groups` | `email` | access tokens are JWTs with the ID token's claims; register the issuer path plus `/device/callback` (e.g. `/dex/device/callback`) in the public client's `redirectURIs`, or v2.46 refuses the device flow with "Unregistered redirect_uri" |

If a user gets "no role binding matches", the 403 echoes the subject,
username and groups k7-api saw; compare them with your bindings.
