"""Private, isolated raster decoder. No database, provider or proposal authority."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import re
import resource
import struct
import sys
import time
import warnings
from pathlib import Path

MAX_SOURCE = 20_000_000
MAX_PIXELS = 24_000_000
MAX_DECODED = 134_217_728
MAX_PNG = 20_000_000
VERSIONS = {"Pillow": "12.3.0", "pillow-heif": "1.8.0"}


class Refused(Exception):
    def __init__(self, status: str, code: str):
        self.status, self.code = status, code


def identity() -> dict:
    import pillow_heif
    from PIL import features

    files = []
    for name, version in VERSIONS.items():
        distribution = importlib.metadata.distribution(name)
        if distribution.version != version:
            raise Refused("engine_failed", "decoder_version_mismatch")
        for member in sorted(distribution.files or (), key=str):
            # Installed wheel native closure and code, excluding volatile bytecode.
            if str(member).endswith((".pyc", ".pyo")):
                continue
            path = Path(str(distribution.locate_file(member)))
            if not path.is_file() or path.is_symlink():
                raise Refused("engine_failed", "decoder_installation_invalid")
            raw = path.read_bytes()
            files.append([name, str(member), len(raw), hashlib.sha256(raw).hexdigest()])
    material = json.dumps(files, separators=(",", ":"), ensure_ascii=True).encode()
    return {
        "packages": VERSIONS,
        "installed_files_sha256": hashlib.sha256(material).hexdigest(),
        "libheif": pillow_heif.libheif_info(),
        "jpeg": features.version_codec("jpg"),
        "zlib": features.version_codec("zlib"),
        "python": list(sys.version_info[:3]),
        "python_prefix": sys.prefix,
        "python_base_prefix": sys.base_prefix,
    }


def heif_brands(raw: bytes) -> tuple[list[str], bool]:
    """Bounded top-level BMFF walk; reject tracks rather than decode one still."""
    offset, count, tracks = 0, 0, False
    brands: list[str] = []
    while offset < len(raw):
        count += 1
        if count > 4096 or len(raw) - offset < 8:
            raise Refused("unsupported_input", "malformed_container")
        size, kind = struct.unpack_from(">I4s", raw, offset)
        header = 8
        if size == 1:
            if len(raw) - offset < 16:
                raise Refused("unsupported_input", "malformed_container")
            size = struct.unpack_from(">Q", raw, offset + 8)[0]
            header = 16
        elif size == 0:
            size = len(raw) - offset
        if size < header or size > len(raw) - offset:
            raise Refused("unsupported_input", "malformed_container")
        if kind == b"ftyp":
            payload = raw[offset + header : offset + size]
            if brands or len(payload) < 8 or len(payload) % 4:
                raise Refused("unsupported_input", "malformed_container")
            brands = [payload[:4].decode("ascii", "strict")]
            brands += [
                payload[i : i + 4].decode("ascii", "strict") for i in range(8, len(payload), 4)
            ]
        tracks |= kind == b"moov"
        offset += size
    return brands, tracks


def sniff(raw: bytes) -> str:
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(raw) >= 12 and raw[4:8] == b"ftyp":
        brands, tracks = heif_brands(raw)
        sequences = {"msf1", "hevc", "hevx", "hevm", "hevs", "avis", "vvis", "j2is", "jpgs"}
        if tracks or sequences.intersection(brands):
            raise Refused("unsupported_input", "sequence_unsupported")
        if {"heic", "heix", "hevc", "hevx"}.intersection(brands):
            return "image/heic"
        if {"mif1", "heif"}.intersection(brands):
            return "image/heif"
    raise Refused("unsupported_input", "format_unsupported")


def orientation(info: dict) -> tuple[int | None, int | None]:
    # getexif() synthesizes Orientation from XMP; only raw TIFF is EXIF evidence.
    exif = None
    raw_exif = info.get("exif")
    if raw_exif is not None:
        if not isinstance(raw_exif, bytes) or len(raw_exif) > 65_536:
            raise Refused("resource_rejected", "metadata_size_limit")
        tiff = raw_exif[6:] if raw_exif.startswith(b"Exif\0\0") else raw_exif
        if len(tiff) < 8 or tiff[:2] not in {b"II", b"MM"}:
            raise Refused("unsupported_input", "orientation_invalid")
        endian = "<" if tiff[:2] == b"II" else ">"
        if struct.unpack_from(endian + "H", tiff, 2)[0] != 42:
            raise Refused("unsupported_input", "orientation_invalid")
        offset = struct.unpack_from(endian + "I", tiff, 4)[0]
        if offset < 8 or offset + 2 > len(tiff):
            raise Refused("unsupported_input", "orientation_invalid")
        entries = struct.unpack_from(endian + "H", tiff, offset)[0]
        if entries > 4096 or offset + 2 + entries * 12 + 4 > len(tiff):
            raise Refused("unsupported_input", "orientation_invalid")
        orientations = []
        for index in range(entries):
            entry = offset + 2 + 12 * index
            tag, field_type, count = struct.unpack_from(endian + "HHI", tiff, entry)
            if tag == 274:
                if field_type != 3 or count != 1:
                    raise Refused("unsupported_input", "orientation_invalid")
                orientations.append(struct.unpack_from(endian + "H", tiff, entry + 8)[0])
        if len(orientations) > 1:
            raise Refused("unsupported_input", "orientation_conflict")
        exif = orientations[0] if orientations else None
    xmp = info.get("xmp", info.get("XML:com.adobe.xmp"))
    values = []
    if xmp is not None:
        if isinstance(xmp, bytes):
            if len(xmp) > 65_536:
                raise Refused("resource_rejected", "metadata_size_limit")
            text = xmp.decode("utf-8", "strict")
        elif isinstance(xmp, str) and len(xmp) <= 65_536:
            text = xmp
        else:
            raise Refused("unsupported_input", "orientation_invalid")
        values = re.findall(
            r'(?:tiff:Orientation\s*=\s*["\']([^"\']+)["\']|<tiff:Orientation>([^<]+)</tiff:Orientation>)',
            text,
        )
        if "Orientation" in text and not values:
            raise Refused("unsupported_input", "orientation_invalid")
    xmp_values = [int(a or b) for a, b in values]
    if len(set(xmp_values)) > 1:
        raise Refused("unsupported_input", "orientation_conflict")
    observed_xmp = xmp_values[0] if xmp_values else None
    for value in (exif, observed_xmp):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value not in range(1, 9)
        ):
            raise Refused("unsupported_input", "orientation_invalid")
    if exif is not None and observed_xmp is not None and exif != observed_xmp:
        raise Refused("unsupported_input", "orientation_conflict")
    return exif, observed_xmp


def geometry(width: int, height: int, channels: int = 4, bits: int = 8) -> None:
    if width <= 0 or height <= 0:
        raise Refused("unsupported_input", "geometry_invalid")
    if width * height > MAX_PIXELS:
        raise Refused("resource_rejected", "decoded_pixel_limit")
    if width * height * channels * ((bits + 7) // 8) > MAX_DECODED:
        raise Refused("resource_rejected", "decoded_byte_limit")


def normalize(source_fd: int, output_fd: int, declaration: dict) -> dict:
    import pillow_heif
    from PIL import Image, ImageFile

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    warnings.simplefilter("error", UserWarning)
    os.lseek(source_fd, 0, os.SEEK_SET)
    with os.fdopen(os.dup(source_fd), "rb") as source:
        raw = source.read(MAX_SOURCE + 1)
    if len(raw) > MAX_SOURCE:
        raise Refused("resource_rejected", "source_size_limit")
    detected = sniff(raw)
    declared = declaration.get("declared_mime_type")
    aliases = {"image/heic": "heif", "image/heif": "heif"}
    if declared is not None and aliases.get(declared, declared) != aliases.get(detected, detected):
        raise Refused("unsupported_input", "declared_type_mismatch")
    filename = declaration.get("original_filename")
    if filename is not None:
        suffixes = Path(filename).suffixes
        allowed = {
            "image/jpeg": {".jpg", ".jpeg"},
            "image/png": {".png"},
            "image/heic": {".heic", ".heif"},
            "image/heif": {".heif", ".heic"},
        }
        if suffixes and suffixes[-1].lower() not in allowed[detected]:
            raise Refused("unsupported_input", "filename_type_mismatch")
    transforms, original_bits, color = [], 8, {}
    if detected in {"image/heic", "image/heif"}:
        pillow_heif.options.DECODE_THREADS = 1
        pillow_heif.options.DISABLE_SECURITY_LIMITS = False
        pillow_heif.options.PREFERRED_DECODER = {"HEIF": "libde265"}
        pillow_heif.options.THUMBNAILS = False
        pillow_heif.options.DEPTH_IMAGES = False
        pillow_heif.options.AUX_IMAGES = False
        container = pillow_heif.open_heif(
            io.BytesIO(raw), convert_hdr_to_8bit=True, hdr_to_16bit=False
        )
        if container.mimetype not in {"image/heic", "image/heif"}:
            raise Refused("unsupported_input", "format_unsupported")
        if len(container) != 1:
            raise Refused("unsupported_input", "multiple_frames")
        primary = container[0]
        original_bits = primary.info.get("bit_depth")
        if original_bits not in {8, 10}:
            raise Refused("unsupported_input", "bit_depth_unsupported")
        # Pinned 1.8.0 native capability; missing evidence is never guessed.
        transforms = [list(item) for item in primary._c_image.transformations]
        if len(transforms) > 32 or any(
            item[0] not in {"irot", "imir", "clap"} for item in transforms
        ):
            raise Refused("unsupported_input", "transform_unsupported")
        before = list(primary.size)
        geometry(*primary.size, channels=4, bits=8)
        info = primary.info.copy()
        # Preserve metadata before to_pillow resets EXIF/XMP orientation.
        exif, xmp = orientation(info)
        if (exif not in (None, 1) or xmp not in (None, 1)) and not any(
            item[0] in {"irot", "imir"} for item in transforms
        ):
            raise Refused("unsupported_input", "ambiguous_orientation")
        for key in ("icc_profile", "nclx_profile"):
            value = info.get(key)
            if value is not None:
                data = (
                    value
                    if isinstance(value, bytes)
                    else json.dumps(value, sort_keys=True).encode()
                )
                if len(data) > 65_536:
                    raise Refused("resource_rejected", "metadata_size_limit")
                color[key + "_sha256"] = hashlib.sha256(data).hexdigest()
        image = primary.to_pillow()
        if primary.stride * primary.size[1] > MAX_DECODED:
            raise Refused("resource_rejected", "decoded_byte_limit")
        policy = "heif_container_once_metadata_removed"
    else:
        image = Image.open(io.BytesIO(raw), formats=["JPEG", "PNG"])
        if image.format != {"image/jpeg": "JPEG", "image/png": "PNG"}[detected]:
            raise Refused("unsupported_input", "format_mismatch")
        if getattr(image, "n_frames", 1) != 1:
            raise Refused("unsupported_input", "multiple_frames")
        before = list(image.size)
        if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA", "CMYK"}:
            raise Refused("unsupported_input", "pixel_mode_unsupported")
        geometry(*image.size)
        # eXIf/XMP may follow PNG IDAT; observe metadata after strict bounded load.
        image.load()
        icc = image.info.get("icc_profile")
        if icc is not None:
            if not isinstance(icc, bytes) or len(icc) > 65_536:
                raise Refused("resource_rejected", "metadata_size_limit")
            color["icc_profile_sha256"] = hashlib.sha256(icc).hexdigest()
        exif, xmp = orientation(image.info)
        # XMP-only raster orientation is not silently substituted for EXIF.
        if exif is None and xmp not in (None, 1):
            raise Refused("unsupported_input", "ambiguous_orientation")
        transposes = {
            2: Image.Transpose.FLIP_LEFT_RIGHT,
            3: Image.Transpose.ROTATE_180,
            4: Image.Transpose.FLIP_TOP_BOTTOM,
            5: Image.Transpose.TRANSPOSE,
            6: Image.Transpose.ROTATE_270,
            7: Image.Transpose.TRANSVERSE,
            8: Image.Transpose.ROTATE_90,
        }
        if exif in transposes:
            image = image.transpose(transposes[exif])
        policy = "raster_exif_once_metadata_removed"
    geometry(*image.size)
    source_has_alpha = image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    )
    if source_has_alpha:
        rgba = image.convert("RGBA")
        image = Image.new("RGB", rgba.size, (255, 255, 255))
        image.paste(rgba, mask=rgba.getchannel("A"))
    else:
        image = image.convert("RGB")
    geometry(*image.size, channels=3)
    # No metadata forwarded; fixed compression, no resizing or lossy PNG stage.
    image.info.clear()
    with os.fdopen(os.dup(output_fd), "wb") as output:
        image.save(
            output,
            format="PNG",
            compress_level=6,
            optimize=False,
            icc_profile=None,
            exif=None,
            pnginfo=None,
        )
        output.flush()
    length = os.fstat(output_fd).st_size
    if length > MAX_PNG:
        raise Refused("resource_rejected", "png_size_limit")
    return {
        "detected_mime_type": detected,
        "source_dimensions": before,
        "dimensions": list(image.size),
        "original_bit_depth": original_bits,
        "output_mode": "RGB",
        "alpha_policy": "white_matte",
        "source_has_alpha": source_has_alpha,
        "convert_hdr_to_8bit": True,
        "container_transformations": transforms,
        "exif_orientation": exif,
        "xmp_orientation": xmp,
        "orientation_policy": policy,
        "color_profile": color,
        "frame_count": 1,
        "decoded_bytes": image.size[0] * image.size[1] * 3,
    }


def main() -> None:
    started = time.monotonic()
    result: dict = {}
    tasks: int | None = None
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity", action="store_true")
    parser.add_argument("--source-fd", type=int)
    parser.add_argument("--output-fd", type=int)
    parser.add_argument("--declaration-fd", type=int)
    args = parser.parse_args()
    try:
        if not sys.platform.startswith("linux"):
            raise Refused("engine_failed", "linux_required")
        if os.getuid() == 0:
            raise Refused("engine_failed", "nonroot_worker_required")
        own_status = Path("/proc/self/status").read_text()
        for field in ("CapEff", "CapPrm"):
            match = re.search(r"^" + field + r":\s+([0-9a-fA-F]+)$", own_status, re.MULTILINE)
            if match is None or int(match[1], 16) != 0:
                raise Refused("engine_failed", "worker_capabilities_refused")
        # RLIMIT_NPROC counts real-UID tasks, including threads in other
        # processes. Do not pretend a shared UID has a per-worker quota.
        tasks = 0
        try:
            for process in Path("/proc").iterdir():
                if not process.name.isdecimal():
                    continue
                try:
                    status = (process / "status").read_text()
                    uid = re.search(r"^Uid:\s+(\d+)", status, re.MULTILINE)
                    if uid is not None and int(uid[1]) == os.getuid():
                        tasks += len(list((process / "task").iterdir()))
                except FileNotFoundError:
                    continue  # A process exited during the snapshot.
            if tasks >= 16:
                raise Refused("resource_rejected", "decoder_uid_task_limit")
        except PermissionError:
            raise Refused("resource_rejected", "decoder_uid_headroom_unknown") from None
        observed = identity()
        if args.identity:
            result = {"status": "identity", "decoder": observed, "uid_tasks": tasks}
        else:
            with os.fdopen(os.dup(args.declaration_fd), "rb") as declaration_file:
                declaration = json.loads(declaration_file.read(4097))
            metadata = normalize(args.source_fd, args.output_fd, declaration)
            result = {
                "status": "normalized",
                "decoder": observed,
                "normalization": metadata,
                "uid_tasks": tasks,
            }
    except Refused as exc:
        result = {"status": exc.status, "outcome_code": exc.code}
    except (OSError, ValueError, SyntaxError, EOFError, UnicodeError, struct.error):
        result = {"status": "unsupported_input", "outcome_code": "decode_invalid"}
    except MemoryError:
        result = {"status": "resource_rejected", "outcome_code": "decoder_memory_limit"}
    except Exception:
        result = {"status": "engine_failed", "outcome_code": "decoder_failed"}
    usage = resource.getrusage(resource.RUSAGE_SELF)
    if tasks is not None:
        result["uid_tasks"] = tasks
    result["usage"] = {
        "wall_seconds": time.monotonic() - started,
        "user_cpu_seconds": usage.ru_utime,
        "system_cpu_seconds": usage.ru_stime,
        "max_rss_kib": usage.ru_maxrss,
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
