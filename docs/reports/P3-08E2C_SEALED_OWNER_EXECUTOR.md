# P3-08E2C — Sealed owner executor report

Date: 2026-08-18
Scope: local pure layer only; no `main.py`, FNOS, Docker, Git, routing, persona, or send-path mutation.

## Outcome

P3-08E2C adds a fail-closed sealed executor and a one-shot completion blueprint. It
does **not** enable any production owner action:

- the production runtime collector allowlist remains empty;
- Shell has no candidate and remains hard-off;
- LivingMemory remains non-executable because its reviewed live admin/private scope
  and mutation result contract have not passed;
- artifact read/grep fake tools may be invoked only through the module-private test
  profile, but their raw bytes cannot become `SafeArtifactOutput` or `ActionOutput`
  because no same-open-object canonical pre/post identity proof issuer exists;
- the current Controller raw completion API is a bypass and must be migrated before
  any production connection.

The fake positive path therefore proves only executor mechanics. It is not a GREEN
production capability claim.

## Read-only design audit and red evidence

The initial import test failed because `core.owner_action_executor` did not exist.
That was the expected red starting point.

The design audit then found two upstream authority gaps:

1. Controller has no supported exact `(controller, request, lease)` opener. E2C uses
   a read-only exact identity audit of the Controller ledger only in this blocked
   stage. E3C must replace this dependency with a Controller-owned friend seam.
2. `OwnerActionController.complete_execution` accepts caller-supplied `status`,
   `effect_state`, `result_digest`, and `output`. An in-process caller can bypass E2C
   and the artifact secret guard and ask Controller to sign a canonical receipt.
   The E2C suite keeps a stable red-state assertion for that signature. E3C must make
   Controller consume one exact E2C blueprint and no raw completion authority.

A third output gap is intentionally not papered over: public `ActionOutput`
constructors and public `safe_text` are data shapes, not authority. E2C emits no
artifact output until a same-open-object E2A proof path can guard the complete raw
bytes. Public/copy/tampered/cross-request output must never be accepted as a substitute.

## Frozen executor contract

- Public symbols are only `OwnerActionExecutionRejected`, `OwnerActionExecutor`, and
  `OwnerExecutionBlueprint`.
- `execute(self, request, lease, draft, collector)` has no hook, event, live context,
  plugin context, tool name, generic argument mapping, output, status, or effect input.
- The exact canonical draft is opened through `_open_adapter_draft`; arguments are
  compiled only from its module-private exact typed `parameter_record`.
- The draft's exact runtime evidence is opened once through `_open_runtime_evidence`;
  execution uses its retained exact wrapper/tool object and never looks up by name or
  unwraps LivingMemory.
- Request, lease, draft, Controller ledger record, collector, binding, epoch,
  operation, parameter digest, schema digest, call budget, and retained object are
  identity/contract bound. Copy, cross-lineage, cross-collector, replay, and drift
  reject closed.
- The schema digest is rechecked and a code-owned closed Draft 2020-12 subset validator
  enforces primitive type, bool-vs-int separation, required keys, unknown keys,
  min/max, enum, pattern, array items, and nested object constraints.
- Only an async local `call` is allowed. Handoff, MCP, background mode, handler/plugin
  hook, live event, nested tool authority, and direct send are absent or rejected.
- Call budget is one. One total async deadline covers invocation and result collection.
  Zero/multiple results reject. Timeout/exception is never retried; mutation uncertainty
  maps to `EFFECT_UNKNOWN/UNKNOWN` in the closed completion helper.
- Blueprint status/effect/result/output/reasons are not public fields. The private
  one-shot opener validates exact request and lease lineage before consuming it.
- Blueprint public metadata separates `attempt_started` (the Controller lease was
  claimed) from `tool_invoked`. Private completion has two closed kinds:
  `PRE_CALL_TERMINATED = DENIED/NOT_STARTED/attempt_count=0`, and
  `EXECUTED = closed executed status/effect/attempt_count=1`. This prevents a pre-call
  hard block from being signed as a fake executed failure.
- Repr/trace contains no raw arguments, paths, tool repr, exception text, raw bytes,
  secret text, status/effect authority, or output payload. Tampered blueprints emit a
  fixed invalid representation.

## E2A independent third-round probes

The six requested redacted groups were rerun independently against E2A with
`blocker_count = 0`:

| Group | Test methods | Probe cases | Result |
|---|---:|---:|---|
| structured JSON escape and JSON/YAML separators | 2 | 4 | PASS |
| YAML block scalar | 1 | 1 | PASS |
| extended private-key header | 1 | 1 | PASS |
| UUID v6-v8 ordinary references | 1 | 3 | PASS |
| compact AstrBot target components | 1 | 2 | PASS |
| Win32 console device names | 1 | 2 | PASS |
| **Total** | **7** | **13** | **PASS** |

This confirms the guard probes themselves are green; it does not close the live
same-handle identity/TOCTOU authority gap.

## Verification

Initial red:

```text
python -m unittest astrbot_plugin_shio.tests.test_owner_action_executor -v
ImportError: cannot import name 'owner_action_executor'
```

Independent E2A probes:

```text
Ran 7 tests in 0.002s — OK (13 probe cases; blocker_count=0)
```

Executor focused:

```text
Ran 16 tests in 0.071s — OK
```

Owner-action related (`contracts`, router, adapters, collector, E2A output guard,
Controller, E2C executor):

```text
Ran 161 tests in 0.163s — OK
```

Full local suite:

```text
Ran 865 tests in 0.918s — OK
```

Compile verification:

```text
python -m compileall -q astrbot_plugin_shio/core astrbot_plugin_shio/tests — OK
```

During the concurrent Controller/router atomic migration, one intermediate run saw
the removed old `claim_route` name and one partial test-file indentation state. They
disappeared after the shared surface synchronized. E2C did not restore the legacy API
or edit those files; all final focused, related, and full runs above are green.

## Remaining hard gates

1. E3C: Controller must accept only an exact one-shot E2C blueprint, locally open it,
   and sign the receipt. Raw completion fields must be removed from the authority API.
2. E3C: Controller must provide a supported exact request/lease opener and snapshot
   integrity; E2C must stop reading Controller internals.
3. Artifact live runtime must provide trusted-root, component-lstat, same-open-handle
   pre/post proof and feed the complete bytes into E2A before any `ActionOutput` exists.
4. A reviewed immutable live source/schema/version/fingerprint manifest must be added;
   current production allowlist remains zero.
5. LivingMemory admin/private wrapper, scope, and mutation result semantics must pass;
   Shell remains hard-off.

No Git write, GitHub write, FNOS write, Docker mutation, deployment, or live capability
enablement occurred.
