"""Minimal PNG decode + pixel comparison (dependency-free).

Decodes the screenshots Playwright produces (8-bit truecolor or
truecolor+alpha, non-interlaced) using only zlib: signature, IHDR, IDAT
(unfiltering types 0-4), IEND. Anything else — other bit depths, color
types, or interlacing — raises :class:`PngError` so gates report
BLOCKED instead of comparing bytes they cannot see.

Comparison counts pixels whose channels differ beyond tolerance (with
policy-frozen protected regions excluded) and reports the differing
fraction. Missing or undecodable images never default to equal.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

_PNG_SIGNATURE = bytes.fromhex("89504e470d0a1a0a")


class PngError(ValueError):
    """PNG bytes are missing, truncated, or use an unsupported encoding."""


@dataclass(frozen=True)
class DecodedPng:
    width: int
    height: int
    channels: int
    pixels: bytes  # row-major, channels per pixel


def decode_png(data: bytes) -> DecodedPng:
    """Decode 8-bit RGB/RGBA PNG bytes; raises PngError otherwise."""
    if not data.startswith(_PNG_SIGNATURE):
        raise PngError("not PNG bytes (bad signature)")
    pos = len(_PNG_SIGNATURE)
    width = height = channels = 0
    seen_ihdr = False
    idat_parts: list[bytes] = []
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        start = pos + 8
        end = start + length
        if end + 4 > len(data):
            raise PngError("truncated PNG chunk")
        payload = data[start:end]
        pos = end + 4  # skip CRC (integrity comes from transport + hashes)
        if ctype == b"IHDR":
            if seen_ihdr or length != 13:
                raise PngError("malformed IHDR")
            seen_ihdr = True
            width, height, depth, color, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if depth != 8 or color not in (2, 6):
                raise PngError(f"unsupported PNG: depth {depth}, color type {color}")
            if interlace != 0:
                raise PngError("interlaced PNG is not supported")
            if not (1 <= width <= 10000 and 1 <= height <= 10000):
                raise PngError(f"implausible dimensions {width}x{height}")
            channels = 3 if color == 2 else 4
        elif ctype == b"IDAT":
            idat_parts.append(payload)
        elif ctype == b"IEND":
            break
    if not seen_ihdr or not idat_parts:
        raise PngError("PNG has no image data")
    try:
        raw = zlib.decompress(b"".join(idat_parts))
    except zlib.error as exc:
        raise PngError(f"IDAT decompression failed: {exc}")
    stride = width * channels
    expected = (stride + 1) * height
    if len(raw) < expected:
        raise PngError("IDAT too short for the declared dimensions")
    pixels = bytearray(stride * height)
    previous = bytearray(stride)
    offset = 0
    for y in range(height):
        filter_type = raw[offset]
        offset += 1
        if filter_type > 4:
            raise PngError(f"unknown filter type {filter_type}")
        current = bytearray(raw[offset : offset + stride])
        offset += stride
        if filter_type == 1:  # Sub
            for i in range(channels, stride):
                current[i] = (current[i] + current[i - channels]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(stride):
                current[i] = (current[i] + previous[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(stride):
                left = current[i - channels] if i >= channels else 0
                current[i] = (current[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(stride):
                left = current[i - channels] if i >= channels else 0
                up = previous[i]
                upper_left = previous[i - channels] if i >= channels else 0
                p = left + up - upper_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upper_left)
                predictor = left if (pa <= pb and pa <= pc) else (up if pb <= pc else upper_left)
                current[i] = (current[i] + predictor) & 0xFF
        pixels[y * stride : (y + 1) * stride] = current
        previous = current
    return DecodedPng(width, height, channels, bytes(pixels))


def compare_pixels(
    first: DecodedPng,
    second: DecodedPng,
    *,
    channel_tolerance: int,
    protected_regions: tuple[tuple[int, int, int, int], ...] = (),
) -> tuple[int, int, float]:
    """Count differing pixels; returns (differing, compared, fraction).

    Images must share dimensions and channel counts or PngError is
    raised. Protected regions are excluded from both counts.
    """
    if (first.width, first.height, first.channels) != (
        second.width, second.height, second.channels,
    ):
        raise PngError(
            f"dimension mismatch: {first.width}x{first.height}x{first.channels} vs "
            f"{second.width}x{second.height}x{second.channels}"
        )

    def protected(x: int, y: int) -> bool:
        return any(rx <= x < rx + rw and ry <= y < ry + rh for rx, ry, rw, rh in protected_regions)

    differing = compared = 0
    stride = first.width * first.channels
    for y in range(first.height):
        for x in range(first.width):
            if protected(x, y):
                continue
            compared += 1
            base = y * stride + x * first.channels
            for c in range(first.channels):
                if abs(first.pixels[base + c] - second.pixels[base + c]) > channel_tolerance:
                    differing += 1
                    break
    fraction = (differing / compared) if compared else 1.0
    return differing, compared, fraction
