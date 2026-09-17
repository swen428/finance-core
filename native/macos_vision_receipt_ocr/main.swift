// Bounded macOS Apple Vision receipt OCR helper — protocol version 1.
//
// Staging-only native helper for the Finance receipt OCR evidence boundary.
// This process is launched by src/intake/macos_vision_receipt_ocr.py with:
//
//   macos_vision_receipt_ocr --identity
//   macos_vision_receipt_ocr --ocr <fd> <max-bytes> <languages> <max-blocks> \
//       <max-chars> <max-image-width> <max-image-height> <max-total-text>
//
// Contract:
// - Reads the already-opened attachment exclusively through the inherited
//   file descriptor; never opens caller-supplied paths.
// - Reads no more than the configured attachment bound.
// - Validates the requested languages against the host's real Vision
//   capability (fixed revision 3, accurate) BEFORE reading or decoding the
//   image; an unsupported language is a configuration error (exit 2).
// - Applies the caller's effective image-width/image-height limits to the
//   orientation-corrected image properties BEFORE decoding pixels or running
//   Vision (exit 3); no independent fixed pixel cap is used.
// - Accumulates and enforces the caller's effective total-text limit BEFORE
//   constructing stdout (exit 4).
// - Emits exactly one bounded UTF-8 JSON result to stdout.
// - stderr is diagnostic-only and must never be persisted by the caller.
// - No network, no telemetry, no downloaded models, no retry, no automatic
//   language detection, no language correction.
// - Uses VNRecognizeTextRequest locally on-device with accurate recognition,
//   language correction disabled, explicit revision 3, explicit languages.
//
// Exit codes:
//   0  success (one valid JSON result on stdout)
//   1  genuine OCR / image / Vision runtime failure
//   2  usage, argument, protocol, or configuration error (incl. unsupported language)
//   3  input bound violation (attachment bytes or image dimensions)
//   4  output bound violation (block count, per-block text, or total text)
//
// Build (reproducible, Apple system frameworks only, no packages):
//   xcrun swiftc -O -swift-version 5 \
//       -module-cache-path /tmp/receipt-ocr-module-cache \
//       -o macos_vision_receipt_ocr \
//       native/macos_vision_receipt_ocr/main.swift
//   chmod 0500 macos_vision_receipt_ocr

import CoreGraphics
import Foundation
import ImageIO
import Vision

// ---------------------------------------------------------------------------
// Protocol constants (bound into the Python engine configuration hash)
// ---------------------------------------------------------------------------

let PROTOCOL_VERSION = 1
let HELPER_NAME = "macos_vision_receipt_ocr"
let HELPER_VERSION = "1.0.0"
let VISION_REQUEST_REVISION = 3
let RECOGNITION_LEVEL = "accurate"
let USES_LANGUAGE_CORRECTION = false

// ---------------------------------------------------------------------------
// Diagnostic-only stderr (never persisted by the Python boundary)
// ---------------------------------------------------------------------------

func diagnose(_ code: String) {
    FileHandle.standardError.write(Data("\(code)\n".utf8))
}

// ---------------------------------------------------------------------------
// Deterministic JSON string escaping (UTF-8 passthrough for printable text)
// ---------------------------------------------------------------------------

func jsonEscape(_ value: String) -> String {
    var escaped = ""
    escaped.reserveCapacity(value.count + 8)
    for scalar in value.unicodeScalars {
        switch scalar.value {
        case 0x22: escaped += "\\\""
        case 0x5C: escaped += "\\\\"
        case 0x08: escaped += "\\b"
        case 0x0C: escaped += "\\f"
        case 0x0A: escaped += "\\n"
        case 0x0D: escaped += "\\r"
        case 0x09: escaped += "\\t"
        case 0x00...0x1F:
            escaped += String(format: "\\u%04x", scalar.value)
        default:
            escaped.unicodeScalars.append(scalar)
        }
    }
    return escaped
}

// ---------------------------------------------------------------------------
// Real Vision language capability (fixed revision 3, accurate)
// ---------------------------------------------------------------------------

func querySupportedLanguages() -> [String]? {
    do {
        let supported = try VNRecognizeTextRequest.supportedRecognitionLanguages(
            for: .accurate, revision: VISION_REQUEST_REVISION)
        return supported.sorted()
    } catch {
        return nil
    }
}

// ---------------------------------------------------------------------------
// Identity command
// ---------------------------------------------------------------------------

