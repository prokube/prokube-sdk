# SandboxV2 backend cutover runbook (strangler-fig Phase 2)

Move the SandboxV2 (Firecracker) backend out of the **pkui monolith** into the
standalone **`fc-sandbox-api`** service, with **zero consumer code change** via
**path-based ingress reroute**.

This runbook is the ordered, reversible sequence the orchestrator executes
**after** Phase 1's `fc-sandbox-api` is deployed and live-verified. Consumer-side
prep (SDK guard test, frontend note, pkui backend removal) is already staged as
un-merged branches (see "Staged branches" below). **Do not merge or switch
ingress before the new service is live** — reversing the order is a production
outage.

> Authoritative service coordinates (Service DNS, exact ingress annotations)
> come from Phase 1's `sandbox-api/CUTOVER.md`. As of this writing Phase 1 has
> published only `manifests/rbac.yaml`. Known so far:
> - ServiceAccount / namespace: `fc-sandbox-api` in `default`
> - Container port: **8083**
> - Expected Service: `fc-sandbox-api.default.svc.cluster.local:8083` (confirm)
> Fill the `<...>` placeholders from CUTOVER.md before executing.

---

## The contract being rerouted

`fc-sandbox-api` serves the **byte-identical** route surface pkui exposes today
(Phase 1 mounts the same routers at the same prefixes). Two path families:

**A. External API-key routes (prokube-sdk, api-key auth)** — clean top-level
prefix, trivial to reroute:

```
/sandboxv2/{ns}/sandboxes                         (list, create)
/sandboxv2/{ns}/sandboxes/claim                   (claim warm member)
/sandboxv2/{ns}/sandboxes/{name}                  (get, delete)
/sandboxv2/{ns}/sandboxes/{name}/wait_ready
/sandboxv2/{ns}/sandboxes/{name}/pause | resume
/sandboxv2/{ns}/sandboxes/{name}/exec
/sandboxv2/{ns}/sandboxes/{name}/files | files/batch | files/download
```

**B. Internal UI routes (pkui frontend + in-cluster SDK, header auth)** — live
**under `/api`, interleaved with pkui's other `/api` routes**. The namespace is a
**mid-path variable**, so these need a **regex** ingress rule, not a simple
prefix:

```
/api/namespaces/{ns}/sandboxv2
/api/namespaces/{ns}/sandboxv2/{name}[/pause|/resume|/exec|/events|/terminal(WS)]
/api/namespaces/{ns}/sandboxv2/{name}/files[/batch|/download]
/api/namespaces/{ns}/sandboxv2-pools[/{name}[/claim]]
/api/namespaces/{ns}/sandboxv2-nodes
/api/namespaces/{ns}/sandboxv2-storageclasses
/api/namespaces/{ns}/sandboxv2-pvcs
/api/namespaces/{ns}/sandboxv2-image-presets
```

> **Do NOT** reroute all of `/api/*` — that path also serves v1 sandbox, pods,
> volumes, etc., which must stay on pkui. Match only `sandboxv2` after the
> namespace segment, e.g. nginx regex
> `^/api/namespaces/[^/]+/sandboxv2` (covers both `/sandboxv2` and
> `/sandboxv2-*` because `-pools` etc. share the `sandboxv2` stem). The
> `/api/namespaces/{ns}/sandboxv2/{name}/terminal` route is a **WebSocket** —
> keep the WS-upgrade annotations on that ingress.

Family A reroute = one rule `^/sandboxv2/` → `fc-sandbox-api`.

### Consumer code-change assessment (evidence)

- **prokube-sdk**: same host, path-routed. api-key mode strips `api_url` to its
  origin and calls top-level `/sandboxv2/...`; in-cluster mode calls
  `/api/namespaces/{ns}/sandboxv2...` on Agent Gateway. Neither hardcodes a
  pkui-specific host. **Zero code change.** (Guard: `tests/test_sandboxv2_path_contract.py`.)
- **pkui frontend**: all v2 calls go through `apiFetch`+`namespacePath`, which
  build **same-origin relative** `${basePath}/api/namespaces/{ns}/sandboxv2...`
  (and a same-host WS for the terminal). No pkui backend base URL is hardcoded.
  **Zero code change.** The v2 frontend module *stays in pkui* and is served by
  `fc-sandbox-api` via the reroute.
