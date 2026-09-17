"""B5.1a/B5.1b/B5.1c staging receipt runner foundation, intake, and finalize.

Provides a minimal, deterministic, staging-only runner that can:

- create an external disposable workspace with a migrated staging database;
- bootstrap participant reference data from a versioned manifest;
- reopen and verify a staging database after process restart;
- recover a persisted receipt finalization authorization from durable truth;
- emit bounded, versioned recovery evidence;
- accept real local JPEG/PNG receipt images via safe copy;
- invoke the merged macOS Vision OCR boundary;
- persist truthful local source evidence and produce a proposal;
- resume a durably authorized receipt in a new process and finalize it exactly
  once through the existing guarded receipt-finalization boundary.

Each lifecycle stage (intake, confirm, convert, fact-set, prepare, authorize,
resume, finalize) is a separate explicit human CLI action.  Finalization is
performed only through the guarded ``finalize_runner_run`` boundary.  This
package never accesses ``database/finance.db``.

See ``docs/design/b5_1a_receipt_staging_runner_foundation_v1.md``,
``docs/design/b5_1b_real_local_intake_v1.md``, and
``docs/design/b5_1c_resume_and_finalize_v1.md``.
"""

from finance_core.receipt_staging_runner.local_intake import (
    LocalIntakeCopyError as LocalIntakeCopyError,
)
from finance_core.receipt_staging_runner.local_intake import (
    LocalIntakeError as LocalIntakeError,
)
from finance_core.receipt_staging_runner.local_intake import (
    LocalIntakeEvidenceError as LocalIntakeEvidenceError,
)
from finance_core.receipt_staging_runner.local_intake import (
    LocalIntakeFileError as LocalIntakeFileError,
)
from finance_core.receipt_staging_runner.local_intake import (
    LocalIntakePersonalOnlyError as LocalIntakePersonalOnlyError,
)
from finance_core.receipt_staging_runner.local_intake import (
    LocalIntakePipelineError as LocalIntakePipelineError,
)
from finance_core.receipt_staging_runner.local_intake import (
    LocalIntakeResult as LocalIntakeResult,
)
from finance_core.receipt_staging_runner.local_intake import (
    LocalLineageError as LocalLineageError,
)
from finance_core.receipt_staging_runner.local_intake import (
    import_local_receipt_file as import_local_receipt_file,
)
from finance_core.receipt_staging_runner.local_intake import (
    persist_local_attachment_evidence as persist_local_attachment_evidence,
)
from finance_core.receipt_staging_runner.local_intake import (
    require_local_runner_receipt as require_local_runner_receipt,
)
from finance_core.receipt_staging_runner.local_intake import (
    require_local_runner_receipt_proposal as require_local_runner_receipt_proposal,
)
from finance_core.receipt_staging_runner.local_intake import (
    run_local_receipt_intake as run_local_receipt_intake,
)
from finance_core.receipt_staging_runner.local_intake import (
    validate_personal_conversion_command as validate_personal_conversion_command,
)
from finance_core.receipt_staging_runner.local_intake import (
    validate_personal_fact_set_command as validate_personal_fact_set_command,
)
from finance_core.receipt_staging_runner.models import (
    RUN_MANIFEST_SCHEMA_VERSION as RUN_MANIFEST_SCHEMA_VERSION,
)
from finance_core.receipt_staging_runner.models import (
    ParticipantBootstrapResult as ParticipantBootstrapResult,
)
from finance_core.receipt_staging_runner.models import (
    ParticipantDefinition as ParticipantDefinition,
)
from finance_core.receipt_staging_runner.models import (
    RecoveryEvidence as RecoveryEvidence,
)
from finance_core.receipt_staging_runner.models import (
    RunManifest as RunManifest,
)
from finance_core.receipt_staging_runner.models import (
    RunnerFinalizeError as RunnerFinalizeError,
)
from finance_core.receipt_staging_runner.models import (
    RunnerInputManifest as RunnerInputManifest,
)
from finance_core.receipt_staging_runner.models import (
    RunnerManifestError as RunnerManifestError,
)
from finance_core.receipt_staging_runner.models import (
    RunnerParticipantError as RunnerParticipantError,
)
from finance_core.receipt_staging_runner.models import (
    RunnerRecoveryError as RunnerRecoveryError,
)
from finance_core.receipt_staging_runner.models import (
    RunnerResumeError as RunnerResumeError,
)
from finance_core.receipt_staging_runner.models import (
    RunnerWorkspace as RunnerWorkspace,
)
from finance_core.receipt_staging_runner.models import (
    RunnerWorkspaceError as RunnerWorkspaceError,
)
from finance_core.receipt_staging_runner.models import (
    parse_runner_manifest as parse_runner_manifest,
)
from finance_core.receipt_staging_runner.participants import (
    bootstrap_participants as bootstrap_participants,
)
from finance_core.receipt_staging_runner.recovery import run_recovery as run_recovery
from finance_core.receipt_staging_runner.resume_finalize import (
    finalize_runner_run as finalize_runner_run,
)
from finance_core.receipt_staging_runner.resume_finalize import (
    resume_runner_run as resume_runner_run,
)
from finance_core.receipt_staging_runner.workspace import (
    create_runner_workspace as create_runner_workspace,
)
from finance_core.receipt_staging_runner.workspace import (
    recover_runner_workspace as recover_runner_workspace,
)

__all__ = [
    "LocalIntakeCopyError",
    "LocalIntakeError",
    "LocalIntakeEvidenceError",
    "LocalIntakeFileError",
    "LocalIntakePersonalOnlyError",
    "LocalIntakePipelineError",
    "LocalIntakeResult",
    "LocalLineageError",
    "ParticipantBootstrapResult",
    "ParticipantDefinition",
    "RUN_MANIFEST_SCHEMA_VERSION",
    "RecoveryEvidence",
    "RunManifest",
    "RunnerFinalizeError",
    "RunnerInputManifest",
    "RunnerManifestError",
    "RunnerParticipantError",
    "RunnerRecoveryError",
    "RunnerResumeError",
    "RunnerWorkspace",
    "RunnerWorkspaceError",
    "bootstrap_participants",
    "create_runner_workspace",
    "finalize_runner_run",
    "import_local_receipt_file",
    "parse_runner_manifest",
    "persist_local_attachment_evidence",
    "recover_runner_workspace",
    "require_local_runner_receipt",
    "require_local_runner_receipt_proposal",
    "resume_runner_run",
    "run_local_receipt_intake",
    "run_recovery",
    "validate_personal_conversion_command",
    "validate_personal_fact_set_command",
]
