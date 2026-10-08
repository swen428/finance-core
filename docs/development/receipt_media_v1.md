# Immutable local receipt media and OCR evidence v1

`finance_core.intake.receipt_media` provides an additive, local media evidence
seam. It retains the **bytes actually received**, normalizes one supported
static image to PNG, invokes the explicit local OCR engine on that exact PNG,
and publishes an immutable, replayable reference. It performs no database,
proposal, confirmation, final-fact, provider, Telegram or runtime operations.

Install the optional `finance-core[media]` extra: `Pillow==12.3.0` and
`pillow-heif==1.8.0`. Base Core imports and existing consumers retain their
existing dependency contract. The native worker requires Linux and a
single-threaded caller. Actual Ubuntu 24.04 qualification is separate from
unit tests with synthetic decoder or OCR fault adapters.

## Trusted composition and captured input

```python
from finance_core.intake.receipt_media import ReceiptMediaProcessor

processor = ReceiptMediaProcessor(
    "/absolute/private/captured-sources",  # existing, current-user-owned 0700
    "/absolute/private/media-evidence",   # existing, disjoint, owned 0700
    ocr_engine=explicit_local_engine,
)
result = processor.process(
    operation_id="media_example_1",
    source_relative_path="received.png",  # captured regular file, mode 0400
    expected_source_sha256=received_sha256,
    expected_source_size=received_size,
    declared_mime_type="image/png",
    original_filename="received.png",
    received_via="telegram_photo",
)
if result.reference is not None:
    verified = processor.read_verified(result.reference)
```

The composition fixes the source/evidence roots, local OCR identity/limits,
installed decoder, interpreter, helper, normalization parameters and budgets.
A request cannot choose code, an interpreter, a module, an OCR engine or a
budget. Installation and the OS owner are trusted; installed decoder code and
native library bytes, versions and helper/interpreter hashes are recorded in
the operation identity. This is not a cryptographic attestation against a
malicious OS owner.

Paths are relative to the held source root. Source directories are private
0700, source files are current-user-owned regular 0400 files, and symlink
traversal is refused. Expected size/hash must agree with the current opened
source. Original bytes are reverified after copying and processing; source
root and nested directory path identities must still agree with held handles.
The caller supplies captured local input; this API does not download files.

`received_via` is `local`, `telegram_photo` or `telegram_document`. These are
provenance descriptions only. A Telegram photo may already have been
transcoded before receipt. The retained copy does not claim to be an unseen
camera original. Source-event/channel authentication and later proposal
lifecycle integration remain separate consumer responsibilities.

## Supported subset and fixed normalization

JPEG and PNG require agreeing actual format, declared MIME and filename,
strict decoding, and one frame. Valid EXIF orientations 1–8 are applied once.
Malformed or conflicting EXIF/XMP orientation is refused. XMP-only nondefault
raster orientation is explicitly ambiguous rather than substituted for EXIF.
Only the validated raw TIFF orientation entry counts as EXIF evidence; Pillow's
XMP-to-EXIF synthesis does not. Metadata is observed after strict raster load,
including PNG eXIf chunks following IDAT, with geometry checked before loading.
The validated orientation selects one fixed pixel transpose directly.
APNG and unsupported pixel layouts are refused.

Static HEVC HEIC/HEIF requires exactly one top-level image. Sequence brands or
a track container (`moov`), multiple top-level images, unsupported codecs and
bit depths are refused. The pinned native decoder exposes the primary item's
ordered `irot`, `imir` and `clap` properties. libheif applies these normative
container transforms exactly once. Original EXIF/XMP orientation is observed
before the binding clears it; it is recorded and removed from the derivative.
Descriptive HEIF metadata does not authorize a second rotation. Nondefault
EXIF/XMP without `irot`/`imir` returns
`unsupported_input / ambiguous_orientation`. Malformed and internally
contradictory direction metadata is refused.

HEIF 8/10-bit inputs use the explicit fixed `convert_hdr_to_8bit=True` policy.
Original bit depth and observed color/profile identity are retained. The PNG
is RGB8, compression level 6, `optimize=False`, with no metadata forwarded.
Alpha is composited onto an explicit fixed white matte so invisible RGB cannot
become visible receipt amounts merely by dropping alpha. This policy is bound
in the parameters and normalization metadata.
There is no geometry resize, adaptive quality reduction, format fallback or
claim of HDR/color fidelity. Unsupported greater-than-10-bit layouts are
refused. Crop/rotation/mirroring specified by the original container are
recorded as geometry transforms, not an adaptive image reduction.

