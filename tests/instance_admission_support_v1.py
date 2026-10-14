"""Synthetic production-shaped profile support; no real operational inputs."""

import json
from pathlib import Path

from finance_core.instance_admission import TrustedInstanceConfig


def synthetic_config(
    tmp_path: Path,
    monkeypatch,
    *,
    verifier=None,
    layout=None,
    control_root=None,
    instance_id="synthetic-instance-v1",
    application_schema=None,
) -> TrustedInstanceConfig:
    import sys

    from finance_core.profile_layout import product_root, profile_root

    actual_layout = layout or ("mac" if sys.platform == "darwin" else "linux")
    support = tmp_path / ("Application Support" if actual_layout == "mac" else "finance-codex")
    support.mkdir(mode=0o700)
    product = product_root(support, actual_layout)
    base = profile_root(support, "synthetic", actual_layout)
    if product != support:
        product.mkdir(mode=0o700)
    for directory in (
        base.parent,
        base,
        base / "runtime",
        base / "runtime/database",
        base / "workspace",
        base / "workspace/database",
        base / "backups",
        base / "work",
        base / "restore",
    ):
        directory.mkdir(mode=0o700)
    manifest = base / "profile.json"
    manifest.write_text(
        json.dumps(
            {
                "profile_id": "synthetic",
                "runtime_root": str(base / "runtime"),
                "workspace_root": str(base / "workspace"),
            }
        )
    )
    manifest.chmod(0o600)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(base / "runtime"))
    control = Path(control_root) if control_root is not None else tmp_path / "control"
    if control_root is None:
        control.mkdir(mode=0o700)
    return TrustedInstanceConfig(
        control,
        support,
        "synthetic",
        layout=actual_layout,
        instance_id=instance_id,
        application_schema=application_schema,
        config_identity="synthetic-config-v1",
        core_identity="synthetic-core-v1",
        approval_verifier=verifier or (lambda ref, action, binding: ref == "approved"),
    )
