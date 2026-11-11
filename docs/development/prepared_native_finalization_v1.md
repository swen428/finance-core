# Prepared native finalization v1 contract

`finance_core.lifecycle_v2.prepared_finalization_v1` is an opt-in, bounded
workflow for preparing and later applying one exact offline-synthetic personal
expense confirmation. It uses migration 062's local control journal. It does not
enable online posting, production activation, encryption, independent
durability, cross-machine takeover, or a complete application backup.

## Prepare and apply

Construct `PreparedFinalizationServiceV1` with the existing `LedgerService`, its
pre-registered evidence archive source, and an independently configured
authenticator. The review must already have an exact delivery, and its human
action must already be registered and retained. Preparation uses evidence
already retained by the configured port; Core does not create or infer missing
evidence.

`prepare(review_id, action_evidence_id, operation_id)` validates that exact
review, action, binding and intended finalization. It records the intent and
authenticated material in the 062 control journal, but adds no row to the
native lifecycle sources, proposals, reviews, deliveries, facts, decisions or
events. The returned `PreparedFinalizationV1` and `PREPARED_OFFLINE` status mean
that the append is pending; they are not a posted fact or confirmation. The
material includes the planned native append as a projection, not as evidence
that the source database already contains it.

While an operation is pending, all supported ordinary production-write paths
are blocked, including lifecycle writes and ordinary protected production write
sessions. Existing history remains readable. The caller must retain and supply
the exact `intent_sha256` to retrieve material, apply or cancel. Inspection is
keyed by the exact operation ID and reports its local state.

`material(operation_id, expected_intent_sha256=...)` returns the complete,
ordered `tuple[bytes, ...]` chain, including all earlier segments and the
prepared segment. `apply_offline` revalidates the original intent and appends
its fact, decision and event once. A first apply must occur inside the original
review window (`created_at <= now < expires_at`); preparation's `accepted_at`
remains the original accepted time and does not extend that deadline. A precise
retry of an already applied operation may return its original receipt after
review expiry. The receipt remains `LOCAL_POSTED`.

`cancel_offline` permanently consumes that operation and its review/action pair.
A later attempt needs a fresh proposal, review and action, subject to the
normal lifecycle rules; cancellation itself creates no native fact or event.
The journal allows at most 1,024 intents and 64 MiB of stored material payloads,
counting each unique package hash once. Material package and chain limits may be
lowered from their published maxima and cannot be raised. A new preparation that
would exceed a limit fails as a unit, without a partially committed intent or
material chain.

The states exposed by inspection are `ABSENT_LOCAL`, `PREPARED_OFFLINE`,
`APPLIED_OFFLINE` and `CANCELLED_OFFLINE`. Applied and cancelled outcomes are
permanent. If protected rollback or close cannot be confirmed, the operation
does not return a successful result; uncertain owner and connection resources
remain held, and the preparation is not treated as completed.

## Material and recovery boundary

Current migration 062 uses
`recovery_material_v2` for native closure material. Historical
[`recovery_material_v1`](native_recovery_material_v1.md) retains its exact
migration 001–061 identity and schema. The v2 material can represent the
planned native append, but excludes the 062 finalization intents, materials and
outcomes control journal. Its offline content check does not establish that the
operation was applied to a live source or authorize posting or restore
promotion.

Native closure material is not a full application backup: it omits the control
journal and unrelated application tables. A pending operation may be inspected
as pending during held restore review, but it cannot be promoted or used to
post. Neither format supplies encryption, independent storage, rollback
protection, a production key policy or takeover authority.
