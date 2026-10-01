# Pinned Linux receipt OCR

This staging adapter reads captured synthetic or separately authorized image
source evidence and produces untrusted OCR/proposal evidence. Human completion
records human provenance. It does not confirm, finalize, create final financial
facts, enable managed capture processing, download resources or discover an
executable. Managed full receipt processing remains a separate contract.

## Trusted workspace configuration

`runtime/ocr_engine.json` schema `v1` retains the macOS Vision contract. Schema
`v2` is Linux-only and accepts exactly these keys:

```json
{
  "schema_version": "v2",
  "engine": "tesseract_tsv",
  "helper_path": "/private/service/tesseract",
  "expected_version": "5.3.4",
  "binary_sha256": "<64 lowercase hexadecimal characters>",
  "tessdata_directory": "/private/service/tessdata",
  "language_resources": [
    {"language": "eng", "size_bytes": 4113088, "sha256": "<eng SHA-256>"},
    {"language": "chi_sim", "size_bytes": 2469156, "sha256": "<chi_sim SHA-256>"}
  ]
}
```

The file is bounded to 4096 complete UTF-8 JSON bytes, rejects duplicate keys,
and must be stable, service-owned and not group/other writable. The request
cannot supply configuration, executable paths, switches or environment.
Resources must be exactly `eng`, then `chi_sim`, as direct regular files named
`<language>.traineddata`, in a canonical directory without symlink components.
Models and their directory are service-owned and not group/other writable.
Each model is bounded to 32 MiB, total to 64 MiB. Constructor verification is
bounded to ten seconds for models and ten seconds for executable hashing.

The executable safety contract is unchanged: a direct service-owned executable,
not group/other writable, whose opened-file identity/hash and actual version
are checked. A root-owned distro executable must first be verified and copied
to a private service-owned location, mode 0500. Dynamically linked libraries and
the OS remain the trusted installation boundary; this protocol does not pin a
complete dynamically linked process or defend against a compromised OS.

## Identity and custody

`TesseractTsvOcrEngine(..., language="eng+chi_sim", pinned_resources=...)` adds
an internal frozen descriptor from `intake/tesseract_resources.py`. Legacy
calls without resources retain their original hash, arguments and environment.
Pinned mode uses `tesseract-tsv-pinned-v1`, binding ordered language sizes and
hashes, fixed normalized arguments and fixed environment. Absolute model paths
are excluded, so identical resources can be relocated. Binary hash/version
remain separate identity fields. Existing extraction IDs conflict if rebound
to a different identity; historical evidence is never rewritten.

Each extraction streams checked source model bytes into its own 0700 snapshot,
with 0400 model files. The completed directory has a held descriptor and recorded
identity including change time. Directory and model identities are checked during
copy and before/after OCR; Linux model reads use the inherited directory descriptor
through `/proc/self/fd`, so a replaced directory pathname cannot redirect them.
Snapshot copying/hashing, binary checks, version query
and OCR share the extraction deadline. Only this owned temporary directory is
cleaned on completion or failure using its held directory descriptor. Cleanup
never recursively removes a replacement pathname or unknown entries; directory
removal requires the original inode still at its pathname. Replacement of an original model after the
snapshot is checked cannot change the current invocation; the next invocation
must verify the newly opened source and refuse a mismatch. Mutation/replacement
of the private snapshot or executable fails before evidence is accepted. This
is an invocation snapshot, not an atomic snapshot of the entire machine or a
defense against arbitrary hostile code under the service UID or a compromised OS.

Pinned arguments are fixed: opened executable/input descriptors, `stdout`,
`-l eng+chi_sim --tessdata-dir <opened snapshot directory descriptor> --oem 1 --psm 3 --dpi 300
-c tessedit_create_tsv=1`. No external `tsv` configuration is loaded. The process
receives only `LANG=C`, `LC_ALL=C`, `TZ=UTC`, `OMP_THREAD_LIMIT=1`; no parent
variables. Existing single-main-thread POSIX restriction, process-group cleanup,
output ceilings and OS limits remain. Default address-space limit is 512 MiB.

## Actual acceptance and asset preparation

`scripts/linux_ocr_assets_v1.json` pins the Apache-2.0 `tessdata_fast` upstream
commit, Tesseract 5.3.4 version, selected files, byte lengths and SHA-256 values. The separate explicit
`scripts/prepare_linux_ocr.py` preparation downloads and verifies only those
assets and copies the trusted distro executable outside Git. No model binaries
or generation fonts are committed. Production extraction performs no download.
The checked-in JPEG/PNG fixtures contain fictional mixed Chinese/English text;
the incomplete fixture has no amount. Fixture provenance records visible text.

The existing `bridge (ubuntu-latest)` identity maps explicitly to Ubuntu 24.04
x86_64 and requires actual Tesseract acceptance. The mandatory environment
`FINANCE_LINUX_OCR_REQUIRED=1` makes missing configuration, platform or resources
fail; its JUnit proof additionally requires at least 13 executed cases and zero
skips/errors. Ordinary test environments skip actual-only tests. Both raster
formats run real resolver/subprocess/capture/propose, verify original image/hash,
engine identity, mixed text, amount, one extraction/proposal on replay and zero
final facts. Incomplete OCR reaches the existing human card completion path;
its original image/OCR and proposal are retained with traceable human evidence.
An actual process output cap proves bounded refusal. A deterministic deadline
fault after observing the actual OCR launch verifies real process-group
termination and zero partial evidence; it does not measure natural OCR latency.
Preparation records distro/package/version/binary/resource identities and the
lane preserves its receipt and actual acceptance JUnit timing, including failures.
Failed asset preparation removes its partial models, binary and configuration,
and independently retains a 0600, at most 16 KiB
`<destination-name>-preparation-failure.json` beside the destination. This receipt
records stage/status, sanitized failure category, expected and observed resource
sizes/hashes and version available so far; it excludes exception text, download
content, host paths and inherited environment. The always-upload step includes
this separate failure receipt. Deterministic tests cover
resource races, cleanup and compatibility; they do not certify actual Linux OCR.

Linux recognition accuracy, memory fit and timing are established only by the
actual hosted lane for the candidate. C3 staging acceptance does not establish
managed Host processing, cloud deployment, release or active consumer upgrade.
