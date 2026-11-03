"""Meta Model API adapter.

Schemas verified against the official docs on 2026-11-03
(https://dev.meta.ai/docs/...):

- Base URL ``https://api.meta.ai/v1``; ``Authorization: Bearer <key>``
  (see ``docs/authentication``).
- Text models use the Responses API: ``POST /v1/responses`` with
  ``{"model", "input", ...}`` (see ``docs/api-reference/responses``).
- ``sam-3.1`` also goes through ``POST /v1/responses``: one user message
  with an ``input_text`` concept prompt plus one ``input_image`` part
  (public URL or base64 data URL). Each match returns a box and a mask;
  zero matches is a normal completed result, not an error
  (see ``docs/sam/segmenting``).

Smoke tests are small and bounded: one short completion per text model
(``reasoning.effort=minimal``, tiny ``max_output_tokens``, ``store=false``)
and one segmentation call over a generated owned test image. Prompts are
fixed constants; no incident data or credentials ever enter a prompt.
"""

from __future__ import annotations

import base64
import re
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .. import config
from .base import (
    CheckResult,
    ProviderReport,
    bounded_request,
    error_detail,
    sanitize,
    summarize,
)

SPARK_MODEL = "muse-spark-1.3"
WONDER_MODEL = "muse-wonder-1.0"
SAM_MODEL = "sam-3.1"

_SMOKE_INPUT = "Reply with the single word: pong"
_SMOKE_MAX_OUTPUT_TOKENS = 64
_SAM_CONCEPT = "red square"
_SAM_TEST_WIDTH = 96
_SAM_TEST_HEIGHT = 64


class SamParseError(ValueError):
    """SAM output_text does not follow the documented segmentation grammar."""


class MetaInferenceError(Exception):
    """A Meta model call failed, timed out, or returned an incomplete result."""


@dataclass(frozen=True)
class InferenceResult:
    """One completed model call: text plus observed request metadata."""

    model: str
    text: str
    status: str
    latency_ms: int
    attempts: int
    usage: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class SamMatch:
    object_id: int
    x1: int
    y1: int
    x2: int
    y2: int
    frame_w: int
    frame_h: int
    mask_h: int
    mask_w: int


_FRAME_RE = re.compile(r"<(\d+)f>")
_RECORD_SPLIT_RE = re.compile(r",(?=\d+<\|box)")
_RECORD_RE = re.compile(
    r"(\d+)"  # object id
    r"<\|box;x1=(\d+);y1=(\d+);x2=(\d+);y2=(\d+);w=(\d+);h=(\d+)(?:;c=([^|]*))?\|>"
    r"<\|mask;x=0;y=0(?:;c=[^|;]*)?;data=(\d+),(\d+),([^|]+)\|>"
)


def parse_sam_output(text: str, *, frame_w: int, frame_h: int) -> list[SamMatch]:
    """Parse one SAM ``output_text`` lane into validated geometry.

    Grammar per dev.meta.ai/docs/sam/reading-segmentation: one line per
    frame, ``<Nf>`` marker followed by comma-separated records of
    ``id + box + mask``; box coordinates are inclusive pixels within the
    reported ``w``/``h`` frame. Empty text means zero matches, which the
    docs call a normal result. Every returned box is checked against the
    owned test image dimensions the request actually sent.
    """
    stripped = (text or "").strip()
    if not stripped:
        return []
    lines = [line for line in stripped.splitlines() if line.strip()]
    if len(lines) != 1:
        raise SamParseError(f"expected one frame line for a still image, got {len(lines)}")
    line = lines[0]
    frame = _FRAME_RE.match(line)
    if not frame:
        raise SamParseError(f"missing <Nf> frame marker: {line[:80]!r}")
    if int(frame.group(1)) != 0:
        raise SamParseError(f"single image must emit frame <0f>, got <{frame.group(1)}f>")
    rest = line[frame.end():]
    if not rest:
        return []
    matches: list[SamMatch] = []
    for part in _RECORD_SPLIT_RE.split(rest):
        record = _RECORD_RE.fullmatch(part)
        if not record:
            raise SamParseError(f"unparseable record: {part[:120]!r}")
        (oid, x1, y1, x2, y2, w, h, mh, mw) = (
            int(record.group(1)),
            int(record.group(2)),
            int(record.group(3)),
            int(record.group(4)),
            int(record.group(5)),
            int(record.group(6)),
            int(record.group(7)),
            int(record.group(9)),
            int(record.group(10)),
        )
        if w != frame_w or h != frame_h:
            raise SamParseError(f"box frame {w}x{h} does not match image {frame_w}x{frame_h}")
        if not (0 <= x1 <= x2 < w and 0 <= y1 <= y2 < h):
            raise SamParseError(f"box ({x1},{y1})-({x2},{y2}) outside {w}x{h}")
        if mh < 1 or mw < 1:
            raise SamParseError(f"mask raster {mw}x{mh} is degenerate")
        matches.append(SamMatch(oid, x1, y1, x2, y2, w, h, mh, mw))
    return matches


