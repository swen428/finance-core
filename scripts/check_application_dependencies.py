"""Check explicit and transitive platform dependencies without importing Finance.

Package initializers are real dependencies. Literal lazy compatibility exports
are followed when a symbol is requested, not when an unrelated submodule is
imported. The two historical lazy loaders are the only computed import sites.
This is a source architecture guard, not a sandbox for arbitrary Python code.
"""

from __future__ import annotations

import argparse
import ast
import json
from collections import defaultdict, deque
from importlib.util import resolve_name
from pathlib import Path
from typing import Any

LAZY_PACKAGES = frozenset({"finance_core.intake", "finance_core.parser_proposals"})


def is_platform(module: str) -> bool:
    return (
        module == "finance_core.telegram_source_context"
        or module.startswith("finance_core.openclaw_staging_bridge")
        or module.startswith("finance_core.receipt_staging_runner")
        or module.startswith("finance_core.intake.telegram_")
        or module == "finance_core.intake.macos_vision_receipt_ocr"
    )


def dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return None if base is None else base + "." + node.attr
    return None


def dependency_inventory(root: Path) -> dict[str, Any]:
    files = {
        ".".join(path.relative_to(root).with_suffix("").parts).removesuffix(".__init__"): path
        for path in (root / "finance_core").rglob("*.py")
    }
    trees = {name: ast.parse(path.read_text(encoding="utf-8")) for name, path in files.items()}
    lazy: dict[str, dict[str, tuple[str, str]]] = {}
    problems: list[str] = []
    for name, tree in trees.items():
        for node in tree.body:
            if not isinstance(node, ast.Assign) or not any(
                isinstance(target, ast.Name) and target.id == "_LAZY_EXPORTS"
                for target in node.targets
            ):
                continue
            if name not in LAZY_PACKAGES:
                problems.append(f"unregistered lazy export table: {name}")
                continue
            try:
                values = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError):
                problems.append(f"nonliteral lazy export table: {name}")
                continue
            if not isinstance(values, dict) or any(
                not isinstance(key, str)
                or not isinstance(value, tuple)
                or len(value) != 2
                or any(not isinstance(part, str) for part in value)
                or value[0] not in files
                for key, value in values.items()
            ):
                problems.append(f"invalid lazy export table: {name}")
                continue
            lazy[name] = values

    graph: dict[str, set[str]] = {name: set() for name in files}
    direct: dict[tuple[str, str], set[str]] = defaultdict(set)

    def add(source: str, target: str, symbol: str, *, eager: bool = True) -> None:
        if target not in files:
            return
        if not is_platform(source) and is_platform(target):
            direct[source, target].add(symbol)
        if eager:
            graph[source].add(target)
            parts = target.split(".")
            graph[source].update(
                parent
                for index in range(1, len(parts))
                if (parent := ".".join(parts[:index])) in files
            )

    def resolve(source: str, module: str, symbol: str | None = None) -> None:
        if symbol is not None and symbol in lazy.get(module, {}):
            target, attribute = lazy[module][symbol]
            add(source, module, "<package>")
            add(source, target, attribute)
        elif symbol is not None and f"{module}.{symbol}" in files:
            add(source, f"{module}.{symbol}", "<module>")
        else:
            add(source, module, symbol or "<module>")

    for source, tree in trees.items():
        for target, symbol in lazy.get(source, {}).values():
            add(source, target, symbol, eager=False)
        aliases: dict[str, str] = {}
        # Conservatively inspect imports inside functions and conditionals too.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
                    resolve(source, alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    package = (
                        source if files[source].name == "__init__.py" else source.rsplit(".", 1)[0]
                    )
                    parts = package.split(".")
                    module = ".".join(
                        parts[: len(parts) - node.level + 1] + ([module] if module else [])
                    )
                for alias in node.names:
                    if alias.name == "*":
                        problems.append(f"wildcard import: {source} -> {module}")
                    aliases[alias.asname or alias.name] = f"{module}.{alias.name}"
                    resolve(source, module, alias.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and (name := dotted(node)):
                first, *rest = name.split(".")
                expanded = ".".join([aliases.get(first, first), *rest])
                module, _, attribute = expanded.rpartition(".")
                if attribute in lazy.get(module, {}):
                    resolve(source, module, attribute)
            if not isinstance(node, ast.Call):
                continue
            called = dotted(node.func) or ""
            first, *rest = called.split(".")
            called = ".".join([aliases.get(first, first), *rest])
            if called == "builtins.__import__":
                called = "__import__"
            if called not in {"importlib.import_module", "__import__"}:
                continue
            keywords = {item.arg: item.value for item in node.keywords}
            if None in keywords:
                problems.append(f"unresolved dynamic import arguments: {source}:{node.lineno}")
                continue
            name_arg = node.args[0] if node.args else keywords.get("name")
            if isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str):
                target = name_arg.value
                if called == "importlib.import_module" and target.startswith("."):
                    package_arg = node.args[1] if len(node.args) > 1 else keywords.get("package")
                    if isinstance(package_arg, ast.Constant) and isinstance(package_arg.value, str):
                        package = package_arg.value
                    elif isinstance(package_arg, ast.Name) and package_arg.id == "__package__":
                        package = (
                            source
                            if files[source].name == "__init__.py"
                            else source.rsplit(".", 1)[0]
                        )
                    else:
                        problems.append(
                            f"unresolved dynamic import package: {source}:{node.lineno}"
                        )
                        continue
                    try:
                        target = resolve_name(target, package)
                    except (ImportError, ValueError):
                        problems.append(f"invalid relative dynamic import: {source}:{node.lineno}")
                        continue
                if called == "__import__":
                    level_arg = node.args[4] if len(node.args) > 4 else keywords.get("level")
                    if level_arg is not None and not (
                        isinstance(level_arg, ast.Constant) and level_arg.value == 0
                    ):
                        problems.append(f"unresolved builtin import level: {source}:{node.lineno}")
                        continue
                    from_arg = node.args[3] if len(node.args) > 3 else keywords.get("fromlist")
                    if from_arg is not None:
                        try:
                            imported = ast.literal_eval(from_arg)
                        except (ValueError, TypeError, SyntaxError):
                            imported = None
                        if not isinstance(imported, (list, tuple)) or any(
                            not isinstance(item, str) or item == "*" for item in imported
                        ):
                            problems.append(f"unresolved builtin fromlist: {source}:{node.lineno}")
                            continue
                        for symbol in imported:
                            resolve(source, target, symbol)
                resolve(source, target)
            elif not (
                source in LAZY_PACKAGES
                and source in lazy
                and called == "importlib.import_module"
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "module_name"
                and not node.keywords
            ):
                problems.append(f"unresolved dynamic import: {source}:{node.lineno}")

    indirect: list[list[str]] = []
    application_paths: list[list[str]] = []
    for source in sorted(files):
        if is_platform(source):
            continue
        queue = deque([(source, [source])])
        seen: set[str] = set()
        while queue:
            target, path = queue.popleft()
            if target in seen:
                continue
            seen.add(target)
            if is_platform(target):
                indirect.append([source, target])
                if source.startswith("finance_core.application"):
                    application_paths.append(path)
                continue
            queue.extend(
                (child, [*path, child]) for child in sorted(graph[target]) if child not in seen
            )
    return {
        "direct": [
            {"source": source, "target": target, "symbols": sorted(symbols)}
            for (source, target), symbols in sorted(direct.items())
        ],
        "indirect": sorted(indirect),
        "application_platform_paths": application_paths,
        "problems": sorted(set(problems)),
    }


def check_boundaries(root: Path, exceptions: dict[str, Any]) -> list[str]:
    actual = dependency_inventory(root)
    errors = list(actual["problems"])
    if exceptions.get("schema_version") != 1:
        errors.append("unknown exception registry schema")
    for field in ("direct", "indirect"):
        if actual[field] != exceptions.get(field):
            errors.append(
                f"historical {field} dependencies changed; review exact edges, no wildcard waiver"
            )
    for path in actual["application_platform_paths"]:
        errors.append("application reaches platform: " + " -> ".join(path))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    registry = args.root / "docs/development/platform_dependency_exceptions_v1.json"
    errors = check_boundaries(args.root, json.loads(registry.read_text(encoding="utf-8")))
    for error in errors:
        print(error)
    if not errors:
        print("Application dependency boundary passed; exact historical exceptions unchanged.")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
