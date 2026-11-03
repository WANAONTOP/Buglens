"""Map browser evidence to source at the incident's pinned commit.

Reads come from GitHub at the exact pinned SHA — never from the newer
local working tree. Mapping is deterministic text search over bounded
web-source files:

- ``exact``: the DOM identity resolves to one element definition site
  (matching tag + ``id``/``data-testid``/``data-buglens``) in a single
  file, plus at least one style/script reference.
- ``inferred``: the identity appears in source but the definition is
  ambiguous or only referenced, never defined.
- ``unknown``: the identity appears nowhere in the searched sources.

Every cited file must exist in the pinned tree and every cited line must
be inside the file; citations that fail validation are reported as
invalid, never silently kept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from ..repro.github_source import GitHubSourceError, fetch_blob, fetch_tree
from .models import MappingCertainty, SourceMapping

_SEARCH_EXTENSIONS = (".html", ".htm", ".css", ".js", ".jsx", ".ts", ".tsx", ".vue")
_MAX_FILES = 60
_MAX_FILE_BYTES = 100 * 1024
_MAX_TOTAL_BYTES = 1024 * 1024


@dataclass
class PinnedFile:
    path: str
    lines: list[str]


def _is_text_blob(data: bytes) -> bool:
    return b"\x00" not in data[:8192]


def load_pinned_sources(
    repo: str, sha: str, token: str, transport: "httpx.BaseTransport | None" = None
) -> tuple[list[PinnedFile], list[str]]:
    """Fetch bounded web sources at the pinned SHA; returns (files, tree_paths)."""
    tree = fetch_tree(repo, sha, token, transport=transport)
    tree_paths = [entry.path for entry in tree]
    candidates = [
        entry
        for entry in tree
        if entry.type == "blob"
        and entry.path.lower().endswith(_SEARCH_EXTENSIONS)
        and (entry.size is None or entry.size <= _MAX_FILE_BYTES)
    ]
    candidates.sort(key=lambda e: e.path)
    files: list[PinnedFile] = []
    total = 0
    for entry in candidates[:_MAX_FILES]:
        try:
            data = fetch_blob(repo, entry.sha, token, transport=transport)
        except GitHubSourceError:
            continue
        if len(data) > _MAX_FILE_BYTES or total + len(data) > _MAX_TOTAL_BYTES:
            continue
        if not _is_text_blob(data):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        total += len(data)
        files.append(PinnedFile(path=entry.path, lines=text.splitlines()))
    return files, tree_paths


def _element_sites(identifier: str, files: list[PinnedFile]) -> list[tuple[str, int]]:
    """Lines that define an element carrying the identity (tag + id/testid).

    Tags may span lines (one attribute per line), so the search runs over
    the whole file text; the reported line is where the opening tag starts.
    """
    sites: list[tuple[str, int]] = []
    attr = re.compile(
        r"<[a-zA-Z][a-zA-Z0-9]*\b[^>]*?("
        + r"id=\"" + re.escape(identifier) + r"\""
        + r"|data-testid=\"" + re.escape(identifier) + r"\""
        + r"|data-buglens=\"" + re.escape(identifier) + r"\""
        + r")[^>]*?>"
    )
    for source in files:
        text = "\n".join(source.lines)
        for match in attr.finditer(text):
            lineno = text.count("\n", 0, match.start()) + 1
            sites.append((source.path, lineno))
    return sites


def _element_spans(identifier: str, files: list[PinnedFile]) -> list[tuple[str, int, int]]:
    """(path, start line, end line) of each defining tag (multi-line aware)."""
    spans: list[tuple[str, int, int]] = []
    attr = re.compile(
        r"<[a-zA-Z][a-zA-Z0-9]*\b[^>]*?("
        + r"id=\"" + re.escape(identifier) + r"\""
        + r"|data-testid=\"" + re.escape(identifier) + r"\""
        + r"|data-buglens=\"" + re.escape(identifier) + r"\""
        + r")[^>]*?>"
    )
    for source in files:
        text = "\n".join(source.lines)
        for match in attr.finditer(text):
            start = text.count("\n", 0, match.start()) + 1
            end = text.count("\n", 0, match.end()) + 1
            spans.append((source.path, start, end))
    return spans


def _reference_sites(identifier: str, files: list[PinnedFile]) -> list[tuple[str, int]]:
    """Lines that reference the identity in style/script/text (not a definition)."""
    sites: list[tuple[str, int]] = []
    css_id = re.compile(r"#" + re.escape(identifier) + r"(?![A-Za-z0-9_-])")
    bare = re.compile(r"(?<![A-Za-z0-9_#/.-])" + re.escape(identifier) + r"(?![A-Za-z0-9_-])")
    spans = _element_spans(identifier, files)

    def inside_definition(path: str, lineno: int) -> bool:
        return any(p == path and start <= lineno <= end for p, start, end in spans)

    for source in files:
        for lineno, line in enumerate(source.lines, start=1):
            if inside_definition(source.path, lineno):
                continue
            if css_id.search(line) or bare.search(line):
                sites.append((source.path, lineno))
    return sites


def map_identity(
    target: str,
    identifier: str,
    files: list[PinnedFile],
    evidence_refs: list[str],
) -> SourceMapping:
    """Classify one DOM identity against the pinned sources."""
    elements = _element_sites(identifier, files)
    references = _reference_sites(identifier, files)
    if elements and len({path for path, _ in elements}) == 1 and references:
        path = elements[0][0]
        first = min(n for _, n in elements)
        last = max(n for _, n in elements)
        return SourceMapping(
            target=target,
            file=path,
            start_line=first,
            end_line=last,
            certainty="exact",
            evidence_refs=evidence_refs,
            note=(
                f"unique element definition in {path} "
                f"(lines {first}-{last}) plus {len(references)} style/script reference(s)"
            ),
        )
    if elements or references:
        path, lineno = (elements + references)[0]
        return SourceMapping(
            target=target,
            file=path,
            start_line=lineno,
            end_line=lineno,
            certainty="inferred",
            evidence_refs=evidence_refs,
            note="identity appears in source but has no unique element definition",
        )
    return SourceMapping(
        target=target,
        certainty="unknown",
        evidence_refs=evidence_refs,
        note="identity found nowhere in the searched pinned sources",
    )


def validate_citation(
    file: str | None,
    start_line: int | None,
    end_line: int | None,
    tree_paths: list[str],
    files: list[PinnedFile],
) -> tuple[bool, str]:
    """Check a Wonder citation against the pinned tree; returns (ok, reason)."""
    if not file:
        return False, "no file cited"
    if file not in tree_paths:
        return False, f"{file} is not in the pinned tree"
    if start_line is None or end_line is None:
        return False, "citation has no line range"
    for source in files:
        if source.path == file:
            total = len(source.lines)
            if 1 <= start_line <= end_line <= total:
                return True, "ok"
            return False, f"lines {start_line}-{end_line} outside {file} (1-{total})"
    return False, f"{file} was not searched (non-web or oversized file)"


def excerpt_for_wonder(
    mappings: list[SourceMapping], files: list[PinnedFile], max_chars: int
) -> tuple[str, bool]:
    """Bounded source excerpts around mapped lines; returns (text, truncated)."""
    by_path = {source.path: source for source in files}
    chunks: list[str] = []
    for mapping in mappings:
        if not mapping.file or mapping.start_line is None or mapping.end_line is None:
            continue
        source = by_path.get(mapping.file)
        if source is None:
            continue
        start = max(1, mapping.start_line - 6)
        end = min(len(source.lines), mapping.end_line + 6)
        numbered = "\n".join(
            f"{lineno:4d}: {source.lines[lineno - 1]}" for lineno in range(start, end + 1)
        )
        chunks.append(f"--- {mapping.file} (lines {start}-{end}) ---\n{numbered}")
    text = "\n\n".join(chunks)
    if len(text) > max_chars:
        return text[:max_chars] + "\n…[truncated to the source budget]", True
    return text, False