func runIdentity() -> Int32 {
    guard let supported = querySupportedLanguages() else {
        diagnose("language_capability_unavailable")
        return 1
    }
    var langsJson = ""
    for (position, lang) in supported.enumerated() {
        if position > 0 {
            langsJson += ","
        }
        langsJson += "\"\(jsonEscape(lang))\""
    }
    let json = "{"
        + "\"protocol_version\":\(PROTOCOL_VERSION),"
        + "\"helper_name\":\"\(HELPER_NAME)\","
        + "\"helper_version\":\"\(HELPER_VERSION)\","
        + "\"vision_request_revision\":\(VISION_REQUEST_REVISION),"
        + "\"recognition_level\":\"\(RECOGNITION_LEVEL)\","
        + "\"uses_language_correction\":\(USES_LANGUAGE_CORRECTION),"
        + "\"supported_languages\":[\(langsJson)]"
        + "}\n"
    FileHandle.standardOutput.write(Data(json.utf8))
    return 0
}

// ---------------------------------------------------------------------------
// Bounded fd read
// ---------------------------------------------------------------------------

func readBounded(fd: Int32, maxBytes: Int) -> Data? {
    var data = Data()
    let chunkSize = 65_536
    let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: chunkSize)
    defer { buffer.deallocate() }
    while true {
        let want = min(chunkSize, maxBytes + 1 - data.count)
        if want <= 0 { break }
        let n = read(fd, buffer, want)
        if n < 0 {
            if errno == EINTR { continue }
            return nil
        }
        if n == 0 { break }
        data.append(buffer, count: n)
    }
    if data.count > maxBytes {
        return nil
    }
    return data
}

// ---------------------------------------------------------------------------
// OCR command
// ---------------------------------------------------------------------------

struct Observation {
    let index: Int
    let text: String
    let confidence: Float
    let box: CGRect
}