def make_sam_test_image(width: int = _SAM_TEST_WIDTH, height: int = _SAM_TEST_HEIGHT) -> bytes:
    """Generate the owned SAM smoke-test image: a red square on blue."""
    blue = bytes([30, 80, 200])
    red = bytes([220, 40, 40])
    rows = []
    for y in range(height):
        row = bytearray(b"\x00")
        for x in range(width):
            inside = width // 4 <= x < 3 * width // 4 and height // 4 <= y < 3 * height // 4
            row += red if inside else blue
        rows.append(bytes(row))

    def chunk(ctype: bytes, payload: bytes) -> bytes:
        body = ctype + payload
        return (
            struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        bytes.fromhex("89504e470d0a1a0a")
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


def _output_text(body: Any) -> str:
    """Defensively extract aggregated output text from a Responses payload."""
    if not isinstance(body, dict):
        return ""
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    parts: list[str] = []
    output = body.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
    return "".join(parts)


class MetaAdapter:
    name = "meta"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url or config.META_API_BASE_URL
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self._transport)

    def _secrets(self) -> list[str]:
        return [self._api_key] if self._api_key else []

    def status(self) -> ProviderReport:
        """Readiness without network traffic."""
        if not self._api_key:
            return ProviderReport(
                name=self.name,
                state="not_configured",
                detail="Set META_API_KEY in .env (create one at dev.meta.ai → API keys).",
                checks=[
                    CheckResult(name="spark", state="not_configured", detail="META_API_KEY missing"),
                    CheckResult(name="wonder", state="not_configured", detail="META_API_KEY missing"),
                    CheckResult(name="sam", state="not_configured", detail="META_API_KEY missing"),
                ],
            )
        return ProviderReport(
            name=self.name,
            state="configured_untested",
            detail="META_API_KEY is set; no smoke test has run in this process.",
            checks=[
                CheckResult(name="spark", state="configured_untested"),
                CheckResult(name="wonder", state="configured_untested"),
                CheckResult(name="sam", state="configured_untested"),
            ],
        )

    def complete(
        self,
        model: str,
        text: str,
        images: Optional[list[tuple[str, str]]] = None,
        max_output_tokens: int = 512,
    ) -> InferenceResult:
        """One bounded text completion (Spark/Wonder investigation calls).

        ``images`` is ``[(mime_type, base64_png)]``; prompts come from the
        caller and must already exclude credentials. Raises
        :class:`MetaInferenceError` on transport failure, rejection, or an
        incomplete result — never returns a fabricated completion.
        """
        if not self._api_key:
            raise MetaInferenceError(f"{model} is not configured (META_API_KEY missing)")
        content: list[dict[str, str]] = [{"type": "input_text", "text": text}]
        for mime, payload in images or []:
            content.append(
                {"type": "input_image", "image_url": f"data:{mime};base64,{payload}"}
            )
        with self._client() as client:
            outcome = bounded_request(
                client,
                "POST",
                f"{self._base_url}/responses",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json_body={
                    "model": model,
                    "input": [{"role": "user", "content": content}],
                    "max_output_tokens": max_output_tokens,
                    "store": False,
                },
                secrets=self._secrets(),
                timeout_s=config.INVESTIGATE_MODEL_TIMEOUT_S,
                max_retries=config.PROVIDER_MAX_RETRIES,
            )
        if not outcome.ok:
            raise MetaInferenceError(outcome.detail)
        if outcome.status != 200:
            raise MetaInferenceError(
                sanitize(
                    error_detail(outcome.status, outcome.body, f"{model} rejected the request"),
                    self._secrets(),
                )
            )
        status = outcome.body.get("status") if isinstance(outcome.body, dict) else None
        result_text = _output_text(outcome.body)
        if status != "completed" or not result_text.strip():
            raise MetaInferenceError(
                sanitize(
                    f"{model} returned status {status!r} with {len(result_text)} output chars",
                    self._secrets(),
                )
            )
        usage = outcome.body.get("usage") if isinstance(outcome.body, dict) else None
        return InferenceResult(
            model=model,
            text=result_text,
            status=str(status),
            latency_ms=outcome.latency_ms,
            attempts=outcome.attempts,
            usage=dict(usage) if isinstance(usage, dict) else None,
        )

    def segment(
        self, concept: str, image: bytes, mime: str, *, frame_w: int, frame_h: int
    ) -> tuple[list[SamMatch], InferenceResult]:
        """Segment ``concept`` in one still frame; geometry validated.

        Returns ``(matches, call_metadata)``. Zero matches is a normal
        completed result (empty list, never an invented mask); malformed
        geometry raises :class:`SamParseError`, call failure raises
        :class:`MetaInferenceError`.
        """
        if not self._api_key:
            raise MetaInferenceError(f"{SAM_MODEL} is not configured (META_API_KEY missing)")
        image_b64 = base64.b64encode(image).decode("ascii")
        with self._client() as client:
            outcome = bounded_request(
                client,
                "POST",
                f"{self._base_url}/responses",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json_body={
                    "model": SAM_MODEL,
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": concept},
                                {
                                    "type": "input_image",
                                    "image_url": f"data:{mime};base64,{image_b64}",
                                },
                            ],
                        }
                    ],
                    "store": False,
                },
                secrets=self._secrets(),
                timeout_s=config.INVESTIGATE_MODEL_TIMEOUT_S,
                max_retries=config.PROVIDER_MAX_RETRIES,
            )
        if not outcome.ok:
            raise MetaInferenceError(outcome.detail)
        if outcome.status != 200:
            raise MetaInferenceError(
                sanitize(
                    error_detail(outcome.status, outcome.body, f"{SAM_MODEL} rejected the request"),
                    self._secrets(),
                )
            )
        status = outcome.body.get("status") if isinstance(outcome.body, dict) else None
        if status != "completed":
            raise MetaInferenceError(
                sanitize(f"{SAM_MODEL} returned status {status!r}", self._secrets())
            )
        text = _output_text(outcome.body)
        matches = parse_sam_output(text, frame_w=frame_w, frame_h=frame_h)
        usage = outcome.body.get("usage") if isinstance(outcome.body, dict) else None
        return matches, InferenceResult(
            model=SAM_MODEL,
            text=text,
            status=str(status),
            latency_ms=outcome.latency_ms,
            attempts=outcome.attempts,
            usage=dict(usage) if isinstance(usage, dict) else None,
        )

    def smoke(self) -> ProviderReport:
        if not self._api_key:
            return self.status()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        with self._client() as client:
            checks = [
                self._smoke_text(client, headers, "spark", SPARK_MODEL),
                self._smoke_text(client, headers, "wonder", WONDER_MODEL),
                self._smoke_sam(client, headers),
            ]
        report = summarize(self.name, checks)
        report.checked_at = datetime.now(timezone.utc).isoformat()
        return report

    def _smoke_text(
        self, client: httpx.Client, headers: dict[str, str], check: str, model: str
    ) -> CheckResult:
        outcome = bounded_request(
            client,
            "POST",
            f"{self._base_url}/responses",
            headers=headers,
            json_body={
                "model": model,
                "input": _SMOKE_INPUT,
                "max_output_tokens": _SMOKE_MAX_OUTPUT_TOKENS,
                "reasoning": {"effort": "minimal"},
                "store": False,
            },
            secrets=self._secrets(),
            timeout_s=config.PROVIDER_TIMEOUT_S,
            max_retries=config.PROVIDER_MAX_RETRIES,
        )
        if not outcome.ok:
            return CheckResult(name=check, state="failed", detail=outcome.detail)
        if outcome.status != 200:
            return CheckResult(
                name=check,
                state="failed",
                detail=sanitize(
                    error_detail(outcome.status, outcome.body, f"{model} rejected the smoke request"),
                    self._secrets(),
                ),
                latency_ms=outcome.latency_ms,
            )
        text = _output_text(outcome.body)
        status = outcome.body.get("status") if isinstance(outcome.body, dict) else None
        if status == "completed" and text.strip():
            return CheckResult(
                name=check,
                state="verified",
                detail=f"{model} completed in {outcome.latency_ms}ms ({len(text)} output chars)",
                latency_ms=outcome.latency_ms,
            )
        return CheckResult(
            name=check,
            state="failed",
            detail=sanitize(
                f"{model} returned status {status!r} with {len(text)} output chars",
                self._secrets(),
            ),
            latency_ms=outcome.latency_ms,
        )

    def _smoke_sam(self, client: httpx.Client, headers: dict[str, str]) -> CheckResult:
        image_b64 = base64.b64encode(make_sam_test_image()).decode("ascii")
        outcome = bounded_request(
            client,
            "POST",
            f"{self._base_url}/responses",
            headers=headers,
            json_body={
                "model": SAM_MODEL,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": _SAM_CONCEPT},
                            {
                                "type": "input_image",
                                "image_url": f"data:image/png;base64,{image_b64}",
                            },
                        ],
                    }
                ],
                "store": False,
            },
            secrets=self._secrets(),
            timeout_s=config.PROVIDER_TIMEOUT_S,
            max_retries=config.PROVIDER_MAX_RETRIES,
        )
        if not outcome.ok:
            return CheckResult(name="sam", state="failed", detail=outcome.detail)
        if outcome.status != 200:
            return CheckResult(
                name="sam",
                state="failed",
                detail=sanitize(
                    error_detail(outcome.status, outcome.body, f"{SAM_MODEL} rejected the smoke request"),
                    self._secrets(),
                ),
                latency_ms=outcome.latency_ms,
            )
        status = outcome.body.get("status") if isinstance(outcome.body, dict) else None
        if status != "completed":
            return CheckResult(
                name="sam",
                state="failed",
                detail=sanitize(f"{SAM_MODEL} returned status {status!r}", self._secrets()),
                latency_ms=outcome.latency_ms,
            )
        text = _output_text(outcome.body)
        try:
            matches = parse_sam_output(text, frame_w=_SAM_TEST_WIDTH, frame_h=_SAM_TEST_HEIGHT)
        except SamParseError as exc:
            return CheckResult(
                name="sam",
                state="failed",
                detail=sanitize(f"{SAM_MODEL} geometry invalid: {exc}", self._secrets()),
                latency_ms=outcome.latency_ms,
            )
        if not matches:
            # Zero matches is a valid API response in general (the parser
            # accepts it), but this smoke sends a KNOWN red square: the
            # expected object must come back with geometry or the check fails.
            return CheckResult(
                name="sam",
                state="failed",
                detail=(
                    f"{SAM_MODEL} completed with 0 matches for the known red-square "
                    "fixture; expected-object check failed"
                ),
                latency_ms=outcome.latency_ms,
            )
        else:
            first = matches[0]
            detail = (
                f"{SAM_MODEL} completed: {len(matches)} match(es) within "
                f"{_SAM_TEST_WIDTH}x{_SAM_TEST_HEIGHT} "
                f"(id{first.object_id} box {first.x1},{first.y1}-{first.x2},{first.y2})"
            )
        return CheckResult(
            name="sam",
            state="verified",
            detail=f"{detail} in {outcome.latency_ms}ms",
            latency_ms=outcome.latency_ms,
        )