## Independent resource boundaries

| Resource | Fixed media bound |
| --- | ---: |
| Actual captured input | 20,000,000 bytes |
| Decoded image pixels | 24,000,000 |
| Decoded plane bytes | 134,217,728 |
| PNG derivative | 20,000,000 bytes |
| Aggregate operation temporary files | 268,435,456 bytes |
| Decoder address space | 536,870,912 bytes |
| Decoder CPU | 30 seconds |
| Normalization wall budget, shared by launch/decode/transform/PNG verification | 30 seconds |
| Decoder threads and operation concurrency per evidence root | 1 |

Decoder stdout/stderr, metadata and manifest sizes have separate bounds.
The worker invokes the constructing interpreter using its absolute executable
spelling, preserving virtual-environment selection. The resolved binary hash
and target, invocation path, observed Python prefixes and installed native
package closure are bound; another Python environment is refused.
The decoder uses `RLIMIT_NPROC=16`, `RLIMIT_NOFILE=64`, stdout 262,144 bytes,
stderr 65,536 bytes and a 0.25-second termination grace. UID-wide process/thread
headroom is checked before native loading; root media execution is refused.
Before a `preexec_fn` launch the Linux caller must have exactly one OS thread
and zero permitted/effective capabilities. Python's active thread count is
also checked. This caller condition, the decoder's configured thread count
and UID-wide `RLIMIT_NPROC=16` are distinct; the policy does not claim the
native decoder can never create an internal OS thread.
Dimensions and decoded bytes are checked before/after decoding; image size
can change during HEIF transformation. PNG signature, geometry, RGB8 layout,
chunk CRCs and complete ending are checked without a second native decode.
Fixed worker code writes only its passed output descriptor. The complete
process group is terminated/reaped on deadlines/output bounds and after
normal exit. Native process failures never widen a budget or retry.

OCR retains its own `ReceiptOcrLimits` and separate default 30-second budget.
Trusted composition may tighten these defaults, but cannot enlarge any bound.
The actual `ReceiptOcrSource` names the immutable opened PNG, `image/png`, its
exact hash and size. The original and PNG descriptors supplied to children
are read-only after publication. The engine identity must remain fixed.
Canonical returned blocks/status/outcome are persisted; OCR output remains
untrusted evidence. `no_text` is a completed OCR evidence result, not proof
that a receipt proposal can be constructed.

## Durable operation and terminal states

A root owner lock serializes operation processing. Each `media_` operation ID
first reserves an exclusive root-level `<operation_id>.claim.json`. Its
canonical `receipt-media-operation-claim-v1` payload contains only the operation
ID and intent SHA-256, without absolute paths or filesystem inode numbers.
Creation uses `O_EXCL`/`O_NOFOLLOW`, 0600 writing, file fsync, 0400 sealing,
another file fsync and root fsync before creating the operation directory.
The claim is read with a 4096-byte bound. Its held descriptor, canonical
pathname, immutable bytes, owner/mode, device/inode and single link are
reverified at processing custody checkpoints and before returns.
The operation then gets a new private 0700 directory with an exclusive immutable `intent.json`.
The intent binds actual source identity/declarations, decoder/code identity,
normalization parameters/bounds and OCR identity/limits. Original bytes are
copied and fsynced at `original.bin`, mode 0400, before native work begins.
Complete evidence members use exclusive no-overwrite creation, file fsync,
0400 sealing and directory fsync. No source or existing member is overwritten.
The held operation directory must continue to match its canonical root/name,
device, inode, private owner/mode and directory link count. Custody is
checked before native/OCR path use, terminal publication and every return,
including replay and fresh reference reads. A moved, replaced or symlinked
operation refuses; its existing evidence remains available for inspection.
An existing claim with a missing operation directory returns UNKNOWN without
creating a directory or invoking decoder/OCR. An existing directory without
its claim refuses processing; there is no automatic adoption or repair.
Partial, malformed, mismatched, unsafe or changed claims are retained and
refused. A claim-only interrupted reservation also prevents a fresh instance
from silently retrying the operation ID. The trusted OS owner can intentionally
delete both reservation and operation state; this contract does not claim
protection against that deliberate deletion.

