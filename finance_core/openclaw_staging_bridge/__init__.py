"""OpenClaw/Telegram staging bridge — bounded JSON CLI boundary (slices S1–S3).

This package is the single narrow application boundary between OpenClaw and
Finance: versioned allowlisted JSON envelopes in, one bounded JSON response
out, stable exit codes, staging-only connections, and reuse of existing
public Finance service boundaries.  It performs no network I/O, holds no
credentials, and never creates final financial facts: confirm/edit/reject
persist decisions only; conversion and finalization remain separately
invoked guarded steps owned elsewhere.

Public entry point::

    .venv/bin/python -m finance_core.openclaw_staging_bridge.cli

See ``docs/design/openclaw_telegram_staging_bridge_v1.md`` and
``docs/design/openclaw_staging_bridge_s1_s3_v1.md``.
"""

from finance_core.openclaw_staging_bridge import callback_tokens, commands, envelope, errors

__all__ = [
    "callback_tokens",
    "commands",
    "envelope",
    "errors",
]
