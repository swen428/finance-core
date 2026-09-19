# Contributing

For task scope, review and delivery, follow [AGENTS.md](AGENTS.md). Before a
release or consumer upgrade, follow the
[release validation contract](docs/development/release_validation_v1.md).

Use synthetic inputs only. Never add real financial data, personal runtime
configuration, credentials, local absolute paths, or production evidence.

Changes to monetary calculations, migrations, persistence, authorization,
audit evidence, or reconciliation require focused tests and independent
review. Historical migrations are immutable; add a new migration instead of
editing an existing one.
