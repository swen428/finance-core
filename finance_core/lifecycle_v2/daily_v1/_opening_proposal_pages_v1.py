"""Owned pages for an initial, unresolved opening proposal on the original 061 rows."""

import json
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
    row = conn.execute(
        "SELECT coalesce(max(length(CAST(p.observation_json AS BLOB))),0),"
        "coalesce(max(length(CAST(p.values_json AS BLOB))),0),"
        "coalesce(max(length(CAST(e.payload_json AS BLOB))),0),"
        "coalesce(sum(length(CAST(p.observation_json AS BLOB)) + "
        "length(CAST(p.values_json AS BLOB)) + length(CAST(e.payload_json AS BLOB))),0) "
        "FROM l2_proposals p LEFT JOIN l2_events e "
        "ON e.kind='propose' AND e.entity_id=p.proposal_id "
        "WHERE json_extract(p.observation_json,'$.format')=?",
        (PAGED_OBSERVATION,),
    ).fetchone()
    added = tuple(_size(cell) for cell in added_cells)
    if (
        row[0] > 131072
        or max(row[1], row[2], *added) > _CELL_BYTES
        or row[3] + sum(added) > _CUT_BYTES
    ):
        raise LifecycleError("Opening proposal page material exceeds its storage budget")


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
    strict_json(canonical(values))
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
