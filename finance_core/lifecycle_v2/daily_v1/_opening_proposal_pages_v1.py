"""Owned pages for an initial, unresolved opening proposal on the original 061 rows."""

import json
import sqlite3
from dataclasses import asdict

from ..contracts import DailyCapacityV2, LifecycleError, canonical, digest, validate_daily_capacity
from .contracts import _pairs, exact, strict_json
from .opening_contracts_v1 import INCOMPLETE_FORMAT, OBSERVATION_FORMAT

PAGED_OBSERVATION = "finance-daily-opening-paged-observation-v1"
PAGED_PENDING = "finance-daily-opening-paged-pending-v1"
_PAGE = "finance-daily-opening-dependency-page-v1"
_MANIFEST = "finance-daily-opening-dependency-pages-v1"
_PAGE_BYTES = 64 * 1024
_CELL_BYTES = 1024 * 1024
_CUT_BYTES = 4 * 1024 * 1024


def _size(text):
    return len(text.encode("utf-8"))


def preflight(conn, added_cells=()):
    """Check retained and projected physical bytes before loading any new material."""
    try:
        row = conn.execute(
            """
            WITH paged_owners AS (
                SELECT proposal_id AS owner_id
                FROM l2_proposals
                WHERE json_extract(observation_json, '$.format') IS :observation_format
                   OR json_extract(values_json, '$.format') IS :pending_format
                UNION
                SELECT entity_id
                FROM l2_events
                WHERE kind = 'propose' AND json_extract(payload_json, '$[3].format')
                      IS :pending_format
            ), page_metadata AS (
                SELECT coalesce(length(CAST(p.observation_json AS BLOB)), 0) AS observation_bytes,
                       coalesce(length(CAST(p.values_json AS BLOB)), 0) AS values_bytes,
                       json_extract(p.observation_json, '$.format') IS NOT :observation_format
                           OR json_extract(p.values_json, '$.format') IS NOT :pending_format
                           AS mismatched_formats,
                       p.proposal_id IS NULL AS missing_proposal,
                       count(e.seq) AS event_count,
                       coalesce(max(length(CAST(e.payload_json AS BLOB))), 0) AS largest_event,
                       coalesce(sum(length(CAST(e.payload_json AS BLOB))), 0) AS event_bytes,
                       max(CASE WHEN e.seq IS NOT NULL
                                     AND length(CAST(e.payload_json AS BLOB)) <= :cell_bound
                                THEN json_array_length(e.payload_json) IS NOT 6
                                     OR json_extract(e.payload_json, '$[3].format')
                                        IS NOT :pending_format
                                ELSE 0 END) AS malformed_event
                FROM paged_owners o
                LEFT JOIN l2_proposals p ON p.proposal_id = o.owner_id
                LEFT JOIN l2_events e ON e.kind = 'propose' AND e.entity_id = o.owner_id
                GROUP BY o.owner_id
            )
            SELECT coalesce(max(observation_bytes), 0), coalesce(max(values_bytes), 0),
                   coalesce(max(largest_event), 0),
                   coalesce(sum(observation_bytes + values_bytes + event_bytes), 0),
                   coalesce(max(mismatched_formats), 0), coalesce(max(missing_proposal), 0),
                   coalesce(max(event_count <> 1), 0),
                   coalesce(max(malformed_event), 0)
            FROM page_metadata
            """,
            {
                "observation_format": PAGED_OBSERVATION,
                "pending_format": PAGED_PENDING,
                "cell_bound": _CELL_BYTES,
            },
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if str(exc) != "malformed JSON":
            raise
        raise LifecycleError("Malformed opening proposal page material") from exc

    (
        observation_bytes,
        values_bytes,
        event_bytes,
        cut_bytes,
        mismatched,
        no_proposal,
        unowned,
        malformed,
    ) = row
    added = tuple(_size(cell) for cell in added_cells)
    if (
        max((observation_bytes, *added[:1])) > 128 * 1024
        or max(values_bytes, event_bytes, *added) > _CELL_BYTES
        or cut_bytes + sum(added) > _CUT_BYTES
    ):
        raise LifecycleError("Opening proposal page material exceeds its storage budget")
    if no_proposal:
        raise LifecycleError("Paged opening propose event lacks its exact owned proposal")
    if mismatched:
        raise LifecycleError("Opening proposal page format markers differ")
    if unowned:
        raise LifecycleError("Opening proposal pages lack their exact owning propose event")
    if malformed:
        raise LifecycleError("Malformed opening proposal page material")


def _page(owner_sha, index, items):
    return {"format": _PAGE, "owner_sha256": owner_sha, "index": index, "items": items}


def encode(expected, observation, values, proposal_id, event_seq, previous_sha):
    validate_daily_capacity(expected.binding, DailyCapacityV2.SYNTHETIC_512)
    if (
        expected.instruction["evidence_id"] != expected.reply_evidence_id
        or not expected.dependencies
        or values["format"] != INCOMPLETE_FORMAT
        or values["candidate"] is None
        or not any(v.startswith("coverage:") for v in values["missing_fields"])
    ):
        raise LifecycleError("Paged opening requires an original unresolved dependency list")
    if _size(canonical(values)) > _CELL_BYTES:
        raise LifecycleError("Opening proposal page material exceeds its storage budget")
    raw_expected = asdict(expected)
    dependencies = raw_expected.pop("dependencies")
    owner = {
        "proposal_id": proposal_id,
        "proposal_version": expected.expected_proposal_version + 1,
        "observation_id": observation.evidence_id,
        "source_id": expected.source_id,
        "account_id": expected.record_id,
        "target_bundle_ref": expected.target_bundle_ref,
        "binding_sha256": digest(raw_expected["binding"]),
        "instruction_evidence_id": expected.reply_evidence_id,
        "predecessor_proposal_id": expected.current_proposal_id,
        "event_seq": event_seq,
        "previous_event_sha256": previous_sha,
    }
    owner_sha = digest(owner)
    pages, batch = [], []
    for item in dependencies:
        tentative = _page(owner_sha, len(pages), [*batch, item])
        if batch and (len(batch) == 16 or _size(canonical(tentative)) > _PAGE_BYTES):
            pages.append(_page(owner_sha, len(pages), batch))
            batch = []
        batch.append(item)
        if _size(canonical(_page(owner_sha, len(pages), batch))) > _PAGE_BYTES:
            raise LifecycleError("Opening dependency does not fit one page")
    pages.append(_page(owner_sha, len(pages), batch))
    manifest = {
        "format": _MANIFEST,
        "owner": owner,
        "count": len(dependencies),
        "closure_sha256": digest(dependencies),
        "pages": [
            {"sha256": digest(p), "count": len(p["items"]), "bytes": _size(canonical(p))}
            for p in pages
        ],
    }
    header = {
        "format": PAGED_OBSERVATION,
        "expected": {**raw_expected, "dependencies": manifest},
        "observation": asdict(observation),
    }
    stored = {
        "format": PAGED_PENDING,
        "manifest_sha256": digest(manifest),
        "pending": values,
        "pages": pages,
    }
    return header, stored


def stored_pending(proposal):
    raw = proposal["values_json"]
    if type(raw) is not str or _size(raw) > _CELL_BYTES:
        raise LifecycleError("Opening proposal page material exceeds its storage budget")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, RecursionError) as exc:
        raise LifecycleError("Invalid stored opening proposal pages") from exc
    exact(value, {"format", "manifest_sha256", "pending", "pages"}, "paged opening pending")
    if value["format"] != PAGED_PENDING or type(value["pages"]) is not list:
        raise LifecycleError("Invalid stored opening proposal pages")
    return value


