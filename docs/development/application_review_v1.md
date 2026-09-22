# Internal financial review application

`finance_core.application.review.get_proposal_review(connection, proposal_id)`
is the internal, read-only business entry for a financial proposal review.
Supply a migrated Finance SQLite connection with `sqlite3.Row` as its row
factory; the caller owns the connection. The entry reuses a caller transaction
or opens a deferred read transaction that it ends on success or refusal. It
never commits or rolls back caller-owned work. It works on a
read-only connection and does not open a workspace, create a key, start a
host, call a network/provider API, or issue an approval/posting capability.
The public `finance-core-api-v1` and Bridge envelope version are unchanged.

The existing Bridge `get_review` uses the same implementation in two stages:

1. `prepare_proposal_review`: read the effective proposal/version/content hash,
   verify AI/deterministic source lineage, validate the account and classify.
2. The adapter performs its existing deadline/key check and callback issuance.
3. `project_proposal_review`: validate remaining review fields, receipt/OCR
   ambiguity and AI flags and produce the financial review projection.
4. The adapter applies the existing AI-ambiguity token suppression and wraps
   the response, mapping neutral review exceptions to existing Bridge errors.

Both stages and the intervening adapter key/token work share one SQLite read
snapshot. A concurrent completion cannot combine old displayed fields/version
with a new content hash. Internal staged callers must use `review_snapshot`
around both stages; a prepared object must not outlive that read scope.

The ordering is intentional. A malformed account is refused before a missing
key; a malformed merchant is refused after the missing-key check. A single
upfront projection in the adapter would change this observable contract.
Direct callers use the composed entry without callbacks. A prepared review is
an internal read snapshot, not authorization and not a safe substitute for
D2's delivered-content, actor, version, expiry and one-use proof. Existing
signing/redemption/posting/recovery services continue to own that authority.

The financial rules exist once. Existing Bridge callers of proposal lookup,
effective state, ambiguity and classification delegate to the same helpers;
financial error messages and protocol mapping remain compatible. Neither the
Application nor the adapter silently repairs malformed stored content.

## Dependency guard and retained debt

Run `python scripts/check_application_dependencies.py`; the Python test suite
runs the same guard. It follows absolute/relative imports, package initializers,
explicit lazy reexports, common attribute access and literal dynamic imports.
Unresolved computed imports are rejected except the two registered compatibility
export loaders. Mutation tests cover new direct/indirect dependencies, package
initializer imports, lazy exports, dynamic imports and enlarged/stale exceptions.
A cold-process import plus real synthetic SQLite read also blocks platform
imports at runtime. Static analysis is an architecture check, not a sandbox
against arbitrary Python reflection or intentionally obfuscated code.

The two existing `intake` and `parser_proposals` compatibility export packages
load requested exports lazily. Export names, `__all__` and resolved object
identities are retained; importing a neutral submodule no longer eagerly loads
unrelated Telegram, OpenClaw or macOS adapters. Explicitly requesting a platform
export still imports that adapter and is included in dependency analysis.

[Exact exception registry](platform_dependency_exceptions_v1.json) records
existing module pairs and imported symbols, plus transitive source/target pairs.
There are no Application-to-platform exceptions. Both additions and stale
entries fail the guard; future approved removals update the exact registry and
its supporting evidence. This registry does not allow a whole business
directory to depend on a platform.

| Remaining dependency | Removal trigger / required proof |
| --- | --- |
| `posting_authority` -> human actions, delivery proof and Telegram source context | Future source/approval boundary extraction; preserve full D2 confirmation, terminal receipt, replay, atomic posting and recovery proof. |
| Proposal confirmation/conversion service and its callers -> Telegram source context | Same source/approval work; retain authenticated actor/source binding and refusal compatibility. |
| Intake compatibility exports -> Telegram transport/acquisition and macOS OCR | Adapter packaging work when required; preserve import compatibility and explicit capability/installation proof. These are lazy exported adapters, not review-entry dependencies. |

This extraction does not make the whole Core platform-independent. Broader
source/approval abstraction, business versus channel recovery, independent
component versioning, new tools/MCP and alternate hosts remain separate scoped
work. No disabled tool is enabled and no historical migration is rewritten.

## Validation

Use synthetic inputs and temporary databases. Compare text and total-receipt
review fields and token bindings; exercise missing, blank, malformed and
oversized fields, ambiguity/classification and lineage refusals, terminal
states and key-loss error ordering. Verify direct read-only use without a key
or platform module and compatibility export identities. Retain affected D2
confirmation, duplicate, posting and recovery tests; a passing projection test
cannot certify or replace those authority contracts.
