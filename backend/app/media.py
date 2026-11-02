"""Media validation for evidence uploads.

Dependency-free magic-byte identification plus best-effort dimension parsing:

- PNG: signature + IHDR dimensions (required).
- JPEG: SOI + SOF0/1/2/3 dimensions (required; files without a start-of-frame
  marker are rejected as unreadable).
- WebP: RIFF/WEBP container (required); VP8/VP8L/VP8X dimensions best-effort
  (accepted with ``None`` dimensions when the image-data chunk is unknown,
  e.g. animation frames nested in ANMF chunks).
- MP4: ``ftyp`` box (dimensions/duration are client-reported, see below).
- WebM: EBML header magic (dimensions/duration are client-reported).

Video container parsing (mvhd/tkhd, EBML segment info) is deliberately out of
scope for M1: MP4/WebM uploads are accepted on container magic and the
frontend reports width/height/duration from the decoded stream. Those values
are sanity-checked (1..10000 px, non-negative duration) and stored as
client-reported metadata, never as server-verified facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class MediaValidationError(ValueError):
    """Raised when uploaded bytes are not acceptable evidence media."""


@dataclass(frozen=True)
class MediaInfo:
    content_type: str
    extension: str  # without dot; server-chosen, never from the client
    width: Optional[int]
    height: Optional[int]


PNG_SIGNATURE = bytes.fromhex("89504e470d0a1a0a")
JPEG_SOI = b"\xff\xd8"
EBML_MAGIC = bytes.fromhex("1a45dfa3")
_HEADER_BYTES = 64 * 1024
_JPEG_SCAN_LIMIT = 4 * 1024 * 1024

_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
_VIDEO_TYPES = {"video/mp4", "video/webm"}
# Client aliases normalized before comparison.
_TYPE_ALIASES = {"image/jpg": "image/jpeg"}


def identify_media(
    path: Path, declared_content_type: Optional[str], family: str
) -> MediaInfo:
    """Identify and validate an uploaded media file.

    ``family`` is ``"image"`` or ``"video"``. Detection is by magic bytes;
    the client-declared content type must agree when it is specific (a
    missing or ``application/octet-stream`` declaration defers to detection).
    """
    with open(path, "rb") as handle:
        head = handle.read(_HEADER_BYTES)
    if not head:
        raise MediaValidationError("uploaded file is empty")

    info: Optional[MediaInfo] = None
    if head.startswith(PNG_SIGNATURE):
        info = _png_info(head)
    elif head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        info = _webp_info(head)
    elif head.startswith(JPEG_SOI):
        info = _jpeg_info(path)
    elif len(head) >= 12 and head[4:8] == b"ftyp":
        info = MediaInfo("video/mp4", "mp4", None, None)
    elif head.startswith(EBML_MAGIC):
        info = MediaInfo("video/webm", "webm", None, None)

    if info is None:
        raise MediaValidationError(
            "unrecognized file format: expected PNG, JPEG, WebP, MP4, or WebM"
        )

    actual_family = "image" if info.content_type in _IMAGE_TYPES else "video"
    if actual_family != family:
        raise MediaValidationError(
            f"bytes are {info.content_type} but a {family} was declared"
        )

    declared = (declared_content_type or "").split(";")[0].strip().lower()
    declared = _TYPE_ALIASES.get(declared, declared)
    if declared and declared != "application/octet-stream" and declared != info.content_type:
        raise MediaValidationError(
            f"declared content type {declared!r} does not match detected {info.content_type!r}"
        )
    return info


def _png_info(head: bytes) -> MediaInfo:
    if len(head) < 24 or head[12:16] != b"IHDR":
        raise MediaValidationError("PNG is truncated or missing its IHDR chunk")
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    if not (1 <= width <= 10000 and 1 <= height <= 10000):
        raise MediaValidationError("PNG dimensions are out of range")
    return MediaInfo("image/png", "png", width, height)


def _jpeg_info(path: Path) -> MediaInfo:
    """Scan JPEG markers for a start-of-frame segment carrying dimensions."""
    with open(path, "rb") as handle:
        data = handle.read(_JPEG_SCAN_LIMIT)
    # Standalone markers without a length field.
    standalone = {0xD8, 0xD9, 0x01} | set(range(0xD0, 0xD8))
    pos = 2  # skip SOI
    while pos + 1 < len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker == 0xFF or marker == 0x00:  # padding / stuffed byte
            pos += 2
            continue
        if marker == 0xDA:  # SOS: entropy-coded data follows; SOF was missed
            break
        if marker in standalone:
            pos += 2
            continue
        if pos + 3 >= len(data):
            break
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if length < 2 or pos + 2 + length > len(data):
            break
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):  # SOF0..SOF3
            segment = data[pos + 4 : pos + 2 + length]
            if len(segment) >= 5:
                height = int.from_bytes(segment[1:3], "big")
                width = int.from_bytes(segment[3:5], "big")
                if 1 <= width <= 10000 and 1 <= height <= 10000:
                    return MediaInfo("image/jpeg", "jpg", width, height)
            raise MediaValidationError("JPEG frame dimensions are out of range")
        pos += 2 + length
    raise MediaValidationError("not a readable JPEG: no start-of-frame marker found")


def _webp_info(head: bytes) -> MediaInfo:
    """Parse top-level WebP chunks; dimensions are best-effort (may be None)."""
    width = height = None
    pos = 12
    while pos + 8 <= len(head):
        fourcc = head[pos : pos + 4]
        size = int.from_bytes(head[pos + 4 : pos + 8], "little")
        payload = head[pos + 8 : pos + 8 + size]
        if fourcc == b"VP8X" and len(payload) >= 7:
            width = int.from_bytes(payload[1:4], "little") + 1
            height = int.from_bytes(payload[4:7], "little") + 1
            break
        if fourcc == b"VP8" and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
            width = int.from_bytes(payload[6:8], "little") & 0x3FFF
            height = int.from_bytes(payload[8:10], "little") & 0x3FFF
            break
        if fourcc == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
            packed = int.from_bytes(payload[1:5], "little")
            width = (packed & 0x3FFF) + 1
            height = ((packed >> 14) & 0x3FFF) + 1
            break
        pos += 8 + size + (size % 2)
    if width is not None and not (1 <= width <= 10000 and 1 <= height <= 10000):
        width = height = None
    return MediaInfo("image/webp", "webp", width, height)


def sanitize_filename(name: str, fallback: str = "evidence") -> str:
    """Reduce a client-provided filename to a safe display/download name.

    The result is metadata only: storage paths never use client filenames.
    """
    base = (name or "").replace("\\", "/").split("/")[-1].strip().strip(".")
    base = "".join(
        char if (char.isprintable() and char not in '<>:"|?*') else "_"
        for char in base
    )
    base = re.sub(r"\s+", " ", base).strip()
    if not base:
        base = fallback
    return base[:180]
