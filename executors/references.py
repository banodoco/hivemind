"""The deliberately small, closed Hivemind reference grammar.

Supported tokens are ``[resource:ID]``, ``[resource:ID@REV]``,
``[message:ID]``, ``[message:ID@SNAPSHOT]``, ``[revision:ID]`` and
``[evidence:ID]``.  A Markdown link destination may use the equivalent
``hivemind:TYPE:ID[@VERSION]`` form.  This is a scanner, not a Markdown
parser: fenced/inline code and backslash-escaped literals are ignored.
"""

from __future__ import annotations

import re
from typing import Any

_KINDS = {"resource", "message", "revision", "evidence"}
_TOKEN = re.compile(
    r"\[(?P<kind>resource|message|revision|evidence):"
    r"(?P<id>[1-9][0-9]*)(?:@(?P<version>[1-9][0-9]*))?\]"
)
_DESTINATION = re.compile(
    r"\[[^\]\n]*\]\(hivemind:(?P<kind>resource|message|revision|evidence):"
    r"(?P<id>[1-9][0-9]*)(?:@(?P<version>[1-9][0-9]*))?\)"
)


def _masked_markdown(text: str) -> str:
    """Return a same-length mask for code spans/fences and escaped chars."""
    chars = list(text)
    in_fence = False
    in_inline = False
    fence_char = ""
    fence_len = 0
    inline_len = 0
    i = 0
    while i < len(text):
        if text[i] in "`~":
            delimiter = text[i]
            j = i + 1
            while j < len(text) and text[j] == delimiter:
                j += 1
            run_len = j - i
            if in_fence:
                if delimiter == fence_char and run_len >= fence_len:
                    in_fence = False
                    fence_char = ""
                    fence_len = 0
                chars[i:j] = " " * run_len
                i = j
                continue
            if run_len >= 3:
                in_fence = True
                fence_char = delimiter
                fence_len = run_len
                chars[i:j] = " " * run_len
                i = j
                continue
            if delimiter == "`":
                if not in_inline:
                    in_inline = True
                    inline_len = run_len
                elif run_len == inline_len:
                    in_inline = False
                    inline_len = 0
                chars[i:j] = " " * run_len
                i = j
                continue
        if in_fence:
            chars[i] = " "
            i += 1
            continue
        if in_inline:
            chars[i] = " "
            i += 1
            continue
        if text[i] == "\\":
            chars[i] = " "
            if i + 1 < len(text):
                chars[i + 1] = " "
                i += 2
            else:
                i += 1
            continue
        i += 1
    return "".join(chars)


def _reference(match: re.Match[str], *, labelled: bool) -> dict[str, str | None]:
    kind = match.group("kind")
    version = match.group("version")
    if kind in {"revision", "evidence"} and version is not None:
        raise ValueError(f"{kind} references cannot carry a version")
    result: dict[str, str | None] = {
        "target_kind": kind,
        "target_id": match.group("id"),
        "target_version_id": version,
        "labelled": "true" if labelled else "false",
    }
    return result


def parse_references(text: str) -> list[dict[str, str | None]]:
    """Parse supported references in source order.

    Unknown kinds, malformed IDs, ordinary HTTP links, code literals, and
    escaped literals are not references.  A version on ``revision`` or
    ``evidence`` is a malformed supported token and raises ``ValueError`` so
    callers do not silently publish a different target than the author wrote.
    """
    if not isinstance(text, str):
        raise TypeError("reference text must be a string")
    masked = _masked_markdown(text)
    found: list[tuple[int, dict[str, str | None]]] = []
    labelled_spans: list[tuple[int, int]] = []
    for match in _DESTINATION.finditer(masked):
        found.append((match.start(), _reference(match, labelled=True)))
        labelled_spans.append(match.span())
    for match in _TOKEN.finditer(masked):
        if any(start <= match.start() < end for start, end in labelled_spans):
            continue
        found.append((match.start(), _reference(match, labelled=False)))
    found.sort(key=lambda pair: pair[0])
    # A labelled destination also contains no standalone token in the masked
    # text, but retain stable de-duplication for repeated scanner matches.
    result: list[dict[str, str | None]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for _, item in found:
        key = (str(item["target_kind"]), str(item["target_id"]), item["target_version_id"])
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def reference_to_rpc(item: dict[str, Any]) -> dict[str, str]:
    """Normalize a parsed reference for the trusted SQL RPC envelope."""
    kind = item.get("target_kind")
    target_id = item.get("target_id")
    version = item.get("target_version_id")
    if kind not in _KINDS or not isinstance(target_id, str) or not target_id.isdigit():
        raise ValueError("invalid reference")
    result = {"target_kind": kind, "target_id": target_id}
    if version is not None:
        result["target_version_id"] = str(version)
    if item.get("label") is not None:
        result["label"] = str(item["label"])
    return result
