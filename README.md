# Finance Core

Finance Core is the deterministic, auditable product engine used by the
Finance project. It contains monetary primitives, proposal and receipt
lifecycles, persistence and migration contracts, reconciliation, settlement,
synthetic test fixtures, and the generic Finance Bridge integration.

The repository contains no production database, credentials, personal runtime
configuration, attachments, receipts, backups, or operational evidence. All
examples are synthetic. AI and parser outputs remain proposals; deterministic
Python code owns monetary calculations and guarded finalization.

## Development

Python 3.12 is required.

```bash
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.txt
.venv/bin/pytest -q
```

The TypeScript bridge is tested separately from `plugins/finance-bridge` with
the exact Node version declared in its package metadata.

## Release identity

Every release is built from a protected commit and includes the Python wheel,
Python source distribution, generic Bridge package, `component-manifest-v1.json`,
and `SHA256SUMS`. The manifest binds the release to the exact core commit, API
contract, migration ledger, artifact names, sizes, and SHA-256 digests.

After building the three packages in one artifact directory, create the two
identity files with:

```bash
python scripts/build_release_manifest.py \
  --artifacts-dir dist/release \
  --source-root . \
  --core-version 0.1.3 \
  --core-commit <40-character-release-commit> \
  --api-contract-version finance-core-api-v1 \
  --migration-ledger-digest \
  61e7dfaa6b1d8e4ffaccb04c52fb9335d709bf82a9c8c48965138fe859b6e6f3
```

Consumers must verify the manifest and checksums before installing. They must
not follow a floating branch or tag.

Version 0.1.3 completes the bounded D1 whole-card Bridge routing flow. It
parses and renders bilingual whole cards, routes reply-bound edits through the
atomic Python authority, and preserves generation-bound actions and recovery
evidence. It does not create final financial facts or activate a runtime.

Version 0.1.2 adds the bounded D1 whole-card Bridge command authority and its
durable recovery, action-generation and decision bindings. It does not register
Telegram whole-card routing, create final financial facts or activate a runtime.

Version 0.1.1 pins pypdf 6.16.1. Its corrected character mappings can change
extracted PDF text, so new PDF evidence uses
`pypdf-6.16.1-text-extraction-v3`, propagated from the actual extractor rather
than a template's saved label. Historical evidence and fingerprints remain
unchanged; the upgrade performs no automatic reimport or database migration.

## Data boundary

Never commit real financial data or credentials. Runtime databases, receipts,
attachments, OAuth profiles, tokens, logs, evidence receipts, and backups must
remain outside this repository.

Consumers that use runtime-facing modules must set `FINANCE_RUNTIME_ROOT` to
an absolute, owner-controlled directory outside the installed package. The
core fails closed when that boundary is missing or unsafe; it never infers a
live database location from the wheel or source checkout.

## License

Apache License 2.0. See `LICENSE`, `NOTICE`, and
`THIRD_PARTY_NOTICES.md`.