Successful completion includes `normalized.png`, canonical `ocr.json` and a
terminal `result.json`. The result records exact normalization metadata,
derivative identity, normalization fingerprint, OCR identity/input
fingerprint and OCR result digest.
The completed OCR fingerprint includes its actual canonical result digest and
status in addition to the exact input, normalization, engine and limits.
Decoder process wall/CPU/peak-RSS observations are retained when the worker can
report them. Process termination
may prevent self-reported usage; a failure code is not invented usage proof.

`MediaProcessingResult` exposes `operation_id`, `status`, `outcome_code`,
`reference` and `persistence_idempotent`. Successful identical replay verifies
the retained original, current captured source and complete successful bundle
and returns its original reference without invoking either decoder or OCR.
Changed source/declarations/engine/limits/parameters cannot reuse that operation.

Decoder refusals, resource rejection and OCR errors publish terminal
`unsupported_input`, `resource_rejected` or `engine_failed` results with a
sanitized reason and retained original. Identical replay returns that terminal
failure and does not retry. Replay verifies every completed stage explicitly
recorded by the failure, including its exact PNG, normalization parameters and
fingerprint, and actual canonical OCR result/status/member/fingerprint when
present. Missing or contradictory recorded stages refuse. Unrecorded partial
work files do not gain a completeness claim. A durable intent without a completed terminal
result returns `unknown / incomplete_operation`; partial/malformed publication
or an integrity conflict refuses. Work files, original and intent remain
available for inspection. No missing terminal automatically reruns OCR or
promotes a partial PNG. A deliberate new attempt must receive a different
operation ID, preserving prior evidence.

## Exact reference and export unit

`MediaOcrReference` binds operation ID, intent SHA, terminal manifest SHA,
original SHA, normalization fingerprint, PNG SHA, OCR fingerprint and OCR
result SHA. `read_verified(reference)` reopens the trusted evidence root and
checks the reference, canonical manifests, immutable modes, member hashes,
normalization policy/metadata, precise OCR links and canonical blocks. A
caller-created DTO is a request to verify evidence, never a verified
capability by itself. Missing, wrong or tampered references/members refuse.
Reference reads do not require the processing claim: the six-member evidence
bundle remains portable and can be copied into a trusted reader-only root.
Resuming processing after a root copy/restore requires preserving all claims
and operation directories, including claim-only UNKNOWN reservations. A
six-member-only copy can verify references but cannot be adopted for processing.
The returned manifest and inventory are ordinary projection dictionaries;
changing them does not modify durable evidence or authorize an effect. A
consumer must reopen the frozen reference before making an evidence-bound
decision.

`VerifiedMediaOcrEvidence` contains the verified reference, terminal manifest,
typed blocks and complete required member inventory: `intent.json`,
`original.bin`, `declaration.json`, `normalized.png`, `ocr.json`, `result.json`.
An export/backup of this successful seam must copy and verify the entire unit;
retaining only its PNG loses original and processing evidence. The reference
is relative to the trusted evidence root and can be reopened after an exact
verified copy. Retained incomplete/failure operation directories must also be
preserved rather than silently reclaimed as successful units.

Existing `receipt_ocr_extractions`, original attachment/source hashes, proposal
source bindings and managed snapshot inventory remain unchanged. They do not
automatically consume or back up this new bundle. A later consumer must
explicitly verify this reference and link its original source-event identity
and exact normalized OCR result; it must not pretend the legacy OCR API
consumed the PNG or alias the derivative as the received original.

## Primary decoder contracts

The [libheif API](https://raw.githubusercontent.com/strukturag/libheif/v1.17.6/libheif/heif.h)
defines top-level image counts and normative geometry transformation during
decode. The [binding's orientation contract](https://pillow-heif.readthedocs.io/en/stable/workaround-orientation.html)
distinguishes descriptive EXIF/XMP from container transformation. The pinned
[1.8.0 native source](https://raw.githubusercontent.com/bigcat88/pillow_heif/v1.8.0/pillow_heif/_pillow_heif.c)
exposes ordered actual primary transformation properties. Real acceptance
requires asymmetric pixel/geometry vectors, 10-bit readable-content evidence,
actual local OCR, refusal/replay/resource/crash coverage and an exact installed
Ubuntu worker run; an earlier CLI decoder proof is not this binding's proof.
