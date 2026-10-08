# Third-party notices

Finance Core declares its direct Python dependencies in `pyproject.toml` and
its exact development environment in `requirements-dev.txt`. The generic
Finance Bridge declares its Node dependencies in
`plugins/finance-bridge/package.json` and `npm-shrinkwrap.json`.

Direct runtime dependencies and their declared licenses:

- `pypdf` 6.16.1 — BSD-3-Clause.
- `typing-extensions` 4.16.0 — PSF-2.0.
- `fs-ext` 2.1.1 — MIT.
- `typebox` 1.3.3 — MIT.
- `openclaw` 2026.7.1-2 is an optional peer dependency — MIT.

The optional Python `media` extra adds `Pillow` 12.3.0 (MIT-CMU) and
`pillow-heif` 1.8.0 (BSD-3-Clause). Their binary wheels include native
libraries under their own licenses; retain each installed wheel's license
files, including `pillow-heif`'s `LICENSES_bundled.txt`. This repository
declares these dependencies and does not vendor their binaries or source.

Direct build and test dependencies include `@openclaw/ai` (MIT),
`@types/node` (MIT), `openclaw` through the `openclaw-sdk` npm alias (MIT),
and TypeScript (Apache-2.0). Transitive packages and the complete resolved
inventory are recorded in the two lock files. Third-party packages remain
under their respective licenses. No third-party source patch or vendored
OpenClaw runtime is included in this repository.
