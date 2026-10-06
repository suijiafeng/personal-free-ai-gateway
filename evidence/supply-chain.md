# Supply chain and native LiteLLM baseline

Checked: 2026-10-06 UTC. Scope: isolated development and mocked-upstream testing. This is not approval to deploy, to enable paid services, or to send real user content.

## Selected package and provenance

- Pin: `litellm[proxy]==1.104.0`, released 2026-10-03. The official GitHub latest stable release and official PyPI stable metadata agreed; release candidates were not selected.
- GitHub: https://github.com/BerriAI/litellm/releases/tag/v1.104.0 (published `2026-10-03T22:50:14Z`, `prerelease=false`).
- PyPI: https://pypi.org/project/litellm/1.104.0/ ; machine-readable metadata https://pypi.org/pypi/litellm/1.104.0/json .
- Installed from `https://pypi.org/simple` into project `.venv` using uv and CPython 3.12.14, Linux x86_64. Package requires Python >=3.10,<3.15.
- Exact dependency snapshot: [`requirements.lock`](../requirements.lock). Full installed package/version/license-metadata inventory: [`dependency-inventory.json`](dependency-inventory.json). The lock was regenerated with uv SHA-256 artifact hashes without changing the tested package versions. The resolved set was tested only on this Linux/Python environment; artifact hashes do not authenticate publisher identity or prove a cross-platform installation works.
- PyPI manylinux x86_64 wheel: `litellm-1.104.0-cp310-abi3-manylinux_2_28_x86_64.whl`, SHA-256 from official registry metadata `b0a690d18b0d25f907d08329027ca57b03f0173a4824b89b8f91528490d10bde`. PyPI reports Trusted Publishing. This metadata does not constitute independent verification of all build inputs.
- Directly fetched the version-tagged official router, utilities, proxy and callback Python sources and compared their SHA-256 to the installed files. All four matched byte-for-byte. They were not edited.

| File under `litellm/` | SHA-256 |
|---|---|
| `router.py` | `5f700100a802aa77c74b72a63a161ea9b486891ebb8258e8448030c04e28d343` |
| `utils.py` | `86ae14eaecc554e7dedc1b807d534b7dd16f09761e39b51d096fdf178759f0eb` |
| `integrations/custom_logger.py` | `d0260a889c370eb764c05fcd315fc3eb594a2b1dd708e57a94209ad7d803303a` |
| `proxy/proxy_server.py` | `32bdd5e335da78426935c72e3c06b1417f26d23709eb180aaddbfc56819cc1e2` |

Source URLs have the form `https://raw.githubusercontent.com/BerriAI/litellm/v1.104.0/<file>`. These four comparisons are a targeted provenance check, not a whole-package reproducible-build proof.

## Security review

Reviewed all 17 published repository advisories returned by the official GitHub advisory API on this date: https://api.github.com/repos/BerriAI/litellm/security-advisories?per_page=100 . The selected 1.104.0 version is outside each listed affected range. This statement is limited to the published ranges below, and is not a claim that the release is vulnerability-free.

