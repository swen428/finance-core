from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "finance_core"
MIGRATION_EXECUTOR = PACKAGE_ROOT / "reconciliation" / "migrations.py"
STAGING_GUARD = PACKAGE_ROOT / "staging_guard" / "__init__.py"
EXPECTED_STAGING_AUTHORIZATION_DDL = (
    "CREATE TABLE IF NOT EXISTS _staging_authorization ("
    "token_hash TEXT NOT NULL,"
    "authorization_version INTEGER NOT NULL,"
    "db_identity TEXT NOT NULL,"
    "created_at TEXT NOT NULL"
    ")"
)

DDL_PATTERN = re.compile(
    r"(?:^|\s)(?:create\s+(?:table|index|trigger|view)|alter\s+table|"
    r"drop\s+(?:table|index|trigger|view)|reindex|vacuum)\b",
    re.IGNORECASE | re.DOTALL,
)


def test_runtime_modules_do_not_execute_schema_changing_sql() -> None:
    violations: list[str] = []
    for source_path in PACKAGE_ROOT.rglob("*.py"):
        if source_path == MIGRATION_EXECUTOR:
            continue
        violations.extend(_ddl_execution_violations(source_path))
    assert violations == []


def test_only_migration_executor_uses_executescript() -> None:
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*.py")
        if "executescript(" in path.read_text(encoding="utf-8") and path != MIGRATION_EXECUTOR
    ]
    assert offenders == []


def test_guard_detects_lowercase_and_multiline_runtime_ddl() -> None:
    assert _contains_ddl("create table records (id integer)")
    assert _contains_ddl("\n  CREATE\n  TABLE records (id integer)")
    assert _contains_ddl("/* migration-like comment */\n drop table records")
    assert not _contains_ddl("SELECT * FROM records")


def test_guard_permits_only_the_exact_staging_authorization_ddl() -> None:
    assert _ddl_execution_violations(STAGING_GUARD) == []


def test_guard_detects_additional_staging_ddl() -> None:
    source = STAGING_GUARD.read_text(encoding="utf-8")
    source += '\nconn.execute("CREATE TABLE staging_escape_hatch (id INTEGER)")\n'

    assert _ddl_execution_violations(STAGING_GUARD, source=source)


def test_guard_rejects_staging_ddl_outside_its_exact_execution_boundary() -> None:
    source = STAGING_GUARD.read_text(encoding="utf-8").replace(
        "conn.execute(_STAGING_TOKEN_DDL)",
        "conn.executescript(_STAGING_TOKEN_DDL)",
        1,
    )

    assert _ddl_execution_violations(STAGING_GUARD, source=source)


def _ddl_execution_violations(source_path: Path, *, source: str | None = None) -> list[str]:
    tree = ast.parse(source or source_path.read_text(encoding="utf-8"), filename=str(source_path))
    constants = _module_string_constants(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in {
            "execute",
            "executemany",
            "executescript",
        }:
            continue
        sql = _resolve_sql(node.args[0], constants)
        if sql is not None and _contains_ddl(sql):
            if _is_allowed_staging_authorization_ddl(source_path, node, tree, sql):
                continue
            violations.append(f"{source_path.relative_to(ROOT)}:{node.lineno}")
    return violations


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name):
                value = _literal_string(statement.value, constants)
                if value is not None:
                    constants[target.id] = value
    return constants


def _resolve_sql(node: ast.expr, constants: dict[str, str]) -> str | None:
    literal = _literal_string(node, constants)
    if literal is not None:
        return literal
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _literal_string(node: ast.expr, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue) and isinstance(value.value, ast.Name):
                formatted = constants.get(value.value.id)
                if formatted is None:
                    return None
                parts.append(formatted)
            else:
                return None
        return "".join(parts)
    return None


def _is_allowed_staging_authorization_ddl(
    source_path: Path,
    node: ast.Call,
    tree: ast.Module,
    sql: str,
) -> bool:
    if source_path != STAGING_GUARD or sql != EXPECTED_STAGING_AUTHORIZATION_DDL:
        return False
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "execute":
        return False
    if not isinstance(node.args[0], ast.Name) or node.args[0].id != "_STAGING_TOKEN_DDL":
        return False
    return _module_string_constants(tree).get("_STAGING_TOKEN_DDL") == sql


def _contains_ddl(sql: str) -> bool:
    return DDL_PATTERN.search(sql) is not None
