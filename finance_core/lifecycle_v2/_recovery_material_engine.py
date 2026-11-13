"""Opt-in bounded, authenticated native closure material; no recovery authority.

Packages retain only native lifecycle history and original archived evidence.
Successful checking reports content relative to an independently pinned frontier.
It does not create an admitted instance, writer, durable remote copy or encryption.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, fields
from decimal import localcontext
from functools import lru_cache

from finance_core.instance_admission.schema_contract import (
    _deterministic_ledger,
    _json,
    _payload,
    ledger_sha256,
    validate_application_database,
)
from finance_core.instance_admission.sessions import ProductionOwner, lifecycle_read_operation
from finance_core.profile_paths import _migration_contract_digest
from finance_core.reconciliation.migrations import (
    TEMP_DB_MIGRATION_PATHS,
    apply_migration_paths,
    verify_migration_history,
)
from finance_core.resources import MIGRATION_FILENAMES, MIGRATION_LEDGER_DIGEST, _ledger_digest
from finance_core.sqlite_connection import configure_sqlite_connection

from ._recovery_rows_v1 import (
    COLUMNS,
    INSERTS,
    ClosureLimitsV1,
    NativeRowsV1,
    RecoveryMaterialError,
    decode_rows,
    exact,
    fail,
    integer,
    limit,
    limits_v1,
    owned_rows,
    read_rows,
    rows_hash,
    sha,
    strict_json,
    text,
)
from .contracts import (
    ExpectedAction,
    ExpectedDisplay,
    ExpectedInvocation,
    LifecycleError,
    RuntimeBinding,
    VerifiedAction,
    VerifiedDelivery,
    VerifiedObservation,
    VerifiedSource,
    canonical,
    digest,
)
from .effective_v1 import _business_fields
from .reader import audit_evidence_connection
from .recovery_material_v1 import (
    ClosureCheckV1,
    ClosureFrontierV1,
    ClosureIdentityV1,
    ClosureInventoryV1,
    NativeClosureV1,
    RecoveryEvidenceArchiveSourceV1,
    RecoveryEvidenceAuthenticatorV1,
    _ArtifactV1,
    _EvidenceEntryV1,
)
from .service import LedgerService, _capture_generation, _snapshot_integrity


@dataclass(frozen=True)
class _MaterialPolicy:
    """Code-owned choice between the two exact supported native formats."""

    version: int
    identity_type: type
    frontier_type: type
    inventory_type: type
    segment_type: type
    check_type: type

    @property
    def migration_count(self):
        return 61 if self.version == 1 else 62


_V1_POLICY = _MaterialPolicy(
    1, ClosureIdentityV1, ClosureFrontierV1, ClosureInventoryV1, NativeClosureV1, ClosureCheckV1
)
_HISTORICAL_CONTRACT_061 = "2066ab9bf906013f4b972554a7c49e5cf19cc3c215357113eea8302290645066"
_HISTORICAL_LEDGER_061 = "d333fff715b95938005bfb7a5bfc31ee94c29bd8c081db62b52932610e7d3850"


def _binding(value, limits):
    exact(value, RuntimeBinding.__dataclass_fields__)
    for item in value.values():
        text(item, limits, identity=True)
    if value["environment"] != "offline-synthetic":
        fail("unsupported_scope", "Only offline-synthetic binding supported")
    try:
        return RuntimeBinding(**value)
    except ValueError as exc:
        raise RecoveryMaterialError(
            "unsupported_scope", "Only offline-synthetic binding supported"
        ) from exc


def _identity(value, limits, policy=_V1_POLICY):
    exact(value, policy.identity_type.__dataclass_fields__)
    if value["format"] != f"native-closure-identity-v{policy.version}" or value["scope"] != (
        f"offline-native-personal-expense-v{policy.version}"
    ):
        fail("unsupported_scope", "Unsupported native closure scope or identity version")
    binding = _binding(value["source_binding"], limits)
    text(value["core_identity"], limits, identity=True)
    sha(value["migration_identity"])
    sha(value["migration_ledger_digest"])
    return policy.identity_type(
        binding,
        value["core_identity"],
        value["migration_identity"],
        value["migration_ledger_digest"],
    )


def _frontier(value, limits, policy=_V1_POLICY):
    exact(value, policy.frontier_type.__dataclass_fields__)
    if value["format"] != f"native-closure-frontier-v{policy.version}":
        fail("invalid_material", "Unsupported frontier version")
    sha(value["package_sha256"])
    integer(value["event_seq"])
    limit(value["event_seq"], limits.max_events)
    sha(value["event_sha256"], nullable=value["event_seq"] == 0)
    if value["event_seq"] == 0 and value["event_sha256"] is not None:
        fail("invalid_material", "Empty frontier must have a null event hash")
    return policy.frontier_type(value["package_sha256"], value["event_seq"], value["event_sha256"])


def _inventory_value(value, limits, policy=_V1_POLICY):
    exact(value, policy.inventory_type.__dataclass_fields__)
    integer(value["event_count"])
    integer(value["fact_version_count"])
    limit(value["event_count"], limits.max_events)
    limit(value["fact_version_count"], limits.max_rows)
    sha(value["event_sha256"], nullable=value["event_count"] == 0)
    if value["event_count"] == 0 and value["event_sha256"] is not None:
        fail("invalid_material", "Empty inventory must have a null event hash")
    sha(value["rows_sha256"])
    sha(value["evidence_sha256"])
    if type(value["current_facts"]) is not list:
        fail("invalid_material", "Current facts must be a canonical ordered array")
    limit(len(value["current_facts"]), limits.max_rows)
    facts = []
    for fact in value["current_facts"]:
        if type(fact) is not list or len(fact) != 3:
            fail("invalid_material", "Current fact identity is malformed")
        text(fact[0], limits, identity=True)
        integer(fact[1], minimum=1)
        sha(fact[2])
        if facts and fact[0] <= facts[-1][0]:
            fail("invalid_material", "Current fact identities must be unique and ordered")
        facts.append(tuple(fact))
    return policy.inventory_type(
        value["event_count"],
        value["event_sha256"],
        value["fact_version_count"],
        tuple(facts),
        value["rows_sha256"],
        value["evidence_sha256"],
    )


def _b64(value, limits, *, nonempty=False, authentication=False):
    if type(value) is not str:
        fail("invalid_material", "Base64 must be text")
    maximum = 65536 if authentication else limits.max_artifact_bytes
    limit(len(value), 4 * ((maximum + 2) // 3))
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RecoveryMaterialError("invalid_material", "Invalid padded base64") from exc
    limit(len(raw), maximum)
    if base64.b64encode(raw).decode("ascii") != value or (nonempty and not raw):
        fail("invalid_material", "Canonical nonempty base64 required")
    return raw


def _context(kind, value, limits):
    if kind == "source":
        if value is not None:
            fail("invalid_material", "Source evidence context must be null")
        return None
    cls = {"provider": ExpectedInvocation, "delivery": ExpectedDisplay, "action": ExpectedAction}[
        kind
    ]
    exact(value, cls.__dataclass_fields__)
    converted = dict(value)
    converted["binding"] = _binding(converted["binding"], limits)
    for field in fields(cls):
        key = field.name
        item = value[key]
        if key == "binding":
            continue
        if key == "projection":
            if type(item) is not dict:
                fail("invalid_material", "Provider projection must be an object")
        elif key == "expected_version":
            integer(item)
        elif item is None and field.default is None:
            continue
        elif key.endswith("sha256"):
            sha(item)
        else:
            text(item, limits, identity=key != "correction_reason")
    return cls(**converted)


def _value(kind, value, limits):
    cls = {
        "source": VerifiedSource,
        "provider": VerifiedObservation,
        "delivery": VerifiedDelivery,
        "action": VerifiedAction,
    }[kind]
    exact(value, cls.__dataclass_fields__)
    for key, item in value.items():
        if key == "source_timestamp":
            integer(item, minimum=1)
        elif key.endswith("sha256"):
            sha(item)
        else:
            text(item, limits, identity=key not in {"raw_input", "raw_response", "reply_text"})
    if kind == "source" and value["timezone"] != "Asia/Singapore":
        fail("unsupported_scope", "Only native source timezone is supported")
    if kind == "action" and value["action"] != "confirm":
        fail("unsupported_scope", "Only historical confirm actions are supported")
    return cls(**value)


def _entry(value, limits):
    exact(
        value,
        (
            "format",
            "kind",
            "evidence_id",
            "owner_id",
            "key_id",
            "context",
            "value",
            "artifacts",
            "authentication_b64",
        ),
    )
    if (
        value["format"] != "native-evidence-archive-v1"
        or type(value["kind"]) is not str
        or value["kind"]
        not in {
            "source",
            "provider",
            "delivery",
            "action",
        }
    ):
        fail("unsupported_scope", "Unsupported evidence archive version or kind")
    kind = value["kind"]
    for key in ("evidence_id", "owner_id", "key_id"):
        text(value[key], limits, identity=True)
    context = _context(kind, value["context"], limits)
    dto = _value(kind, value["value"], limits)
    if dto.evidence_id != value["evidence_id"]:
        fail("evidence_context_mismatch", "Archive key differs from retained DTO identity")
    if type(value["artifacts"]) is not list:
        fail("invalid_material", "Evidence artifacts must be an array")
    roles = {
        "source": {"original", "raw-envelope"},
        "provider": {"request", "response", "raw-envelope"},
        "delivery": {"display", "raw-envelope"},
        "action": {"raw-envelope"},
    }[kind]
    artifacts = []
    retained = {}
    for item in value["artifacts"]:
        exact(item, _ArtifactV1.__dataclass_fields__)
        role = item["role"]
        if type(role) is not str or role not in roles or role in retained:
            fail("invalid_material", "Artifact roles must be exact and unique")
        sha(item["sha256"])
        integer(item["size"])
        raw = _b64(item["bytes_b64"], limits, nonempty=role == "raw-envelope")
        if item["size"] != len(raw) or item["sha256"] != hashlib.sha256(raw).hexdigest():
            fail("invalid_material", "Artifact bytes differ from retained digest/size")
        if item["locator"] is not None:
            text(item["locator"], limits)
        artifacts.append(_ArtifactV1(**item))
        retained[role] = raw
    if set(retained) != roles or [a.role for a in artifacts] != sorted(roles):
        fail("missing_evidence", "Required original evidence artifacts are missing or unordered")
    if kind == "source" and (
        retained["original"] != dto.raw_input.encode("utf-8")
        or dto.original_sha256 != hashlib.sha256(retained["original"]).hexdigest()
    ):
        fail("evidence_context_mismatch", "Original source bytes differ from authenticated DTO")
    if kind == "provider" and (
        retained["request"] != canonical(asdict(context)).encode("utf-8")
        or retained["response"] != dto.raw_response.encode("utf-8")
    ):
        fail(
            "evidence_context_mismatch", "Provider acquisition bytes differ from exact context/DTO"
        )
    if (
        kind == "delivery"
        and hashlib.sha256(retained["display"]).hexdigest() != context.review_sha256
    ):
        fail("evidence_context_mismatch", "Actual display bytes differ from exact review hash")
    _b64(value["authentication_b64"], limits, nonempty=True, authentication=True)
    return _EvidenceEntryV1(
        kind,
        value["evidence_id"],
        value["owner_id"],
        value["key_id"],
        canonical(value["context"]),
        canonical(value["value"]),
        tuple(artifacts),
        value["authentication_b64"],
    )


def _entries(value, limits):
    if type(value) is not list:
        fail("invalid_material", "Evidence collection must be an array")
    limit(len(value), limits.max_evidence_entries)
    entries = tuple(_entry(item, limits) for item in value)
    keys = [(entry.kind, entry.evidence_id) for entry in entries]
    if keys != sorted(set(keys)):
        fail("invalid_material", "Evidence keys must be unique and sorted")
    limit(sum(a.size for e in entries for a in e.artifacts), limits.max_artifact_total_bytes)
    return entries


def _decode(payload: bytes, *, limits: ClosureLimitsV1 | None = None, policy=_V1_POLICY):
    """Strict structural decode only; this function does not authenticate a package."""
    limits = limits_v1(limits)
    value = strict_json(payload, limits)
    exact(value, policy.segment_type.__dataclass_fields__)
    if value["format"] != f"native-closure-segment-v{policy.version}":
        fail("unsupported_scope", "Unsupported native closure format")
    identity = _identity(value["identity"], limits, policy)
    previous = None if value["previous"] is None else _frontier(value["previous"], limits, policy)
    first = integer(value["first_event_seq"], minimum=1)
    last = integer(value["last_event_seq"])
    limit(last, limits.max_events)
    sha(value["last_event_sha256"], nullable=last == 0)
    if last == 0 and value["last_event_sha256"] is not None:
        fail("invalid_material", "Empty segment hash must be null")
    if (
        (previous is None and first != 1)
        or (previous is not None and (first != previous.event_seq + 1 or last < first))
        or (last < first and not (previous is None and first == 1 and last == 0))
    ):
        fail("chain_mismatch", "Invalid segment sequence boundaries")
    rows = decode_rows(value["rows"], limits)
    owned_rows(rows, first, last)
    evidence = _entries(value["evidence"], limits)
    if last == 0 and (evidence or any(getattr(rows, name) for name in COLUMNS)):
        fail("inventory_mismatch", "Empty genesis cannot contain rows or evidence")
    inventory = _inventory_value(value["inventory"], limits, policy)
    return policy.segment_type(
        identity, previous, first, last, value["last_event_sha256"], rows, evidence, inventory
    )


def _authenticate(entry, authenticator, limits):
    if authenticator is None or not callable(getattr(authenticator, "verify", None)):
        fail("evidence_authentication_failed", "Independent trusted authenticator is required")
    material = entry.wire()
    authentication = _b64(
        material.pop("authentication_b64"), limits, nonempty=True, authentication=True
    )
    try:
        result = authenticator.verify(
            owner_id=entry.owner_id,
            key_id=entry.key_id,
            payload=canonical(material).encode("utf-8"),
            authentication=authentication,
        )
    except Exception as exc:
        raise RecoveryMaterialError(
            "evidence_authentication_failed", "Archived evidence authentication failed"
        ) from exc
    if result is not None:
        fail(
            "evidence_authentication_failed", "Authenticator must return None on verified evidence"
        )


class _ArchivedEvidencePortV1:
    """Authenticated fixed DTO lookup only; never generates or fetches evidence."""

    def __init__(self, entries, limits):
        self._entries = {(e.kind, e.evidence_id): e for e in entries}
        self._limits = limits

    def _get(self, kind, evidence_id, expected=None):
        entry = self._entries.get((kind, evidence_id))
        if entry is None:
            fail("missing_evidence", "Exact archived evidence is unavailable")
        if canonical(asdict(expected) if expected is not None else None) != entry.context_json:
            fail(
                "evidence_context_mismatch",
                "Actual expected context differs from authenticated archive",
            )
        return _value(kind, json.loads(entry.value_json), self._limits)

    def source(self, conn, evidence_id):
        return self._get("source", evidence_id)

    def provider(self, conn, evidence_id, expected):
        return self._get("provider", evidence_id, expected)

    def delivery(self, conn, evidence_id, expected):
        return self._get("delivery", evidence_id, expected)

    def action(self, conn, evidence_id, expected):
        return self._get("action", evidence_id, expected)


def _required_evidence(rows):
    wire = rows.wire()
    required = {("source", row["evidence_id"]) for row in wire["sources"]}
    required.update(("provider", row["observation_id"]) for row in wire["proposals"])
    required.update(("delivery", row["evidence_id"]) for row in wire["deliveries"])
    required.update(("action", row["action_evidence_id"]) for row in wire["decisions"])
    for row in wire["proposals"]:
        try:
            reply = json.loads(row["observation_json"])["expected"]["reply_evidence_id"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RecoveryMaterialError(
                "invalid_material", "Proposal lacks retained exact request"
            ) from exc
        if reply is not None:
            if type(reply) is not str:
                fail("invalid_material", "Reply evidence identity must be exact text")
            required.add(("source", reply))
    return required


def _scope(conn, rows):
    if (
        conn.execute("SELECT 1 FROM l2_imports LIMIT 1").fetchone()
        or conn.execute("SELECT 1 FROM l2_archives LIMIT 1").fetchone()
    ):
        fail("unsupported_scope", "Imported and archived legacy material is unsupported")
    events = {row["entity_id"]: row for row in rows.wire()["events"] if row["kind"] == "capture"}
    for row in rows.wire()["sources"]:
        if row["source_key"].startswith("legacy:"):
            fail("unsupported_scope", "Legacy sources are unsupported")
        event = events.get(row["source_id"])
        if event is None:
            fail("inventory_mismatch", "Native source lacks capture owner")
        if _capture_generation(row, event["payload_json"]) is None:
            fail("unsupported_scope", "Historical captures without generation are unsupported")


def _trusted_paths(policy):
    names = tuple(path.name for path in TEMP_DB_MIGRATION_PATHS)
    count = policy.migration_count
    supported_counts = (61, 62) if policy.version == 1 else (62,)
    if (
        policy.version not in (1, 2)
        or names != tuple(MIGRATION_FILENAMES)
        or len(names) not in supported_counts
        or tuple(int(name[:3]) for name in names) != tuple(range(1, len(names) + 1))
    ):
        fail("identity_mismatch", "Unsupported installed native migration ledger")
    # Slicing preserves the immutable 029 preflight artifact binding.
    return TEMP_DB_MIGRATION_PATHS[:count]


def _installed_identity(policy=_V1_POLICY):
    paths = _trusted_paths(policy)
    contract = hashlib.sha256()
    for path in paths:
        contract.update(path.name.encode("utf-8"))
        contract.update(b"\0")
        contract.update(hashlib.sha256(path.read_bytes()).digest())
    migration, ledger = contract.hexdigest(), _ledger_digest(tuple(paths))
    if policy.version == 1:
        expected = (_HISTORICAL_CONTRACT_061, _HISTORICAL_LEDGER_061)
    else:
        expected = (_migration_contract_digest(), MIGRATION_LEDGER_DIGEST)
    if (migration, ledger) != expected:
        fail("identity_mismatch", "Trusted native migration resource bytes have changed")
    return migration, ledger


def _validate_installed(identity, policy=_V1_POLICY):
    migration, ledger = _installed_identity(policy)
    if identity.migration_identity != migration or identity.migration_ledger_digest != ledger:
        fail("identity_mismatch", "Closure migration identity differs from installed trusted Core")


@lru_cache(maxsize=1)
def _historical_schema_061():
    # Compile only immutable 001-061; the current schema compiler intentionally
    # compiles the complete current installation, which would be wrong here.
    conn = sqlite3.connect(":memory:")
    try:
        configure_sqlite_connection(conn)
        paths = _trusted_paths(_V1_POLICY)
        apply_migration_paths(conn, paths)
        verify_migration_history(conn, paths, require_complete=True)
        return _json(_payload(conn)), _json(_deterministic_ledger(conn))
    finally:
        conn.close()


def _schema(conn, identity, schema=None, *, isolated=True, policy=_V1_POLICY):
    _validate_installed(identity, policy)
    verify_migration_history(conn, _trusted_paths(policy), require_complete=True)
    if isolated and policy.version == 1:
        expected_schema, expected_ledger = _historical_schema_061()
        if (
            _json(_payload(conn)) != expected_schema
            or _json(_deterministic_ledger(conn)) != expected_ledger
        ):
            fail("identity_mismatch", "Historical native closure requires exact 001-061 schema")
    else:
        validate_application_database(
            conn, identity.migration_identity, schema, ledger_sha256(conn)
        )
    if isolated:
        _snapshot_integrity(conn)
    elif conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        fail("invalid_material", "Native source foreign keys are invalid")


def _audit(conn, identity, entries, limits, *, schema=None, isolated=True, policy=_V1_POLICY):
    rows = read_rows(conn, limits)
    last = len(rows.events)
    owned_rows(rows, 1, last)
    _scope(conn, rows)
    _schema(conn, identity, schema, isolated=isolated, policy=policy)
    required = _required_evidence(rows)
    keys = {(entry.kind, entry.evidence_id) for entry in entries}
    if required != keys:
        fail(
            "missing_evidence" if required - keys else "inventory_mismatch",
            "Archive keys differ from complete native evidence closure",
        )
    with localcontext() as context:
        context.prec = 1024
        try:
            for row in rows.wire()["facts"]:
                _business_fields(row["values_json"])
            audit = audit_evidence_connection(
                conn, _ArchivedEvidencePortV1(entries, limits), identity.source_binding
            )
        except LifecycleError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            # Existing readers assume nested retained JSON has the expected
            # fields and mapping methods; archive material is untrusted here.
            raise RecoveryMaterialError(
                "invalid_material", "Native closure content shape is invalid"
            ) from exc
    inventory = _inventory(conn, rows, entries, audit, policy)
    return rows, inventory


def _inventory(conn, rows, entries, audit, policy=_V1_POLICY):
    facts = tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT record_id,version,fact_sha256 FROM l2_facts f WHERE version="
            "(SELECT max(version) FROM l2_facts WHERE record_id=f.record_id) ORDER BY record_id"
        )
    )
    return policy.inventory_type(
        audit["event_count"],
        audit["audit_sha256"],
        audit["fact_version_count"],
        facts,
        rows_hash(rows),
        digest([entry.wire() for entry in entries]),
    )


def _current_values(conn):
    return tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT record_id,economic_event_id,version,fact_sha256,commit_seq,"
            "source_id,values_json "
            "FROM l2_facts f WHERE version=(SELECT max(version) FROM l2_facts "
            "WHERE record_id=f.record_id) ORDER BY record_id"
        )
    )


def _frontier_for(payload, segment, policy=_V1_POLICY):
    return policy.frontier_type(
        hashlib.sha256(payload).hexdigest(), segment.last_event_seq, segment.last_event_sha256
    )


def _packages(value, limits):
    if type(value) is not tuple or any(type(item) is not bytes for item in value):
        fail("invalid_material", "Packages must be an exact tuple of bytes")
    limit(len(value), limits.max_packages)
    limit(sum(len(item) for item in value), limits.max_chain_bytes)
    for item in value:
        limit(len(item), limits.max_package_bytes)


def _replay(conn, segment):
    _replay_rows(conn, segment.rows, segment.first_event_seq, segment.last_event_seq)


def _replay_rows(conn, rows, first, last):
    wire = owned_rows(rows, first, last)
    cursor = {name: 0 for name in COLUMNS}
    for event in wire["events"]:
        kind = event["kind"]
        names = {
            "capture": ("sources",),
            "propose": ("proposals",),
            "review": ("reviews",),
            "delivery": ("deliveries",),
            "confirm": ("facts", "decisions"),
        }[kind]
        for name in names:
            row = getattr(rows, name)[cursor[name]]
            conn.execute(INSERTS[name], row)
            cursor[name] += 1
        conn.execute(INSERTS["events"], tuple(event[key] for key in COLUMNS["events"]))


def _rebuild(
    packages, *, authenticator, expected_identity, expected_frontier, limits, policy=_V1_POLICY
):
    """Own the isolated connection; all result values are immutable and non-executable."""
    _packages(packages, limits)
    if not packages:
        fail("chain_mismatch", "A complete genesis package chain is required")
    if authenticator is None or not callable(getattr(authenticator, "verify", None)):
        fail("evidence_authentication_failed", "Independent trusted authenticator is required")
    _validate_installed(expected_identity, policy)
    conn = sqlite3.connect(":memory:")
    try:
        configure_sqlite_connection(conn)
        apply_migration_paths(conn, _trusted_paths(policy))
        entries = ()
        seen = set()
        previous = None
        total_rows = total_events = total_artifacts = 0
        rows = NativeRowsV1()
        for payload in packages:
            segment = _decode(payload, limits=limits, policy=policy)
            if segment.identity != expected_identity:
                fail(
                    "identity_mismatch",
                    "Package identity differs from independently pinned identity",
                )
            if segment.previous != previous:
                fail("chain_mismatch", "Package predecessor differs from actual complete chain")
            total_rows += sum(len(getattr(segment.rows, key)) for key in COLUMNS)
            total_events += len(segment.rows.events)
            limit(total_rows, limits.max_rows)
            limit(total_events, limits.max_events)
            for entry in segment.evidence:
                key = (entry.kind, entry.evidence_id)
                if key in seen:
                    fail("chain_mismatch", "Evidence key cannot be reintroduced in another segment")
                seen.add(key)
                _authenticate(entry, authenticator, limits)
                total_artifacts += sum(item.size for item in entry.artifacts)
            limit(len(seen), limits.max_evidence_entries)
            limit(total_artifacts, limits.max_artifact_total_bytes)
            entries = tuple(
                sorted(
                    entries + segment.evidence, key=lambda entry: (entry.kind, entry.evidence_id)
                )
            )
            conn.execute("BEGIN")
            _replay(conn, segment)
            rows, inventory = _audit(conn, expected_identity, entries, limits, policy=policy)
            if (
                inventory != segment.inventory
                or inventory.event_count != segment.last_event_seq
                or inventory.event_sha256 != segment.last_event_sha256
            ):
                fail("inventory_mismatch", "Reconstructed complete inventory differs from segment")
            conn.commit()
            previous = _frontier_for(payload, segment, policy)
        if expected_frontier is not None and previous != expected_frontier:
            fail(
                "frontier_mismatch",
                "Complete chain differs from independently pinned final frontier",
            )
        # Final proof remains separate from each segment's assertion.
        rows, inventory = _audit(conn, expected_identity, entries, limits, policy=policy)
        result = policy.check_type(expected_identity, previous, inventory, _current_values(conn))
    except RecoveryMaterialError:
        raise
    except LifecycleError as exc:
        raise RecoveryMaterialError(
            "invalid_material", "Native closure content audit failed"
        ) from exc
    except sqlite3.Error as exc:
        raise RecoveryMaterialError("invalid_material", "Fixed native row replay failed") from exc
    finally:
        try:
            if conn.in_transaction:
                conn.rollback()
        finally:
            conn.close()
    return result, rows, entries


def _check(
    packages: tuple[bytes, ...],
    *,
    authenticator: RecoveryEvidenceAuthenticatorV1,
    expected_identity: ClosureIdentityV1,
    expected_frontier: ClosureFrontierV1,
    limits: ClosureLimitsV1 | None = None,
    policy=_V1_POLICY,
):
    """Reconstruct and audit a complete chain against caller-fixed identity and tail."""
    limits = limits_v1(limits)
    if (
        type(expected_identity) is not policy.identity_type
        or type(expected_frontier) is not policy.frontier_type
    ):
        fail("invalid_material", "Exact independently fixed identity and frontier are required")
    identity = _identity(asdict(expected_identity), limits, policy)
    frontier = _frontier(asdict(expected_frontier), limits, policy)
    result, _, _ = _rebuild(
        packages,
        authenticator=authenticator,
        expected_identity=identity,
        expected_frontier=frontier,
        limits=limits,
        policy=policy,
    )
    return result


def _slice_rows(rows, first, last, limits):
    wire = rows.wire()
    events = [event for event in wire["events"] if first <= event["seq"] <= last]
    by_kind = {
        kind: {event["entity_id"] for event in events if event["kind"] == kind}
        for kind in ("capture", "propose", "review", "delivery")
    }
    operations = {event["operation_id"] for event in events if event["kind"] == "confirm"}
    result = {"events": events}
    for name, kind, key in (
        ("sources", "capture", "source_id"),
        ("proposals", "propose", "proposal_id"),
        ("reviews", "review", "review_id"),
        ("deliveries", "delivery", "review_id"),
    ):
        result[name] = [row for row in wire[name] if row[key] in by_kind[kind]]
    result["facts"] = [row for row in wire["facts"] if first <= row["commit_seq"] <= last]
    result["decisions"] = [row for row in wire["decisions"] if row["operation_id"] in operations]
    return decode_rows(result, limits)


def _collect_entries(rows, archive_source, authenticator, limits):
    required = sorted(_required_evidence(rows))
    limit(len(required), limits.max_evidence_entries)
    collected_entries = []
    total_artifacts = 0
    for kind, evidence_id in required:
        try:
            exported = archive_source.export(kind, evidence_id)
        except Exception as exc:
            raise RecoveryMaterialError(
                "missing_evidence", "Required pre-stored archive unavailable"
            ) from exc
        # Bound returned bytes before parsing or copying any artifact.
        value = strict_json(exported, limits)
        entry = _entry(value, limits)
        if (entry.kind, entry.evidence_id) != (kind, evidence_id):
            fail(
                "evidence_context_mismatch",
                "Archive export returned a different evidence key",
            )
        _authenticate(entry, authenticator, limits)
        total_artifacts += sum(artifact.size for artifact in entry.artifacts)
        limit(total_artifacts, limits.max_artifact_total_bytes)
        collected_entries.append(entry)
    return tuple(collected_entries)


def _segment_payload(
    rows, inventory, entries, identity, previous_packages, authenticator, limits, policy
):
    previous = None
    start = 1
    new_entries = entries
    if previous_packages:
        old, old_rows, old_entries = _rebuild(
            previous_packages,
            authenticator=authenticator,
            expected_identity=identity,
            expected_frontier=None,
            limits=limits,
            policy=policy,
        )
        previous = old.frontier
        if previous.event_seq > inventory.event_count or old_rows != _slice_rows(
            rows, 1, previous.event_seq, limits
        ):
            fail("chain_mismatch", "Previous chain differs from the same source read prefix")
        current = {(entry.kind, entry.evidence_id): entry for entry in entries}
        for entry in old_entries:
            if current.get((entry.kind, entry.evidence_id)) != entry:
                fail(
                    "chain_mismatch",
                    "Previous authenticated bytes differ from current source archive",
                )
        old_keys = {(entry.kind, entry.evidence_id) for entry in old_entries}
        new_entries = tuple(
            entry for entry in entries if (entry.kind, entry.evidence_id) not in old_keys
        )
        start = previous.event_seq + 1
        if start > inventory.event_count:
            fail("no_new_material", "Source has no new native lifecycle events")
    segment = policy.segment_type(
        identity,
        previous,
        start,
        inventory.event_count,
        inventory.event_sha256,
        _slice_rows(rows, start, inventory.event_count, limits),
        new_entries,
        inventory,
    )
    payload = canonical(segment._wire()).encode("utf-8")
    limit(len(payload), limits.max_package_bytes)
    limit(len(previous_packages) + 1, limits.max_packages)
    limit(sum(len(item) for item in previous_packages) + len(payload), limits.max_chain_bytes)
    # Apply the same structural contract to emitted bytes before release.
    _decode(payload, limits=limits, policy=policy)
    return payload


def _collect(
    service: LedgerService,
    *,
    archive_source: RecoveryEvidenceArchiveSourceV1,
    authenticator: RecoveryEvidenceAuthenticatorV1,
    previous_packages: tuple[bytes, ...] = (),
    limits: ClosureLimitsV1 | None = None,
    policy=_V1_POLICY,
) -> bytes:
    """Collect one append-only segment under the actual owner and close gate."""
    limits = limits_v1(limits)
    if (
        not isinstance(service, LedgerService)
        or not isinstance(service.owner, ProductionOwner)
        or type(service.binding) is not RuntimeBinding
        or service.owner.config.instance_id != service.binding.instance_id
    ):
        fail("invalid_material", "Actual matching LedgerService and production owner required")
    service.owner.assert_valid()
    if archive_source is None or not callable(getattr(archive_source, "export", None)):
        fail("missing_evidence", "Explicit pre-stored evidence archive source required")
    if authenticator is None or not callable(getattr(authenticator, "verify", None)):
        fail("evidence_authentication_failed", "Independent trusted authenticator is required")
    _packages(previous_packages, limits)
    migration, ledger = _installed_identity(policy)
    if len(TEMP_DB_MIGRATION_PATHS) != policy.migration_count:
        fail("identity_mismatch", "Current source requires collect_native_closure_v2")
    identity = _identity(
        asdict(
            policy.identity_type(
                service.binding, service.owner.config.core_identity, migration, ledger
            )
        ),
        limits,
        policy,
    )
    if service.owner.config._binding["migration_identity"] != migration:
        fail("identity_mismatch", "Source owner differs from trusted installed migration identity")
    with lifecycle_read_operation(service.owner) as conn:
        conn.execute("BEGIN")
        try:
            rows = read_rows(conn, limits)
            owned_rows(rows, 1, len(rows.events))
            _scope(conn, rows)
            _schema(
                conn,
                identity,
                service.owner.config.application_schema,
                isolated=False,
                policy=policy,
            )
            with localcontext() as context:
                context.prec = 1024
                for row in rows.wire()["facts"]:
                    _business_fields(row["values_json"])
                audit_evidence_connection(
                    conn, service.port, service.binding, service.legacy_verifier
                )
            entries = _collect_entries(rows, archive_source, authenticator, limits)
            rows, inventory = _audit(
                conn,
                identity,
                entries,
                limits,
                schema=service.owner.config.application_schema,
                isolated=False,
                policy=policy,
            )
            payload = _segment_payload(
                rows, inventory, entries, identity, previous_packages, authenticator, limits, policy
            )
        finally:
            conn.rollback()
    # Actual source operation close and cleanup must both complete first.
    return payload
