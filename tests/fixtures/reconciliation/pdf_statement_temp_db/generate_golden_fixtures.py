"""Generate deterministic, synthetic PDF statement golden fixtures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

FIXTURE_ROOT = Path(__file__).resolve().parent

SINGLE_PAGE_LINES = (
    "01/07/2026 CoffeeShop 12.50 D",
    "02/07/2026 Salary (1000.00) C",
    "03/07/2026 Grocer 45.67 DEBIT",
)
MULTI_PAGE_LINES = (
    (
        "01/07/2026 Synthetic Cafe 12.50 D",
        "02/07/2026 Synthetic Refund 5.00 C",
    ),
    (
        "03/07/2026 Synthetic Transit 3.20 DEBIT",
        "04/07/2026 Synthetic Credit 8.75 CREDIT",
    ),
)
BOUNDARY_TEXT_CHARS = 512


def _boundary_line() -> str:
    prefix = "05/07/2026 "
    suffix = " 1.00 D"
    description_length = BOUNDARY_TEXT_CHARS - len(prefix) - len(suffix)
    return f"{prefix}{'X' * description_length}{suffix}"


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _content_stream(lines: tuple[str, ...]) -> bytes:
    operators = ["BT", "/F1 10 Tf", "72 740 Td", "14 TL"]
    for index, line in enumerate(lines):
        if index:
            operators.append("T*")
        operators.append(f"({_escape_pdf_text(line)}) Tj")
    operators.append("ET")
    return ("\n".join(operators) + "\n").encode("ascii")


def build_text_pdf_bytes(page_lines: tuple[tuple[str, ...], ...]) -> bytes:
    """Build a strict, deterministic text PDF for checked-in or temporary fixtures."""
    page_ids = tuple(4 + index * 2 for index in range(len(page_lines)))
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            f"<< /Type /Pages /Kids [{' '.join(f'{page_id} 0 R' for page_id in page_ids)}] "
            f"/Count {len(page_ids)} >>"
        ).encode("ascii"),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
    }
    for index, lines in enumerate(page_lines):
        page_id = page_ids[index]
        content_id = page_id + 1
        stream = _content_stream(lines)
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream"
        )

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id in range(1, max(objects) + 1):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode("ascii"))
        output.extend(objects[object_id])
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def _encrypt_pdf(source_path: Path, destination_path: Path) -> None:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, ByteStringObject

    writer = PdfWriter()
    writer.clone_document_from_reader(PdfReader(source_path, strict=True))
    fixed_identifier = ByteStringObject(b"finance-codex-synthetic-encrypted-fixture")
    writer._ID = ArrayObject((fixed_identifier, fixed_identifier))  # noqa: SLF001
    writer.encrypt(
        "synthetic-test-password",
        owner_password="synthetic-owner-password",
        algorithm="RC4-128",
    )
    with destination_path.open("wb") as stream:
        writer.write(stream)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    fixtures = {
        "sample_bank_statement.pdf": build_text_pdf_bytes((SINGLE_PAGE_LINES,)),
        "multi_page_bank_statement.pdf": build_text_pdf_bytes(MULTI_PAGE_LINES),
        "near_text_boundary_statement.pdf": build_text_pdf_bytes(((_boundary_line(),),)),
    }
    for filename, content in fixtures.items():
        (FIXTURE_ROOT / filename).write_bytes(content)

    encrypted_path = FIXTURE_ROOT / "encrypted_bank_statement.pdf"
    _encrypt_pdf(FIXTURE_ROOT / "sample_bank_statement.pdf", encrypted_path)

    manifest = {
        "contract_version": "synthetic-pdf-golden-v1",
        "sanitization": (
            "All fixture content is synthetic and contains no real account, employee, "
            "personal financial, production transaction, identifying, or secret data."
        ),
        "fixtures": [
            {
                "filename": "sample_bank_statement.pdf",
                "purpose": "Valid single-page debit and credit statement",
                "sha256": _sha256(FIXTURE_ROOT / "sample_bank_statement.pdf"),
                "expected_page_count": 1,
                "expected_parsed_rows": 3,
                "expected_failure": None,
                "synthetic_and_sanitized": True,
            },
            {
                "filename": "multi_page_bank_statement.pdf",
                "purpose": "Valid multi-page statement with page and row evidence",
                "sha256": _sha256(FIXTURE_ROOT / "multi_page_bank_statement.pdf"),
                "expected_page_count": 2,
                "expected_parsed_rows": 4,
                "expected_failure": None,
                "synthetic_and_sanitized": True,
            },
            {
                "filename": "near_text_boundary_statement.pdf",
                "purpose": "Valid one-page statement at a controlled 512-character test limit",
                "sha256": _sha256(FIXTURE_ROOT / "near_text_boundary_statement.pdf"),
                "expected_page_count": 1,
                "expected_parsed_rows": 1,
                "expected_failure": None,
                "controlled_boundary_chars": BOUNDARY_TEXT_CHARS,
                "synthetic_and_sanitized": True,
            },
            {
                "filename": "encrypted_bank_statement.pdf",
                "purpose": "Valid encrypted PDF rejected by the untrusted import boundary",
                "sha256": _sha256(encrypted_path),
                "expected_page_count": 1,
                "expected_parsed_rows": None,
                "expected_failure": "pdf_encrypted_document",
                "synthetic_and_sanitized": True,
            },
        ],
    }
    (FIXTURE_ROOT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
