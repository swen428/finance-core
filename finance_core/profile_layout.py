"""Pure recognition and derivation of the two fixed managed profile layouts.

Recognition is refusal-only. It never grants authority to a path; the trusted
locator and descriptor checks live in :mod:`finance_core.profile_paths`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

ProfileLayout = Literal["mac", "linux"]


def product_root(root: Path, layout: ProfileLayout) -> Path:
    return root / "Finance-Codex" if layout == "mac" else root


def profile_root(root: Path, profile_id: str, layout: ProfileLayout) -> Path:
    return product_root(root, layout) / "profiles" / profile_id


def workspace_layout(path: Path) -> ProfileLayout | None:
    if path.name != "workspace" or path.parent.parent.name != "profiles":
        return None
    product = path.parent.parent.parent
    if product.name == "Finance-Codex" and product.parent.name == "Application Support":
        return "mac"
    if product.name == "finance-codex":
        return "linux"
    return None


def is_fixed_staging_path(path: Path) -> bool:
    return (
        path.name == "staging.sqlite"
        and path.parent.name == "database"
        and workspace_layout(path.parent.parent) is not None
    )


def is_managed_namespace(path: Path) -> bool:
    """Recognize reserved ancestors and copies even without registration."""
    parts = path.parts
    return any(
        parts[index : index + 3] == ("Application Support", "Finance-Codex", "profiles")
        or parts[index : index + 2] == ("finance-codex", "profiles")
        for index in range(len(parts))
    )


def is_linux_managed_namespace(path: Path) -> bool:
    parts = path.parts
    return any(
        parts[index : index + 2] == ("finance-codex", "profiles") for index in range(len(parts))
    )