- **SDK version check**: in-cluster only, hits generic `/api/version` (NOT a
  sandboxv2 path, so it stays on pkui) and is wrapped in try/except (warn-only).
  Not a blocker. `fc-sandbox-api` need not serve `/api/version`.

---

## Staged branches (all un-merged)

| Repo | Branch | Contents | Merge when |
|------|--------|----------|-----------|
| prokube-sdk | `feat/sandboxv2-cutover-prep` | this runbook + path-contract guard test | step (d), optional |
| pkui (worktree `pkui-sandboxv2`) | `docs/sandboxv2-frontend-cutover-note` | comment in `frontend/.../sandboxv2/api.ts` noting ingress reroute; **no functional change** | step (d), optional |
| pkui (worktree `pkui-sandboxv2`) | `chore/pkui-remove-sandboxv2-backend` | removes pkui-backend v2 (module + external route + wiring); **keeps v1 and the v2 frontend** | step (e), **LAST** |

---

## Ordered cutover steps (each with rollback)

### (a) Phase 1 service deployed + verified
- Apply `fc-sandbox-api` SA/RBAC/Deployment/Service (+ its own ingress manifest,
  owned by Phase 1). Confirm `GET /health` and `/api/health` return healthy and
  the pod is Ready.
- Smoke one route directly against the Service (port-forward or in-cluster curl),
  e.g. `GET /api/namespaces/<ns>/sandboxv2` and `GET /sandboxv2/<ns>/sandboxes`.
- **Rollback**: delete the new Deployment/Service. No consumer impact — nothing
  points at it yet.

### (b) Ingress switch `/sandboxv2/*` and `/api/namespaces/*/sandboxv2*` → new Service
- Add the two reroute rules (Family A prefix + Family B regex, WS annotations on
  the terminal path) pointing at `<fc-sandbox-api Service>`. Higher priority than
  pkui's catch-all `/api`.
- pkui still serves these paths too (removal is step (e)), so this is a pure
  traffic move — instantly reversible.
- **Rollback**: remove the two reroute rules; traffic falls back to pkui, which
  still serves v2. **This is the single most important rollback and stays valid
  until step (e) merges.**

### (c) Verify SDK + frontend against the new service
- **SDK (external)**: with a real api-key + `PROKUBE_API_URL=<gateway host>`, run
  create → wait_ready → exec → files → pause → resume → delete, and a pool
  claim. Confirm requests land on `fc-sandbox-api` (check its logs).
- **SDK (in-cluster)**: from an in-cluster pod against Agent Gateway, same flow.
- **Frontend**: load the SandboxesV2 + Pools pages, create/exec/pause/resume, and
  open the ttyd **terminal** (WebSocket) — verify all hit `fc-sandbox-api`.
- **Rollback**: same as (b) — pull the reroute rules.

### (d) Merge SDK / frontend prep branches (optional, low-risk)
- Merge `feat/sandboxv2-cutover-prep` (SDK) and
  `docs/sandboxv2-frontend-cutover-note` (pkui). Neither changes runtime
  behavior; they document the cutover and guard the path contract. Safe to merge
  any time after (c), or to skip.
- **Rollback**: revert the merge(s). No runtime effect.

### (e) Merge pkui v2-removal **LAST**
- Only after (c) is green and has soaked. Merge
  `chore/pkui-remove-sandboxv2-backend` and redeploy pkui. This removes pkui's
  ability to serve v2 — **after this, the ingress reroute (b) is the only path
  to v2 and step (b) rollback no longer works.**
- Verify post-deploy: pkui starts clean, v1 sandbox + all other modules work,
  and v2 still works end-to-end (now exclusively via `fc-sandbox-api`).
- **Rollback**: revert the removal merge and redeploy pkui (restores pkui's v2
  serving), OR keep the reroute and fix forward. Because this is the
  irreversible-in-practice step, treat (c) soak as the gate.

---

## Pre-flight checklist
- [ ] Phase 1 `CUTOVER.md` read; Service DNS + ingress annotations filled into `<...>`.
- [ ] New service Ready; `/health` + a direct route smoke green (a).
- [ ] Reroute rules added, both families, WS annotations on terminal (b).
- [ ] SDK external + in-cluster + frontend (incl. terminal) verified on new service (c).
- [ ] Soak window observed with no v2 errors.
- [ ] `chore/pkui-remove-sandboxv2-backend` merged + pkui redeployed; v1 + v2 verified (e).