| Advisory | Severity | Subject | Published patched version(s) |
|---|---|---|---|
| [GHSA-7hp6-4w63-5g45](https://github.com/BerriAI/litellm/security/advisories/GHSA-7hp6-4w63-5g45) | critical | Privilege Escalation to Proxy Admin via Cross-Domain Reuse of the Salt Key | 1.100.4; 1.101.3; 1.102.2; 1.103.1; 1.104.0rc2 |
| [GHSA-3cv6-jpf6-8222](https://github.com/BerriAI/litellm/security/advisories/GHSA-3cv6-jpf6-8222) | medium | Authenticated SSRF and provider-credential exfiltration via unvalidated request-body routing parameters | 1.96.2, 1.95.1, 1.94.3, 1.93.2, 1.92.2, 1.91.5, 1.90.7, 1.89.7, 1.88.6 |
| [GHSA-4g5m-c9r5-49xf](https://github.com/BerriAI/litellm/security/advisories/GHSA-4g5m-c9r5-49xf) | low | Local file read via request-supplied OIDC file references | >= 1.83.10-stable |
| [GHSA-5jmr-gcrj-2c9q](https://github.com/BerriAI/litellm/security/advisories/GHSA-5jmr-gcrj-2c9q) | medium | Arbitrary file write via path traversal in Skills archive extraction | >= 1.83.7-stable |
| [GHSA-hx8v-g79f-8w5f](https://github.com/BerriAI/litellm/security/advisories/GHSA-hx8v-g79f-8w5f) | medium | Server-side request forgery via the `user_config` request parameter in LiteLLM Proxy | 1.83.9 |
| [GHSA-g5ff-637f-6q2m](https://github.com/BerriAI/litellm/security/advisories/GHSA-g5ff-637f-6q2m) | high | internal_user_viewer arbitrary local file read via `vertex_ai_credentials` | 1.95.0 |
| [GHSA-4xpc-pv4p-pm3w](https://github.com/BerriAI/litellm/security/advisories/GHSA-4xpc-pv4p-pm3w) | critical | Authentication Bypass via Host Header Injection | >=1.84.0 |
| [GHSA-hhww-mrg2-969h](https://github.com/BerriAI/litellm/security/advisories/GHSA-hhww-mrg2-969h) | medium | Reflected XSS in /sso/debug/callback | 1.85.0 |
| [GHSA-72m8-9m7m-h278](https://github.com/BerriAI/litellm/security/advisories/GHSA-72m8-9m7m-h278) | low | Custom Code Guardrails production endpoints bypass code safety checks | >= 1.82.0-stable |
| [GHSA-7488-6r32-c95q](https://github.com/BerriAI/litellm/security/advisories/GHSA-7488-6r32-c95q) | high | MCP Authentication Bypass via OAuth2 Passthrough Fallback | >= 1.84.0 |
| [GHSA-v4p8-mg3p-g94g](https://github.com/BerriAI/litellm/security/advisories/GHSA-v4p8-mg3p-g94g) | high | Authenticated command execution via MCP stdio test endpoints | >=1.83.7 |
| [GHSA-wxxx-gvqv-xp7p](https://github.com/BerriAI/litellm/security/advisories/GHSA-wxxx-gvqv-xp7p) | medium | Sandbox escape in custom-code guardrail | >= 1.83.10 |
| [GHSA-r75f-5x8p-qvmc](https://github.com/BerriAI/litellm/security/advisories/GHSA-r75f-5x8p-qvmc) | critical | SQL injection in Proxy API key verification | >=1.83.7 |
| [GHSA-xqmj-j6mv-4862](https://github.com/BerriAI/litellm/security/advisories/GHSA-xqmj-j6mv-4862) | high | Server-Side Template Injection in /prompts/test endpoint | >= 1.83.7 |
| [GHSA-69x8-hrgq-fjj8](https://github.com/BerriAI/litellm/security/advisories/GHSA-69x8-hrgq-fjj8) | high | Password hash exposure and pass-the-hash authentication bypass | 1.83.0 |
| [GHSA-53mr-6c8q-9789](https://github.com/BerriAI/litellm/security/advisories/GHSA-53mr-6c8q-9789) | high | Privilege escalation via unrestricted proxy configuration endpoint | 1.83.0 |
| [GHSA-jjhc-v7c2-5hh6](https://github.com/BerriAI/litellm/security/advisories/GHSA-jjhc-v7c2-5hh6) | critical | Authentication bypass via OIDC userinfo cache key collision | 1.83.0 |

The SSRF advisory GHSA-3cv6-jpf6-8222 has an affected-range field `<1.94.0` while its patch narrative lists fixes through 1.96.2 and backports; the chosen pin exceeds both. Defense in depth is still required: disable client-side credentials, reject all routing/address/credential overrides, expose only approved API endpoints, and keep administrator access private.

Historical supply-chain incident: malicious PyPI releases 1.82.7 and 1.82.8 are explicitly excluded. See [GitHub malware advisory GHSA-92x9-889m-jgmw](https://github.com/advisories/GHSA-92x9-889m-jgmw) and [upstream incident thread](https://github.com/BerriAI/litellm/issues/24518). A current GitHub/PyPI match and published advisory review are useful checks, not proof of absence of malicious changes.

Additional scan: `pip-audit -r requirements.lock --no-deps --disable-pip --cache-dir /tmp/gateway-pip-audit-cache --format json` against the default PyPI advisory service returned zero known vulnerabilities for all 124 pinned Python packages on 2026-10-06. Raw machine-readable result: [`dependency-audit.json`](dependency-audit.json). This does not scan OS packages, bundled binaries, npm dependencies, containers, undisclosed issues, or runtime configuration. Scan again on release and before upgrading.

## License boundary

- LiteLLM repository content outside `enterprise/` is MIT; the repository license explicitly excludes the enterprise directory. [Versioned license](https://github.com/BerriAI/litellm/blob/v1.104.0/LICENSE).
- The official `[proxy]` extra installs `litellm-enterprise==0.1.71` and `litellm-proxy-extras==0.4.102.post1`: this is the standard upstream mixed-license distribution. The [official feature comparison](https://docs.litellm.ai/docs/enterprise) lists this project's virtual keys, master-key authentication, fallbacks and custom hooks as OSS. No premium feature is required or enabled. Some native shared enterprise helpers execute, including no-op paths; do not claim enterprise code never executes. Installing the bundle does not grant premium entitlement, and not every transitive package is MIT.
- The complete installed metadata/license-file inventory is in `dependency-inventory.json`. Metadata classifications are not a legal opinion. The enterprise license does not explicitly clarify standard-bundle/no-op imports; seek upstream clarification if deployment or redistribution policy requires package-level clearance. This ambiguity is not evidence that the project's OSS feature set is categorically prohibited in production. Removing the declared enterprise dependency has not been tested and is not part of this baseline.

## Native hooks and routing findings (exact 1.104.0 source)

Primary references: [Custom callbacks](https://docs.litellm.ai/docs/observability/custom_callback), [Router documentation](https://docs.litellm.ai/docs/routing), [reliability documentation](https://docs.litellm.ai/docs/proxy/reliability), and the versioned source files below. Documentation can change; source and integration tests take precedence for this pin.

| Requirement | Native support / actual behavior | Extension or remaining evidence |
|---|---|---|
| Filter before priority | `CustomLogger.async_filter_deployments(...)` is invoked before order filtering in async Router selection (`router.py` around 12877–12929). | Filter all free/permission/privacy/capability/cooldown conditions here, then recheck in pre-attempt hook. |
| Before every selected deployment call | `async_pre_call_deployment_hook(kwargs, call_type)` at `custom_logger.py:276` and `utils.py:1447,1989`. Returns modified kwargs; its error propagates before provider call. | Use for hard attempt/deadline/candidate constraints. It executes before SDK cache checks too, so keep cache disabled when counting actual generations. |
| Per-attempt success | `async_post_call_success_deployment_hook(request_data,response,call_type)` at `custom_logger.py:293`. | Ordinary logger exceptions are swallowed by dispatcher; not suitable as sole fail-closed integrity check. Streaming terminal success is a separate lifecycle. |
| Per-attempt failure | `async_post_call_failure_deployment_hook(request_data,exception,call_type,fallback_depth=None)` at `custom_logger.py:303` and `utils.py:1506,2086`. | Receives shallow read-only kwargs except attempted-target bookkeeping, snapshot exception, best-effort fallback depth. Callback errors are swallowed. Pre-hook failures are outside the provider-call exception block. Does not by itself prove post-start streaming failure coverage. |
| Streaming chunks | `async_post_call_streaming_deployment_hook(request_data,response_chunk,call_type)` exists at `custom_logger.py:351`. | Needed for event commitment and missing-usage/termination checks; verify native stream path with faults. |
| Explicit priority | `litellm_params.order` (or `model_info.order`) selects lowest eligible value; Router prepares higher-order same-group fallbacks first (`router.py:7215–7272`, `utils.py:5167–5200`). | Use distinct order values, no wildcard/default/paid targets. A list position alone is insufficient. Context-window and content-policy errors skip this order fallback path. |
| Two attempts maximum | Router `num_retries=0`, `max_fallbacks=1`; default SDK `max_retries=0` is set by Router at line1112. | Explicit request `num_retries=0` bypasses retry-policy overrides. Reject consumer overrides and enforce shared per-request attempt budget because SDK/provider/client retries can stack. Disable client retries separately. |
| Timeout | Native request/deployment timeouts exist. | A monotonic end-to-end deadline shared across hooks is needed; do not equate per-attempt timeout with overall deadline. |
| Native quota/cooldown | Built-in cooldown and error handling exist. | They do not establish account-free eligibility or an authoritative shared daily quota balance. Missing/expired observations must remain unknown; durable state is a bounded extension. |
| ASGI boundary | `litellm.proxy.proxy_server.app` is a FastAPI ASGI application with native lifespan. | No `custom_middleware` YAML setting was found in this pin. Outer ASGI middleware or `app.add_middleware` can enforce endpoint/body/deadline boundaries without a LiteLLM fork. |

### Important streaming gaps

- Router chat fallback suppression tracks generated content, not all forwarded events (`router.py` around 2907–2912). A role-only event does not natively establish the specification's no-more-fallback boundary. The gateway must record any actual client-visible upstream event, including role/empty events, and refuse later attempts.
- The proxy chat stream generator emits `[DONE]` on clean iterator completion. Its error branch logs/serializes exception text. Premature EOF, provider failures and secret-bearing exception messages require fault tests and thin sanitized boundary handling; do not claim the native proxy alone satisfies AT-05 or AT-19.
- Native model response types/cost machinery can default or synthesize usage data. Never treat a populated numeric `usage` object alone as proof it came from the upstream. Preserve raw-observation provenance; use unknown when upstream final usage is absent, and label token estimates separately. Streaming cancellation cannot prove the upstream stopped billing.
- A failure hook that returns a replacement value is not enough when the native generator ignores that return; the extension must use an actually effective native hook/error mechanism and verify wire output.

## Bootstrap validated locally

- `from litellm.proxy import proxy_server` imports successfully with installed runtime dependencies. The inherited environment uses a SOCKS proxy, so `socksio==1.0.0` was added through the official `httpx[socks]` extra rather than disabling network controls.
- The `[proxy]` extra does not install Prisma Python. Added `prisma==0.15.0`; generated its native client against `litellm/proxy/schema.prisma` using `python -m prisma generate --schema ...`. Generation succeeded. Its pinned CLI is Prisma5.17.0 and expected engine commit is `393aa359c9ad4a4bb28630fb5613f9c281cde053`. Node24.19.0 was already available.
- Set `CONFIG_FILE_PATH` to absolute approved YAML before native lifespan runs. `proxy_startup_event` loads it, then sets up `DATABASE_URL`. Forward ASGI lifespan to the native app instead of replacing native startup. Unknown/weak master keys are rejected in this release; production secrets must not be copied from examples.
- A disposable PostgreSQL17.11 (`17.11-0+deb13u1`) test fixture was downloaded from the [official Debian package directory](https://deb.debian.org/debian/pool/main/p/postgresql-17/), extracted outside this repository without OS installation, and successfully started on loopback only. This fixture uses trust authentication solely for isolated local tests and is not a deployment security template.
- Applied all 189 bundled `litellm_proxy_extras` migrations successfully to the empty PostgreSQL17.11 fixture with `python -m prisma migrate deploy --schema .../litellm_proxy_extras/schema.prisma`; verified 189 finished migration rows. Database then stopped cleanly. This is a fresh-schema test, not an upgrade/rollback or key-lifecycle pass. The executor isolates loopback listeners between separate shell executions, so DB and test processes must run within one shell/session.

## Container and production release gates

The upstream release documents `ghcr.io/berriai/litellm:v1.104.0` and cosign verification using signing key from immutable commit `0112e53046018d726492c814b3644b7d376029d0`. No Docker daemon or cosign verification was available in this environment. Versioned image references, resolved immutable image digests, verified signatures and successful image startup are separate facts. Do not invent a digest or describe an unbuilt image as tested.

Before real activation: verify image digests and signatures; scan OS and bundled/npm dependencies; review the mixed-license distribution under the target deployment policy; run container/PostgreSQL migration, rollback and recovery tests; verify administrator/consumer identity separation; run the full contract/fault/security suite; record real consumer compatibility; independently confirm each real account's free eligibility, terms, privacy scope and model capability. No real provider credential or account was used in this work.
