"""Unified-diff parsing and validation against the pinned baseline.

The model's declared file list is never trusted: this module parses the
actual diff text and enforces every rule structurally — canonical paths,
manifest scope, protected exclusions, no renames/deletes/mode/binary
changes, size bounds, and strict in-memory applicability to the exact
pinned file bytes. Application happens only on fetched bytes in memory
(a disposable check); nothing is ever written to the working tree here.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from .. import config
from .models import ValidationCheck, ValidationResult

# Defense-in-depth backstop: rejected even if a manifest forgot them.
_BACKSTOP_EXACT = {
    ".buglens.json",
    "backend/app/guards.py",
}
_BACKSTOP_PREFIXES = (
    "backend/tests/",
    "fixtures/storefront/reproduce.mjs",  # exact, but prefix-safe either way
    "fixtures/storefront/server.js",
)

_DIFF_GIT_RE = re.compile(r"^diff --git a/(.*) b/(.*)$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NO_NEWLINE = "\\ No newline at end of file"


class DiffError(ValueError):
    """The diff text is malformed or inapplicable."""


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, str]] = field(default_factory=list)  # (kind, text)


@dataclass
class ParsedFile:
    path: str
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(1 for h in self.hunks for k, _ in h.lines if k == "+")

    @property
    def removed(self) -> int:
        return sum(1 for h in self.hunks for k, _ in h.lines if k == "-")


def _canonical_git_path(raw: str) -> str:
    if not raw or raw.startswith("/") or "\\" in raw:
        raise DiffError(f"refusing non-canonical path: {raw!r}")
    normalized = posixpath.normpath(raw)
    if normalized in (".", "..") or normalized.startswith("../") or "/../" in normalized:
        raise DiffError(f"refusing traversal path: {raw!r}")
    return normalized


def parse_diff(text: str) -> list[ParsedFile]:
    """Parse a git-style unified diff; raises DiffError when malformed."""
    lines = text.splitlines()
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines) or not lines[idx].startswith("diff --git "):
        raise DiffError("diff must start with a 'diff --git' header (no preamble)")
    files: list[ParsedFile] = []
    while idx < len(lines):
        if not lines[idx].strip():
            idx += 1
            continue
        header = _DIFF_GIT_RE.match(lines[idx])
        if not header:
            raise DiffError(f"expected 'diff --git' header, got: {lines[idx][:80]!r}")
        old_raw, new_raw = header.group(1), header.group(2)
        old_path = _canonical_git_path(old_raw)
        new_path = _canonical_git_path(new_raw)
        if old_path != new_path:
            raise DiffError(f"renames are not allowed: {old_raw!r} -> {new_raw!r}")
        idx += 1
        saw_minus = saw_plus = False
        # File headers up to the first hunk.
        while idx < len(lines) and not lines[idx].startswith("@@"):
            line = lines[idx]
            if line.startswith("diff --git "):
                break
            if line.startswith(("old mode", "new mode")):
                raise DiffError(f"mode changes are not allowed: {line!r}")
            if line.startswith("deleted file mode"):
                raise DiffError("file deletions are not allowed")
            if line.startswith("new file mode"):
                raise DiffError("new files are not allowed")
            if line.startswith(("rename from", "rename to", "similarity index", "dissimilarity")):
                raise DiffError("renames are not allowed")
            if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                raise DiffError("binary changes are not allowed")
            if line.startswith("--- "):
                minus = line[4:].strip().split("\t")[0]
                if minus == "/dev/null":
                    raise DiffError("file deletions are not allowed")
                if minus.startswith("a/"):
                    minus = minus[2:]
                if _canonical_git_path(minus) != old_path:
                    raise DiffError(f"'---' header does not match: {line[:80]!r}")
                saw_minus = True
            elif line.startswith("+++ "):
                plus = line[4:].strip().split("\t")[0]
                if plus == "/dev/null":
                    raise DiffError("file deletions are not allowed")
                if plus.startswith("b/"):
                    plus = plus[2:]
                if _canonical_git_path(plus) != new_path:
                    raise DiffError(f"'+++' header does not match: {line[:80]!r}")
                saw_plus = True
            elif line.startswith("index "):
                pass
            elif line.strip() == "":
                pass
            else:
                raise DiffError(f"unexpected diff header line: {line[:80]!r}")
            idx += 1
        if not (saw_minus and saw_plus):
            raise DiffError(f"missing ---/+++ headers for {old_path}")
        parsed = ParsedFile(path=old_path)
        # Hunks.
        while idx < len(lines) and lines[idx].startswith("@@"):
            match = _HUNK_RE.match(lines[idx])
            if not match:
                raise DiffError(f"malformed hunk header: {lines[idx][:80]!r}")
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3))
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            if old_start < 1 or new_start < 1 or old_count < 0 or new_count < 0:
                raise DiffError(f"invalid hunk range: {lines[idx][:80]!r}")
            hunk = Hunk(old_start, old_count, new_start, new_count)
            idx += 1
            while idx < len(lines):
                line = lines[idx]
                if line.startswith("diff --git ") or line.startswith("@@"):
                    break
                if line == _NO_NEWLINE:
                    idx += 1
                    continue
                if line.startswith(("+", "-", " ")):
                    hunk.lines.append((line[0], line[1:]))
                elif line == "":
                    hunk.lines.append((" ", ""))
                else:
                    raise DiffError(f"unexpected hunk line: {line[:80]!r}")
                idx += 1
            context = sum(1 for k, _ in hunk.lines if k == " ")
            removed = sum(1 for k, _ in hunk.lines if k == "-")
            added = sum(1 for k, _ in hunk.lines if k == "+")
            if removed + context != old_count or added + context != new_count:
                raise DiffError(
                    f"hunk @@ -{old_start},{old_count} +{new_start},{new_count} @@ "
                    "does not match its body"
                )
            parsed.hunks.append(hunk)
        if not parsed.hunks:
            raise DiffError(f"no hunks for {old_path}")
        files.append(parsed)
    if not files:
        raise DiffError("diff contains no files")
    if sum(f.added + f.removed for f in files) == 0:
        raise DiffError("diff changes no lines")
    return files


def apply_in_memory(source_lines: list[str], hunks: list[Hunk]) -> list[str]:
    """Strict-apply hunks to pinned lines; raises DiffError on any mismatch."""
    out: list[str] = []
    cursor = 0  # consumed source lines
    for hunk in hunks:
        start = hunk.old_start - 1
        if start < cursor or start > len(source_lines):
            raise DiffError(f"hunk at old line {hunk.old_start} is out of order or range")
        out.extend(source_lines[cursor:start])
        cursor = start
        for kind, text in hunk.lines:
            if kind == " ":
                if cursor >= len(source_lines) or source_lines[cursor] != text:
                    raise DiffError(f"context mismatch at old line {cursor + 1}")
                out.append(source_lines[cursor])
                cursor += 1
            elif kind == "-":
                if cursor >= len(source_lines) or source_lines[cursor] != text:
                    raise DiffError(f"removal mismatch at old line {cursor + 1}")
                cursor += 1
            else:
                out.append(text)
    out.extend(source_lines[cursor:])
    return out


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def validate_diff(
    text: str,
    *,
    permitted: list[str],
    protected: list[str],
    pinned: dict[str, list[str]],
    entry_types: dict[str, str],
) -> tuple[ValidationResult, list[str]]:
    """Validate a candidate diff; returns (result, changed_paths)."""
    checks: list[ValidationCheck] = []
    reasons: list[str] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append(ValidationCheck(name=name, ok=ok, detail=detail))
        if not ok:
            reasons.append(detail or name)

    raw_bytes = len(text.encode("utf-8"))
    if raw_bytes > config.PATCH_MAX_DIFF_BYTES:
        record("size", False, f"diff is {raw_bytes} bytes (limit {config.PATCH_MAX_DIFF_BYTES})")
        return ValidationResult(ok=False, checks=checks, reasons=reasons), []
    try:
        files = parse_diff(text)
    except DiffError as exc:
        record("parse", False, f"malformed diff: {exc}")
        return ValidationResult(ok=False, checks=checks, reasons=reasons), []
    record("parse", True, f"{len(files)} file(s), well-formed unified diff")

    if len(files) > config.PATCH_MAX_FILES:
        record("file-count", False, f"{len(files)} files (limit {config.PATCH_MAX_FILES})")
    else:
        record("file-count", True, f"{len(files)} file(s)")
    changed_lines = sum(f.added + f.removed for f in files)
    if changed_lines > config.PATCH_MAX_CHANGED_LINES:
        record("line-count", False,
               f"{changed_lines} changed lines (limit {config.PATCH_MAX_CHANGED_LINES})")
    else:
        record("line-count", True, f"{changed_lines} changed line(s)")

    scope_ok = True
    for parsed in files:
        if not any(_under(parsed.path, prefix) for prefix in permitted):
            record("scope", False, f"{parsed.path} is outside the permitted patch paths")
            scope_ok = False
        if any(_under(parsed.path, guard) or _under(guard, parsed.path) for guard in protected):
            record("protected", False, f"{parsed.path} touches a protected path")
            scope_ok = False
        if parsed.path in _BACKSTOP_EXACT or any(
            parsed.path == prefix.rstrip("/") or parsed.path.startswith(prefix)
            for prefix in _BACKSTOP_PREFIXES
        ):
            record("protected", False, f"{parsed.path} touches a harness/test/verifier path")
            scope_ok = False
        if entry_types.get(parsed.path) == "symlink":
            record("symlink", False, f"{parsed.path} is a symlink in the pinned tree")
            scope_ok = False
        if parsed.path not in pinned:
            record("baseline", False, f"{parsed.path} is not readable at the pinned baseline")
            scope_ok = False
    if scope_ok:
        record("scope", True, "all paths permitted, protected, and present")

    apply_ok = True
    for parsed in files:
        source = pinned.get(parsed.path)
        if source is None:
            continue
        try:
            apply_in_memory(source, parsed.hunks)
        except DiffError as exc:
            record("applies", False, f"{parsed.path}: {exc}")
            apply_ok = False
    if apply_ok and all(f.path in pinned for f in files):
        record("applies", True, "all hunks apply to the pinned baseline")
    ok = all(check.ok for check in checks)
    return ValidationResult(ok=ok, checks=checks, reasons=reasons), [f.path for f in files]