func runOcr(arguments: [String]) -> Int32 {
    guard arguments.count == 8 else {
        diagnose("usage_error")
        return 2
    }
    guard let fd = Int32(arguments[0]), fd >= 0 else {
        diagnose("invalid_fd")
        return 2
    }
    guard let maxBytes = Int(arguments[1]), maxBytes > 0, maxBytes <= 100_000_000 else {
        diagnose("invalid_max_bytes")
        return 2
    }
    let languages = arguments[2].split(separator: ",").map(String.init)
    guard !languages.isEmpty, languages.count <= 8 else {
        diagnose("invalid_languages")
        return 2
    }
    guard let maxBlocks = Int(arguments[3]), maxBlocks > 0, maxBlocks <= 100_000 else {
        diagnose("invalid_max_blocks")
        return 2
    }
    guard let maxChars = Int(arguments[4]), maxChars > 0, maxChars <= 65_536 else {
        diagnose("invalid_max_chars")
        return 2
    }
    guard let maxWidth = Int(arguments[5]), maxWidth > 0, maxWidth <= 100_000 else {
        diagnose("invalid_max_width")
        return 2
    }
    guard let maxHeight = Int(arguments[6]), maxHeight > 0, maxHeight <= 100_000 else {
        diagnose("invalid_max_height")
        return 2
    }
    guard let maxTotalText = Int(arguments[7]), maxTotalText > 0, maxTotalText <= 2_000_000 else {
        diagnose("invalid_max_total_text")
        return 2
    }

    // Validate the requested languages against the host's real Vision
    // capability BEFORE reading or decoding the image. A language Vision will
    // reject is a configuration error and fails closed (exit 2).
    guard let supported = querySupportedLanguages() else {
        diagnose("language_capability_unavailable")
        return 1
    }
    let supportedSet = Set(supported)
    for language in languages where !supportedSet.contains(language) {
        diagnose("unsupported_language")
        return 2
    }

    // Bounded read of the already-opened attachment descriptor.
    guard let imageData = readBounded(fd: fd, maxBytes: maxBytes) else {
        diagnose("input_bound_exceeded")
        return 3
    }
    guard !imageData.isEmpty else {
        diagnose("empty_input")
        return 1
    }

    // Read image identity (metadata only) without repairing or rewriting the
    // attachment, and enforce the caller's effective image limits BEFORE any
    // pixel decode or Vision run.
    guard let source = CGImageSourceCreateWithData(imageData as CFData, nil) else {
        diagnose("image_decode_failed")
        return 1
    }
    guard let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil)
        as? [CFString: Any] else {
        diagnose("image_properties_failed")
        return 1
    }
    guard let pixelWidth = properties[kCGImagePropertyPixelWidth] as? Int,
          let pixelHeight = properties[kCGImagePropertyPixelHeight] as? Int else {
        diagnose("image_dimensions_invalid")
        return 1
    }
    guard pixelWidth > 0, pixelHeight > 0 else {
        diagnose("image_dimensions_invalid")
        return 1
    }
    let orientationRaw = properties[kCGImagePropertyOrientation] as? Int ?? 1
    guard let orientation = CGImagePropertyOrientation(rawValue: UInt32(orientationRaw)) else {
        diagnose("image_orientation_invalid")
        return 1
    }

    // Orientation-corrected display dimensions, checked against the caller's
    // effective limits before decoding pixels.
    let pageWidth: Int
    let pageHeight: Int
    switch orientation {
    case .left, .right, .leftMirrored, .rightMirrored:
        pageWidth = pixelHeight
        pageHeight = pixelWidth
    default:
        pageWidth = pixelWidth
        pageHeight = pixelHeight
    }
    if pageWidth > maxWidth || pageHeight > maxHeight {
        diagnose("image_bound_exceeded")
        return 3
    }

    guard let cgImage = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        diagnose("image_create_failed")
        return 1
    }

    // Explicit Vision configuration: pinned revision, accurate recognition,
    // language correction disabled, explicit ordered languages, on-device only.
    let request = VNRecognizeTextRequest()
    request.revision = VISION_REQUEST_REVISION
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = USES_LANGUAGE_CORRECTION
    request.recognitionLanguages = languages

    let handler = VNImageRequestHandler(cgImage: cgImage, orientation: orientation)
    do {
        try handler.perform([request])
    } catch {
        // Never emit Foundation error descriptions as structured output.
        diagnose("vision_request_failed")
        return 1
    }

    var observations: [Observation] = []
    var totalText = 0
    if let results = request.results {
        for (index, observation) in results.enumerated() {
            guard let candidate = observation.topCandidates(1).first else {
                continue
            }
            let text = candidate.string
            if text.isEmpty {
                continue
            }
            if text.count > maxChars {
                diagnose("text_bound_exceeded")
                return 4
            }
            totalText += text.count
            if totalText > maxTotalText {
                diagnose("total_text_bound_exceeded")
                return 4
            }
            observations.append(Observation(
                index: index,
                text: text,
                confidence: candidate.confidence,
                box: observation.boundingBox
            ))
            if observations.count > maxBlocks {
                diagnose("block_bound_exceeded")
                return 4
            }
        }
    }

    let status = observations.isEmpty ? "no_text" : "ok"
    let outcomeCode = observations.isEmpty ? "no_text" : "ok"

    // One bounded deterministic UTF-8 JSON result to stdout.
    var json = "{"
    json += "\"protocol_version\":\(PROTOCOL_VERSION),"
    json += "\"status\":\"\(status)\","
    json += "\"outcome_code\":\"\(outcomeCode)\","
    json += "\"page_width\":\(pageWidth),"
    json += "\"page_height\":\(pageHeight),"
    json += "\"orientation\":\(orientationRaw),"
    json += "\"observations\":["
    for (position, observation) in observations.enumerated() {
        if position > 0 {
            json += ","
        }
        let confidence = String(format: "%.6f", Double(observation.confidence))
        let x = String(format: "%.9f", observation.box.origin.x)
        let y = String(format: "%.9f", observation.box.origin.y)
        let w = String(format: "%.9f", observation.box.size.width)
        let h = String(format: "%.9f", observation.box.size.height)
        json += "{"
        json += "\"index\":\(observation.index),"
        json += "\"text\":\"\(jsonEscape(observation.text))\","
        json += "\"confidence\":\(confidence),"
        json += "\"bounding_box\":[\(x),\(y),\(w),\(h)]"
        json += "}"
    }
    json += "]}\n"

    guard let outputData = json.data(using: .utf8) else {
        diagnose("output_encoding_failed")
        return 1
    }
    FileHandle.standardOutput.write(outputData)
    return 0
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

let arguments = CommandLine.arguments
guard arguments.count >= 2 else {
    diagnose("usage_error")
    exit(2)
}

switch arguments[1] {
case "--identity":
    exit(runIdentity())
case "--ocr":
    exit(runOcr(arguments: Array(arguments[2...])))
default:
    diagnose("usage_error")
    exit(2)
}
