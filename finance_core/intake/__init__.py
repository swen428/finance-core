"""Compatibility exports, loaded only when explicitly requested.

Importing a neutral submodule must not eagerly import unrelated platform
adapters or authority services. Export names and resolved object identities
remain unchanged; no wrapper implementation replaces the underlying service.
"""

from importlib import import_module
from typing import Any

_LAZY_EXPORTS = {
    "AcquisitionReplayConflictError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "AcquisitionReplayConflictError",
    ),
    "AcquisitionReplayIntegrityError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "AcquisitionReplayIntegrityError",
    ),
    "AttachmentEvidenceConflictError": (
        "finance_core.intake.attachment_evidence",
        "AttachmentEvidenceConflictError",
    ),
    "AttachmentEvidenceError": (
        "finance_core.intake.attachment_evidence",
        "AttachmentEvidenceError",
    ),
    "AttachmentEvidenceHandoffConflictError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "AttachmentEvidenceHandoffConflictError",
    ),
    "AttachmentEvidencePersistenceError": (
        "finance_core.intake.attachment_evidence",
        "AttachmentEvidencePersistenceError",
    ),
    "AttachmentExpectedHashMismatchError": (
        "finance_core.intake.attachment_evidence",
        "AttachmentExpectedHashMismatchError",
    ),
    "AttachmentExpectedSizeMismatchError": (
        "finance_core.intake.attachment_evidence",
        "AttachmentExpectedSizeMismatchError",
    ),
    "AttachmentFileChangedDuringHashError": (
        "finance_core.intake.attachment_evidence",
        "AttachmentFileChangedDuringHashError",
    ),
    "AttachmentFileNotFoundError": (
        "finance_core.intake.attachment_evidence",
        "AttachmentFileNotFoundError",
    ),
    "AttachmentFileUnreadableError": (
        "finance_core.intake.attachment_evidence",
        "AttachmentFileUnreadableError",
    ),
    "AttachmentRawIntakeConflictError": (
        "finance_core.intake.attachment_evidence",
        "AttachmentRawIntakeConflictError",
    ),
    "CallerOwnedTransactionError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "CallerOwnedTransactionError",
    ),
    "ContentSignatureMismatchError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "ContentSignatureMismatchError",
    ),
    "DurableFileIntegrityConflictError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "DurableFileIntegrityConflictError",
    ),
    "DurablePublicationError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "DurablePublicationError",
    ),
    "InvalidAcquisitionConfigurationError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "InvalidAcquisitionConfigurationError",
    ),
    "InvalidAttachmentIdentityError": (
        "finance_core.intake.attachment_evidence",
        "InvalidAttachmentIdentityError",
    ),
    "InvalidOcrConfigurationError": (
        "finance_core.intake.receipt_ocr_evidence",
        "InvalidOcrConfigurationError",
    ),
    "InvalidProposalCommandError": (
        "finance_core.intake.receipt_ocr_proposal",
        "InvalidProposalCommandError",
    ),
    "InvalidRemoteFilePathError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "InvalidRemoteFilePathError",
    ),
    "InvalidTelegramIdentityError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "InvalidTelegramIdentityError",
    ),
    "MACOS_VISION_HELPER_NAME": ("finance_core.intake.macos_vision_receipt_ocr", "HELPER_NAME"),
    "MACOS_VISION_HELPER_PROTOCOL_VERSION": (
        "finance_core.intake.macos_vision_receipt_ocr",
        "HELPER_PROTOCOL_VERSION",
    ),
    "MACOS_VISION_MAX_LANGUAGE_COUNT": (
        "finance_core.intake.macos_vision_receipt_ocr",
        "MAX_LANGUAGE_COUNT",
    ),
    "MACOS_VISION_RECOGNITION_LEVEL": (
        "finance_core.intake.macos_vision_receipt_ocr",
        "RECOGNITION_LEVEL",
    ),
    "MACOS_VISION_REQUEST_REVISION": (
        "finance_core.intake.macos_vision_receipt_ocr",
        "VISION_REQUEST_REVISION",
    ),
    "MACOS_VISION_USES_LANGUAGE_CORRECTION": (
        "finance_core.intake.macos_vision_receipt_ocr",
        "USES_LANGUAGE_CORRECTION",
    ),
    "MacOSVisionOcrEngine": (
        "finance_core.intake.macos_vision_receipt_ocr",
        "MacOSVisionOcrEngine",
    ),
    "MalformedOcrOutputError": (
        "finance_core.intake.receipt_ocr_evidence",
        "MalformedOcrOutputError",
    ),
    "MalformedTelegramMetadataError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "MalformedTelegramMetadataError",
    ),
    "OcrAttachmentIntegrityConflictError": (
        "finance_core.intake.receipt_ocr_evidence",
        "OcrAttachmentIntegrityConflictError",
    ),
    "OcrAttachmentNotFoundError": (
        "finance_core.intake.receipt_ocr_evidence",
        "OcrAttachmentNotFoundError",
    ),
    "OcrCallerOwnedTransactionError": (
        "finance_core.intake.receipt_ocr_evidence",
        "OcrCallerOwnedTransactionError",
    ),
    "OcrDeadlineExceededError": (
        "finance_core.intake.receipt_ocr_evidence",
        "OcrDeadlineExceededError",
    ),
    "OcrEngineLaunchError": ("finance_core.intake.receipt_ocr_evidence", "OcrEngineLaunchError"),
    "OcrExtractionNotFoundError": (
        "finance_core.intake.receipt_ocr_proposal",
        "OcrExtractionNotFoundError",
    ),
    "OcrIdempotencyConflictError": (
        "finance_core.intake.receipt_ocr_evidence",
        "OcrIdempotencyConflictError",
    ),
    "OcrPersistenceConflictError": (
        "finance_core.intake.receipt_ocr_evidence",
        "OcrPersistenceConflictError",
    ),
    "OcrProcessCleanupError": (
        "finance_core.intake.macos_vision_receipt_ocr",
        "OcrProcessCleanupError",
    ),
    "OcrResourceLimitExceededError": (
        "finance_core.intake.receipt_ocr_evidence",
        "OcrResourceLimitExceededError",
    ),
    "OcrStagingDatabaseRejectedError": (
        "finance_core.intake.receipt_ocr_evidence",
        "OcrStagingDatabaseRejectedError",
    ),
    "OcrUnexpectedPersistenceError": (
        "finance_core.intake.receipt_ocr_evidence",
        "OcrUnexpectedPersistenceError",
    ),
    "OcrUnsupportedPlatformError": (
        "finance_core.intake.receipt_ocr_evidence",
        "OcrUnsupportedPlatformError",
    ),
    "ProposalCallerOwnedTransactionError": (
        "finance_core.intake.receipt_ocr_proposal",
        "ProposalCallerOwnedTransactionError",
    ),
    "ProposalDuplicateInitialError": (
        "finance_core.intake.receipt_ocr_proposal",
        "ProposalDuplicateInitialError",
    ),
    "ProposalIdempotencyConflictError": (
        "finance_core.intake.receipt_ocr_proposal",
        "ProposalIdempotencyConflictError",
    ),
    "ProposalPersistenceConflictError": (
        "finance_core.intake.receipt_ocr_proposal",
        "ProposalPersistenceConflictError",
    ),
    "ProposalSourceBindingConflictError": (
        "finance_core.intake.receipt_ocr_proposal",
        "ProposalSourceBindingConflictError",
    ),
    "ProposalStagingDatabaseRejectedError": (
        "finance_core.intake.receipt_ocr_proposal",
        "ProposalStagingDatabaseRejectedError",
    ),
    "ProposalUnexpectedPersistenceError": (
        "finance_core.intake.receipt_ocr_proposal",
        "ProposalUnexpectedPersistenceError",
    ),
    "RawIntakeNotFoundError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "RawIntakeNotFoundError",
    ),
    "ReceiptOcrBlock": ("finance_core.intake.receipt_ocr_evidence", "ReceiptOcrBlock"),
    "ReceiptOcrEngine": ("finance_core.intake.receipt_ocr_evidence", "ReceiptOcrEngine"),
    "ReceiptOcrEngineIdentity": (
        "finance_core.intake.receipt_ocr_evidence",
        "ReceiptOcrEngineIdentity",
    ),
    "ReceiptOcrEngineResult": (
        "finance_core.intake.receipt_ocr_evidence",
        "ReceiptOcrEngineResult",
    ),
    "ReceiptOcrError": ("finance_core.intake.receipt_ocr_evidence", "ReceiptOcrError"),
    "ReceiptOcrExtractionResult": (
        "finance_core.intake.receipt_ocr_evidence",
        "ReceiptOcrExtractionResult",
    ),
    "ReceiptOcrExtractionStatus": (
        "finance_core.intake.receipt_ocr_evidence",
        "ReceiptOcrExtractionStatus",
    ),
    "ReceiptOcrLimits": ("finance_core.intake.receipt_ocr_evidence", "ReceiptOcrLimits"),
    "ReceiptOcrProposalError": (
        "finance_core.intake.receipt_ocr_proposal",
        "ReceiptOcrProposalError",
    ),
    "ReceiptOcrSource": ("finance_core.intake.receipt_ocr_evidence", "ReceiptOcrSource"),
    "ReceiptTotalProposalIngestionResult": (
        "finance_core.intake.receipt_ocr_proposal",
        "ReceiptTotalProposalIngestionResult",
    ),
    "StagingDatabaseRejectedError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "StagingDatabaseRejectedError",
    ),
    "TelegramAttachmentAcquisitionError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "TelegramAttachmentAcquisitionError",
    ),
    "TelegramAttachmentAcquisitionLimits": (
        "finance_core.intake.telegram_attachment_acquisition",
        "TelegramAttachmentAcquisitionLimits",
    ),
    "TelegramAttachmentAcquisitionResult": (
        "finance_core.intake.telegram_attachment_acquisition",
        "TelegramAttachmentAcquisitionResult",
    ),
    "TelegramAttachmentTransport": (
        "finance_core.intake.telegram_attachment_acquisition",
        "TelegramAttachmentTransport",
    ),
    "TelegramBotApiTransport": (
        "finance_core.intake.telegram_bot_api_transport",
        "TelegramBotApiTransport",
    ),
    "TelegramDeclaredFileTooLargeError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "TelegramDeclaredFileTooLargeError",
    ),
    "TelegramDownloadResponse": (
        "finance_core.intake.telegram_attachment_acquisition",
        "TelegramDownloadResponse",
    ),
    "TelegramDownloadTimeoutError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "TelegramDownloadTimeoutError",
    ),
    "TelegramFileMetadata": (
        "finance_core.intake.telegram_attachment_acquisition",
        "TelegramFileMetadata",
    ),
    "TelegramHttpResponseError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "TelegramHttpResponseError",
    ),
    "TelegramMetadataRequestError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "TelegramMetadataRequestError",
    ),
    "TelegramRedirectError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "TelegramRedirectError",
    ),
    "TelegramStreamedFileTooLargeError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "TelegramStreamedFileTooLargeError",
    ),
    "TelegramTruncatedDownloadError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "TelegramTruncatedDownloadError",
    ),
    "TemporaryFileCleanupError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "TemporaryFileCleanupError",
    ),
    "TemporaryFileError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "TemporaryFileError",
    ),
    "TesseractTsvOcrEngine": ("finance_core.intake.receipt_ocr_evidence", "TesseractTsvOcrEngine"),
    "UnexpectedAttachmentPersistenceError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "UnexpectedAttachmentPersistenceError",
    ),
    "UnsafeStorageRootError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "UnsafeStorageRootError",
    ),
    "UnsupportedFilenameExtensionError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "UnsupportedFilenameExtensionError",
    ),
    "UnsupportedMimeTypeError": (
        "finance_core.intake.telegram_attachment_acquisition",
        "UnsupportedMimeTypeError",
    ),
    "UnusableOcrEvidenceError": (
        "finance_core.intake.receipt_ocr_proposal",
        "UnusableOcrEvidenceError",
    ),
    "acquire_and_persist_telegram_attachment": (
        "finance_core.intake.telegram_attachment_acquisition",
        "acquire_and_persist_telegram_attachment",
    ),
    "convert_vision_bounding_box": (
        "finance_core.intake.macos_vision_receipt_ocr",
        "convert_vision_bounding_box",
    ),
    "convert_vision_observations": (
        "finance_core.intake.macos_vision_receipt_ocr",
        "convert_vision_observations",
    ),
    "extract_and_persist_receipt_ocr_evidence": (
        "finance_core.intake.receipt_ocr_evidence",
        "extract_and_persist_receipt_ocr_evidence",
    ),
    "get_attachment_evidence": (
        "finance_core.intake.attachment_evidence",
        "get_attachment_evidence",
    ),
    "ingest_receipt_ocr_evidence_as_total_expense_proposal": (
        "finance_core.intake.receipt_ocr_proposal",
        "ingest_receipt_ocr_evidence_as_total_expense_proposal",
    ),
    "persist_attachment_evidence": (
        "finance_core.intake.attachment_evidence",
        "persist_attachment_evidence",
    ),
    "scale_vision_confidence": (
        "finance_core.intake.macos_vision_receipt_ocr",
        "scale_vision_confidence",
    ),
}