def read_sealed(proposal):
    sealed = strict_json(proposal["observation_json"])
    if sealed.get("format") != PAGED_OBSERVATION:
        return sealed
    exact(sealed, {"format", "expected", "observation"}, "paged opening observation")
    manifest = sealed["expected"]["dependencies"]
    exact(manifest, {"format", "owner", "count", "closure_sha256", "pages"}, "opening page group")
    stored = stored_pending(proposal)
    pages = stored["pages"]
    if (
        manifest["format"] != _MANIFEST
        or type(manifest["pages"]) is not list
        or not 1 <= len(pages) == len(manifest["pages"]) <= 128
        or stored["manifest_sha256"] != digest(manifest)
    ):
        raise LifecycleError("Opening page group differs")
    dependencies = []
    for index, (page, ref) in enumerate(zip(pages, manifest["pages"], strict=True)):
        exact(page, {"format", "owner_sha256", "index", "items"}, "opening dependency page")
        items = page["items"]
        page_bytes = _size(canonical(page))
        if (
            type(items) is not list
            or not 1 <= len(items) <= 16
            or page_bytes > _PAGE_BYTES
            or canonical(page) != canonical(_page(digest(manifest["owner"]), index, items))
            or ref != {"sha256": digest(page), "count": len(items), "bytes": page_bytes}
        ):
            raise LifecycleError("Opening dependency page differs")
        dependencies.extend(items)
    if len(dependencies) != manifest["count"] or digest(dependencies) != manifest["closure_sha256"]:
        raise LifecycleError("Opening proposal dependency closure differs")
    return {
        **sealed,
        "format": OBSERVATION_FORMAT,
        "expected": {**sealed["expected"], "dependencies": dependencies},
    }


def read_values(proposal):
    if strict_json(proposal["observation_json"]).get("format") == PAGED_OBSERVATION:
        return stored_pending(proposal)["pending"]
    return strict_json(proposal["values_json"])
