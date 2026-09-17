"""Regression tests for the reconciliation public API barrel."""

from __future__ import annotations

import importlib

import finance_core.reconciliation as reconciliation
from finance_core.reconciliation import (
    ApplyPersistence,
    BatchApplyStateManager,
    InternalCandidate,
    ReconciliationRepository,
    ResolutionApplyRuntime,
    SQLiteBatchApplyStateManager,
    StatementAmountDirection,
    StatementCsvAdapter,
    StatementImporter,
    StatementTransaction,
    StructuredEvidencePersistence,
    build_structured_evidence,
    derive_row_public_id,
    match_date_window,
    match_statement,
)
from finance_core.reconciliation.apply import BatchApplyStateManager as ApplyBatchApplyStateManager
from finance_core.reconciliation.apply import ResolutionApplyRuntime as ApplyResolutionApplyRuntime
from finance_core.reconciliation.apply_persistence import ApplyPersistence as ModuleApplyPersistence
from finance_core.reconciliation.apply_state_persistence import (
    SQLiteBatchApplyStateManager as ModuleSQLiteBatchApplyStateManager,
)
from finance_core.reconciliation.matcher import match_statement as module_match_statement
from finance_core.reconciliation.models import (
    InternalCandidate as ModelInternalCandidate,
)
from finance_core.reconciliation.models import (
    StatementAmountDirection as ModelStatementAmountDirection,
)
from finance_core.reconciliation.models import (
    StatementTransaction as ModelStatementTransaction,
)
from finance_core.reconciliation.models import match_date_window as model_match_date_window
from finance_core.reconciliation.repository import (
    ReconciliationRepository as ModuleReconciliationRepository,
)
from finance_core.reconciliation.review_queue import (
    build_structured_evidence as module_build_structured_evidence,
)
from finance_core.reconciliation.statement_csv import (
    StatementCsvAdapter as ModuleStatementCsvAdapter,
)
from finance_core.reconciliation.statement_import import (
    StatementImporter as ModuleStatementImporter,
)
from finance_core.reconciliation.statement_import import (
    derive_row_public_id as module_derive_row_public_id,
)
from finance_core.reconciliation.structured_evidence import (
    StructuredEvidencePersistence as ModuleStructuredEvidencePersistence,
)

PUBLIC_SUBMODULES = (
    "finance_core.reconciliation.adapter",
    "finance_core.reconciliation.apply",
    "finance_core.reconciliation.apply_persistence",
    "finance_core.reconciliation.apply_state_persistence",
    "finance_core.reconciliation.demo_fixture",
    "finance_core.reconciliation.matching",
    "finance_core.reconciliation.models",
    "finance_core.reconciliation.persistence",
    "finance_core.reconciliation.repository",
    "finance_core.reconciliation.resolution",
    "finance_core.reconciliation.resolution_persistence",
    "finance_core.reconciliation.review_evidence",
    "finance_core.reconciliation.review_persistence",
    "finance_core.reconciliation.review_queue",
    "finance_core.reconciliation.review_workflow",
    "finance_core.reconciliation.run_summary",
    "finance_core.reconciliation.service",
    "finance_core.reconciliation.statement_csv",
    "finance_core.reconciliation.statement_csv_contracts",
    "finance_core.reconciliation.statement_csv_parsing",
    "finance_core.reconciliation.statement_import",
    "finance_core.reconciliation.statement_import_contracts",
    "finance_core.reconciliation.structured_evidence",
)

FEATURE_MODULES_IMPORTABLE_DIRECTLY = (
    "finance_core.reconciliation.pdf_import_run_report",
    "finance_core.reconciliation.pdf_import_run_report_cli",
    "finance_core.reconciliation.pdf_import_run_report_production_adapter",
    "finance_core.reconciliation.pdf_statement_import_run_reporting",
    "finance_core.reconciliation.pdf_statement_review_queue_fixture",
    "finance_core.reconciliation.reporting_constants",
    "finance_core.reconciliation.reporting_query_helpers",
)

BACKWARD_COMPAT_ROOT_EXPORTS = (
    "PdfImportRunReport",
    "PdfImportRunIssues",
    "PdfStatementImportRunReviewSummary",
    "PdfStatementReviewQueueFixture",
    "PdfStatementTempDbImportResult",
    "PdfStatementTemplate",
    "PdfParserTemplateFixtureResult",
)


def test_reconciliation_root_all_has_no_duplicate_or_stale_exports() -> None:
    exported_names = reconciliation.__all__

    assert len(exported_names) == len(set(exported_names))
    assert all(hasattr(reconciliation, name) for name in exported_names)
    assert all(not name.startswith("_") for name in exported_names)


def test_reconciliation_public_submodules_define_valid_public_exports() -> None:
    for module_name in PUBLIC_SUBMODULES:
        module = importlib.import_module(module_name)
        module_exports = tuple(getattr(module, "__all__", ()))

        assert len(module_exports) == len(set(module_exports))
        assert all(hasattr(module, name) for name in module_exports)


def test_feature_modules_remain_importable_without_root_barrel_promotion() -> None:
    for module_name in FEATURE_MODULES_IMPORTABLE_DIRECTLY:
        module = importlib.import_module(module_name)

        assert module is not None
        assert getattr(module, "__all__", ())

    production_adapter = importlib.import_module(
        "finance_core.reconciliation.pdf_import_run_report_production_adapter"
    )

    assert hasattr(production_adapter, "PdfImportRunReportProductionAdapterInput")
    assert "PdfImportRunReportProductionAdapterInput" not in reconciliation.__all__


def test_existing_feature_root_exports_remain_backward_compatible() -> None:
    for export_name in BACKWARD_COMPAT_ROOT_EXPORTS:
        assert export_name in reconciliation.__all__
        assert hasattr(reconciliation, export_name)


def test_reconciliation_root_exports_core_public_api_objects() -> None:
    assert InternalCandidate is ModelInternalCandidate
    assert StatementTransaction is ModelStatementTransaction
    assert StatementAmountDirection is ModelStatementAmountDirection
    assert match_statement is module_match_statement
    assert match_date_window is model_match_date_window
    assert ReconciliationRepository is ModuleReconciliationRepository
    assert StatementCsvAdapter is ModuleStatementCsvAdapter
    assert StatementImporter is ModuleStatementImporter
    assert derive_row_public_id is module_derive_row_public_id
    assert ResolutionApplyRuntime is ApplyResolutionApplyRuntime
    assert BatchApplyStateManager is ApplyBatchApplyStateManager
    assert ApplyPersistence is ModuleApplyPersistence
    assert SQLiteBatchApplyStateManager is ModuleSQLiteBatchApplyStateManager
    assert build_structured_evidence is module_build_structured_evidence
    assert StructuredEvidencePersistence is ModuleStructuredEvidencePersistence