__all__ = [
    "persist_attachment_evidence",
    "get_attachment_evidence",
    "AttachmentEvidenceError",
    "AttachmentFileNotFoundError",
    "AttachmentFileUnreadableError",
    "AttachmentFileChangedDuringHashError",
    "AttachmentEvidenceConflictError",
    "AttachmentEvidencePersistenceError",
    "AttachmentRawIntakeConflictError",
    "InvalidAttachmentIdentityError",
    "AttachmentExpectedSizeMismatchError",
    "AttachmentExpectedHashMismatchError",
    "acquire_and_persist_telegram_attachment",
    "TelegramBotApiTransport",
    "TelegramAttachmentAcquisitionLimits",
    "TelegramAttachmentAcquisitionResult",
    "TelegramFileMetadata",
    "TelegramAttachmentTransport",
    "TelegramDownloadResponse",
    "TelegramAttachmentAcquisitionError",
    "InvalidAcquisitionConfigurationError",
    "InvalidTelegramIdentityError",
    "StagingDatabaseRejectedError",
    "CallerOwnedTransactionError",
    "RawIntakeNotFoundError",
    "UnsafeStorageRootError",
    "TelegramMetadataRequestError",
    "MalformedTelegramMetadataError",
    "InvalidRemoteFilePathError",
    "TelegramHttpResponseError",
    "TelegramRedirectError",
    "TelegramDownloadTimeoutError",
    "TelegramDeclaredFileTooLargeError",
    "TelegramStreamedFileTooLargeError",
    "TelegramTruncatedDownloadError",
    "UnsupportedMimeTypeError",
    "UnsupportedFilenameExtensionError",
    "ContentSignatureMismatchError",
    "TemporaryFileCleanupError",
    "TemporaryFileError",
    "DurablePublicationError",
    "DurableFileIntegrityConflictError",
    "AcquisitionReplayConflictError",
    "AcquisitionReplayIntegrityError",
    "AttachmentEvidenceHandoffConflictError",
    "UnexpectedAttachmentPersistenceError",
    "extract_and_persist_receipt_ocr_evidence",
    "TesseractTsvOcrEngine",
    "MacOSVisionOcrEngine",
    "MACOS_VISION_HELPER_NAME",
    "MACOS_VISION_HELPER_PROTOCOL_VERSION",
    "MACOS_VISION_MAX_LANGUAGE_COUNT",
    "MACOS_VISION_RECOGNITION_LEVEL",
    "MACOS_VISION_REQUEST_REVISION",
    "MACOS_VISION_USES_LANGUAGE_CORRECTION",
    "convert_vision_bounding_box",
    "convert_vision_observations",
    "scale_vision_confidence",
    "ReceiptOcrLimits",
    "ReceiptOcrExtractionStatus",
    "ReceiptOcrEngineIdentity",
    "ReceiptOcrSource",
    "ReceiptOcrBlock",
    "ReceiptOcrEngineResult",
    "ReceiptOcrEngine",
    "ReceiptOcrExtractionResult",
    "ReceiptOcrError",
    "InvalidOcrConfigurationError",
    "OcrStagingDatabaseRejectedError",
    "OcrCallerOwnedTransactionError",
    "OcrAttachmentNotFoundError",
    "OcrAttachmentIntegrityConflictError",
    "OcrUnsupportedPlatformError",
    "OcrEngineLaunchError",
    "OcrDeadlineExceededError",
    "OcrResourceLimitExceededError",
    "OcrProcessCleanupError",
    "MalformedOcrOutputError",
    "OcrIdempotencyConflictError",
    "OcrPersistenceConflictError",
    "OcrUnexpectedPersistenceError",
    "ingest_receipt_ocr_evidence_as_total_expense_proposal",
    "ReceiptTotalProposalIngestionResult",
    "ReceiptOcrProposalError",
    "InvalidProposalCommandError",
    "ProposalStagingDatabaseRejectedError",
    "ProposalCallerOwnedTransactionError",
    "OcrExtractionNotFoundError",
    "UnusableOcrEvidenceError",
    "ProposalSourceBindingConflictError",
    "ProposalIdempotencyConflictError",
    "ProposalDuplicateInitialError",
    "ProposalPersistenceConflictError",
    "ProposalUnexpectedPersistenceError",
]


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
