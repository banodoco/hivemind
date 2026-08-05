"""Frozen workflow representation contract for Hivemind hybrid search (plan task 0.8).

This module is the **authoritative Phase-0 reference contract** for how a
``kind=workflow`` resource's Python and prose are represented for lexical
indexing, embedding, hashing, chunking, secret scanning, and retrieval.

It is intentionally **pure, offline, stdlib-only, and dependency-free**. It does
not import VibeComfy, does not touch the database, does not call an embedding
provider, does not execute stored Python, and does not read the live corpus. The
deployed SQL/Edge production path (plan tasks 1.2 / 2.5 / 2.6) must match the
behaviour pinned here; this module exists so that behaviour is frozen, reviewed,
and unit-tested *before* Phase 1 implementation.

Everything in this file is a frozen decision recorded in
``docs/hybrid-search/phase0-workflow-representation-contract.{md,json}`` dated
2026-07-28. Bumping any ``*_VERSION`` constant below is a contract change that
must trigger a controlled re-canonicalize / re-embed of affected representations
(plan AD-4); it is never a silent edit.

References (read-only, not imported at runtime):
- VibeComfy body delimiters:
  ``upload_ready_templates_to_hivemind.py`` ("Python ready-template source:")
  ``upload_external_workflows_to_hivemind.py`` ("Python scratchpad source:")
- Hivemind structured projection: ``executors/workflow_semantics.py`` (v1).
"""

from __future__ import annotations

import ast
import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# Frozen contract versions
# ---------------------------------------------------------------------------

#: Bumped when the delimiter grammar, precedence, semantics projection, or the
#: dedup/no-duplication rule changes. Part of every embedding contract identity
#: and the representation freshness check.
CANONICALIZATION_VERSION = 1

#: Bumped when the body-delimiter set or its parsing grammar changes.
DELIMITER_GRAMMAR_VERSION = 1

#: Bumped when the deterministic secret scanner patterns/reason codes change.
SECRET_SCAN_VERSION = 1

#: Bumped when the prose/code chunker alignment, budget, or overlap rule changes.
#:
#: v2 (task-2.14/2.16 chunking-version propagation): the oversized workflow-Python
#: fallback is now BOUNDED with complete coverage. The AST oversized-literal
#: branch already windows via :func:`_fixed_window`; the parser-fallback path
#: (:func:`_line_window`, taken on ``ast.parse`` failure) now windows any single
#: line that alone exceeds the budget instead of emitting it whole, so no chunk
#: from any oversized path is unbounded while coverage stays complete. The fix is
#: a no-op for normal-length lines (every real-cohort statement parses and is
#: chunked byte-identically), which is what permits a zero-provider-call v2
#: equivalence evaluation. Because ``chunking_version`` is itself an identity
#: axis, this BEHAVIOR bump does move the identities that carry it (the selected
#: full-literal bigint and the eval contract id change from their chunking-v1
#: values). The chunk-config *identity* scheme, however, stays at v1 — the
#: bounded-fallback fix added no config identity axis (no target/overlap/mode or
#: prose/python config axis changed), so the chunk-config identity version does
#: NOT bump. The two version axes are deliberately distinct: chunker behavior v2,
#: chunk-config identity scheme v1.
CHUNKING_VERSION = 2

#: Mirrors ``executors.workflow_semantics.WORKFLOW_SEMANTICS_VERSION``.
SEMANTICS_PROJECTION_VERSION = 1

#: Rough tokens-per-char estimate (matches the task-0.3 inventory heuristic).
#: Real tokenization happens in plan task 2.6; this is the frozen planner estimate.
CHARS_PER_TOKEN = 4

# ---------------------------------------------------------------------------
# Body delimiters (exact VibeComfy literals)
# ---------------------------------------------------------------------------

#: Recognized body delimiters, verbatim from the VibeComfy upload path. Both are
#: a trailing-colon label on its own line, with **no** markdown fence, followed
#: by the Python source. Order matters only for stable display; matching is
#: exact-line (see :func:`extract_python_blocks`).
BODY_DELIMITERS: tuple[str, ...] = (
    "Python ready-template source:",
    "Python scratchpad source:",
)

#: The marker that terminates a Python block when the body also carries a
#: rule-based semantics section (appended by ``executors/workflow_semantics.py``).
SEMANTICS_BLOCK_MARKER = "Workflow semantics (rule-based):"

# Some enrichment paths write this marker as a replaceable suffix; remediation
# also recognizes it while preserving the established unmarked output shape.
CANONICAL_SEMANTICS_MARKER = "Workflow semantics (canonical):"

#: Emitter headers VibeComfy writes at the top of the generated source. They are
#: provenance signals, not delimiters. Order is most-specific-first so
#: ``startswith`` cannot match the bare ``# vibecomfy: generated`` header before
#: the ``... scratchpad`` / ``... external workflow archive`` variants.
EMITTER_HEADERS: dict[str, str] = {
    "# vibecomfy: generated scratchpad": "scratchpad",
    "# vibecomfy: generated external workflow archive": "external_archive",
    "# vibecomfy: generated": "ready_template",
}

# ---------------------------------------------------------------------------
# Cohort vocabulary (plan AD-4 + task 0.8)
# ---------------------------------------------------------------------------

COHORT_PAYLOAD_PYTHON = "payload_python"
COHORT_BODY_PYTHON = "body_python"
COHORT_RECOVERABLE = "recoverable"
COHORT_UNAVAILABLE = "unavailable"

#: Every eligible workflow resolves to exactly one authoritative source cohort.
SOURCE_COHORTS: tuple[str, ...] = (
    COHORT_PAYLOAD_PYTHON,
    COHORT_BODY_PYTHON,
    COHORT_RECOVERABLE,
    COHORT_UNAVAILABLE,
)

#: Cross-cutting public-search state, independent of source cohort.
PUBLIC_STATE_SAFE = "safe"
PUBLIC_STATE_QUARANTINED = "quarantined"
PUBLIC_STATES: tuple[str, ...] = (PUBLIC_STATE_SAFE, PUBLIC_STATE_QUARANTINED)

#: Embedding representation types (plan AD-2). Part of embedding identity.
REP_PROSE = "prose"
REP_WORKFLOW_PYTHON = "workflow_python"
REPRESENTATION_TYPES: tuple[str, ...] = (REP_PROSE, REP_WORKFLOW_PYTHON)


# ---------------------------------------------------------------------------
# Text normalization + hashing (frozen, deterministic)
# ---------------------------------------------------------------------------

def normalize_for_hash(text: str) -> str:
    """Return the frozen canonical form used for every representation hash.

    - Unicode NFC.
    - CRLF / CR newlines collapsed to ``\\n``.
    - Trailing per-line whitespace stripped.
    - Exactly one trailing newline, then stripped to a stable form.

    Identical inputs always produce identical hashes; the rule is frozen so a
    hash never changes unless ``CANONICALIZATION_VERSION`` bumps.
    """
    if text is None:
        return ""
    text = unicodedata.normalize("NFC", str(text))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def representation_hash(text: str) -> str:
    """SHA-256 hex of the frozen-normalized text (the representation-level hash)."""
    return hashlib.sha256(normalize_for_hash(text).encode("utf-8")).hexdigest()


def chunk_hash(text: str) -> str:
    """SHA-256 hex of a single embedded chunk.

    Same algorithm as :func:`representation_hash`; the distinction is structural
    (per-chunk vs per-representation), kept as a separate name for clarity and
    so the two hash columns cannot be confused."""
    return representation_hash(text)


def estimate_tokens(text: str) -> int:
    """Frozen chars/4 token estimate (planning only; real tokenizer in task 2.6)."""
    return max(0, len(normalize_for_hash(text)) // CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Delimiter parsing + Python precedence (plan AD-4)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PythonBlock:
    delimiter: str
    text: str
    start_line: int  # 1-based index of the delimiter line in the source body
    end_line: int    # 1-based index of the last line belonging to this block


def extract_python_blocks(body: str | None) -> list[PythonBlock]:
    """Extract every recognized delimited Python block from a workflow ``body``.

    A block starts at a line whose :py:meth:`str.rstrip` value equals a
    :data:`BODY_DELIMITERS` literal and runs until the next delimiter line, the
    :data:`SEMANTICS_BLOCK_MARKER`, or end of body. The delimiter line itself is
    not part of the returned Python text. Delimiters are matched exactly — a
    near-miss (``Python source:``, ``python ready-template source:``) is ignored,
    which is why an unrecognized block never silently masquerades as Python.
    """
    if not body:
        return []
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[PythonBlock] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].rstrip() in BODY_DELIMITERS:
            delimiter = lines[i].rstrip()
            start = i + 1  # 1-based delimiter line
            j = i + 1
            while j < n:
                candidate = lines[j].rstrip()
                if candidate in BODY_DELIMITERS:
                    break
                if candidate.startswith(SEMANTICS_BLOCK_MARKER):
                    break
                j += 1
            text = "\n".join(lines[i + 1:j]).strip()
            blocks.append(
                PythonBlock(
                    delimiter=delimiter,
                    text=text,
                    start_line=start,
                    end_line=j,  # exclusive end == last included line index (1-based)
                )
            )
            i = j
        else:
            i += 1
    return blocks


def strip_python_blocks(body: str | None) -> str:
    """Return ``body`` with every recognized delimited Python block removed.

    This is the no-duplication step: when the same Python lives in both
    ``payload.python_source`` and a body block, the block is removed from the
    canonical *prose* representation so the code is hashed/embedded/indexed
    exactly once (from the authoritative Python representation).
    """
    if not body:
        return ""
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].rstrip() in BODY_DELIMITERS:
            delimiter = lines[i].rstrip()
            j = i + 1
            while j < n:
                candidate = lines[j].rstrip()
                if candidate in BODY_DELIMITERS:
                    break
                if candidate.startswith(SEMANTICS_BLOCK_MARKER):
                    break
                j += 1
            # Drop the delimiter line and its block. Keep subsequent text.
            i = j
            continue
        kept.append(lines[i])
        i += 1
    # Collapse the blank lines the removed blocks leave behind, but preserve
    # paragraph structure for the prose chunker.
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


#: Optional recovery callable signature: ``recoverer(row) -> str | None``.
#: In production (plan task 2.12) this wraps VibeComfy's exporters
#: (``emit_ready_template_python`` / ``emit_scratchpad_python`` /
#: ``_fallback_python_archive_source``). Here it is injected so the reference
#: stays offline and testable without importing VibeComfy or executing code.
Recoverer = Callable[[dict[str, Any]], "str | None"]


@dataclass(frozen=True)
class PythonResolution:
    """The authoritative Python representation for one workflow row."""

    cohort: str                      # one of SOURCE_COHORTS
    source: str                      # authoritative Python bytes ("" when unavailable)
    available: bool                  # False only for the unavailable cohort
    body_duplicate: bool             # True when payload bytes also appear in body
    delimiter: str | None            # which delimiter yielded body/recovered source
    derivation: str                  # payload | body_delimiter | ready_template | scratchpad | external_archive | none
    body_blocks: tuple[PythonBlock, ...] = field(default_factory=tuple)
    unavailable_reason: str | None = None


def _payload_python_source(row: dict[str, Any]) -> str:
    payload = row.get("payload") or {}
    if isinstance(payload, dict):
        value = payload.get("python_source")
    else:
        value = None
    return value if isinstance(value, str) else ""


def resolve_authoritative_python(
    row: dict[str, Any],
    *,
    recoverer: Recoverer | None = None,
) -> PythonResolution:
    """Resolve the authoritative Python for a workflow row under frozen precedence.

    Precedence (plan AD-4, frozen):

    1. Non-empty ``payload.python_source`` is authoritative (cohort
       ``payload_python``). If the same bytes also form a body block, the row is
       flagged ``body_duplicate`` (the "both" case) for no-duplication handling.
    2. Otherwise, a recognized body-delimited block is authoritative (cohort
       ``body_python``).
    3. Otherwise, deterministic VibeComfy recovery (the injected ``recoverer``)
       yields the source (cohort ``recoverable``). No LLM, no execution.
    4. Otherwise the cohort is ``unavailable`` with an explicit reason; the row
       remains prose-searchable but has no Python representation.
    """
    body = row.get("body") if isinstance(row.get("body"), str) else ""
    blocks = tuple(extract_python_blocks(body))

    payload_ps = _payload_python_source(row)
    if payload_ps.strip():
        payload_norm = normalize_for_hash(payload_ps)
        body_dup = any(normalize_for_hash(b.text) == payload_norm for b in blocks)
        if not body_dup:
            # Also accept verbatim substring presence (delimiter + same bytes).
            body_dup = payload_ps.strip() in (body or "")
        return PythonResolution(
            cohort=COHORT_PAYLOAD_PYTHON,
            source=payload_ps,
            available=True,
            body_duplicate=body_dup,
            delimiter=next((b.delimiter for b in blocks if normalize_for_hash(b.text) == payload_norm), None),
            derivation="payload",
            body_blocks=blocks,
        )

    if blocks:
        first = blocks[0]
        return PythonResolution(
            cohort=COHORT_BODY_PYTHON,
            source=first.text,
            available=True,
            body_duplicate=False,
            delimiter=first.delimiter,
            derivation="body_delimiter",
            body_blocks=blocks,
        )

    if recoverer is not None:
        try:
            recovered = recoverer(row)
        except Exception as exc:  # never let recovery abort classification
            recovered = None
            reason = f"conversion_error:{type(exc).__name__}"
        else:
            reason = None
        if isinstance(recovered, str) and recovered.strip():
            derivation = _infer_recovery_derivation(recovered)
            return PythonResolution(
                cohort=COHORT_RECOVERABLE,
                source=recovered,
                available=True,
                body_duplicate=False,
                delimiter=None,
                derivation=derivation,
                body_blocks=(),
            )
        return PythonResolution(
            cohort=COHORT_UNAVAILABLE,
            source="",
            available=False,
            body_duplicate=False,
            delimiter=None,
            derivation="none",
            body_blocks=(),
            unavailable_reason=reason or "no_source_artifact",
        )

    # No recoverer supplied: the recoverable/unavailable split is unresolved here
    # and is finalized by the operator remediation job (plan task 2.12).
    return PythonResolution(
        cohort=COHORT_UNAVAILABLE,
        source="",
        available=False,
        body_duplicate=False,
        delimiter=None,
        derivation="none",
        body_blocks=(),
        unavailable_reason="recovery_not_attempted",
    )


def _infer_recovery_derivation(source: str) -> str:
    head = source.lstrip()[:64]
    for header, derivation in EMITTER_HEADERS.items():
        if head.startswith(header):
            return derivation
    return "scratchpad"


# ---------------------------------------------------------------------------
# Canonical prose representation (plan AD-4 prose row)
# ---------------------------------------------------------------------------

#: Stable ``workflow_semantics`` fields projected into the canonical prose text.
#: Mirrors ``executors/workflow_semantics.py`` output (v1).
SEMANTICS_PROJECTION_FIELDS: tuple[str, ...] = (
    "media_type",
    "task_type",
    "model_families",
    "adapter_directions",
    "node_types",
    "node_class_multiset",
    "custom_nodes",
    "models",
    "searchable_aliases",
)


def project_semantics(metadata: dict[str, Any] | None) -> str:
    """Render the stable ``workflow_semantics`` fields as canonical prose text.

    Output is deterministic and ordered; absent fields are omitted. This is the
    structured projection that makes a workflow's facts searchable without
    embedding arbitrary ``payload``/``workflow_json`` wholesale.
    """
    if not isinstance(metadata, dict):
        return ""
    semantics = metadata.get("workflow_semantics")
    if not isinstance(semantics, dict):
        return ""
    parts: list[str] = []
    for key in SEMANTICS_PROJECTION_FIELDS:
        value = semantics.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (list, tuple, dict)):
            rendered = " ".join(_flatten(value))
        else:
            rendered = str(value)
        rendered = rendered.strip()
        if rendered:
            parts.append(rendered)
    return "\n".join(parts)


def strip_generated_semantics(body: str | None) -> str:
    """Remove a generated semantics suffix from a workflow body."""
    if not body:
        return ""
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, line in enumerate(lines):
        if line.startswith((CANONICAL_SEMANTICS_MARKER, SEMANTICS_BLOCK_MARKER)):
            return "\n".join(lines[:index]).rstrip()
    return body.strip()


def strip_projected_semantics(body: str | None, projection: str) -> str:
    """Remove a prior unmarked canonical semantics suffix, if present."""
    cleaned = strip_generated_semantics(body)
    projection = projection.strip()
    if not projection:
        return cleaned
    for separator in ("\n\n", "\n"):
        suffix = separator + projection
        if cleaned.endswith(suffix):
            return cleaned[: -len(suffix)].rstrip()
    return cleaned


def strip_canonical_workflow_prefix(body: str | None, title: str) -> str:
    """Remove the title prefix emitted by :func:`build_workflow_prose`."""
    cleaned = strip_generated_semantics(body)
    title = (title or "").strip()
    if title and (cleaned == title or cleaned.startswith(title + "\n\n")):
        return cleaned[len(title):].lstrip()
    return cleaned


def _flatten(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _flatten(v)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _flatten(item)
    elif value in (None, ""):
        return
    else:
        yield str(value)


def build_workflow_prose(row: dict[str, Any]) -> str:
    """Build the canonical workflow *prose* representation (plan AD-4 prose row).

    Title, then body with delimited Python blocks removed (no-duplication), then
    the projected ``workflow_semantics``. The full canonical Python lives in its
    own ``workflow_python`` representation; it never appears in the prose hash.
    """
    title = (row.get("title") or "").strip()
    body = row.get("body") if isinstance(row.get("body"), str) else ""
    semantics = project_semantics(row.get("metadata"))
    prose_body = strip_projected_semantics(strip_python_blocks(body), semantics)
    prose_body = strip_canonical_workflow_prefix(prose_body, title)
    chunks = [c for c in (title, prose_body, semantics) if c]
    return "\n\n".join(chunks)


def build_workflow_python(row: dict[str, Any], *, recoverer: Recoverer | None = None) -> str:
    """Return the authoritative canonical Python bytes ("" if unavailable)."""
    return resolve_authoritative_python(row, recoverer=recoverer).source


# ---------------------------------------------------------------------------
# Deterministic secret scanner (plan AD-4 security / task-0.8 freeze)
# ---------------------------------------------------------------------------

#: The frozen reason-code vocabulary. Note what is deliberately ABSENT:
#: ``high_entropy_base64``. The task-0.3 inventory flagged all 222 Python-bearing
#: workflows with ``high_entropy_base64`` because VibeComfy-generated source
#: legitimately embeds large base64/JSON literals. A bare high-entropy run with
#: no credential context is NOT a quarantine reason here — this is the trap fix.
SECRET_REASON_CODES: tuple[str, ...] = (
    "private_key_block",
    "provider_token_prefix",
    "credential_assignment",
    "credential_url",
    "aws_env_var",
)

# Tier 1: structured private-key blocks (PEM/SSH/PGP headers).
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED |)PRIVATE KEY-----",
)

# Tier 2: known provider/token prefixes with a plausible length + charset.
_PROVIDER_TOKEN_RES: dict[str, re.Pattern[str]] = {
    "openai": re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{30,}"),
    "openai_legacy": re.compile(r"sk-[A-Za-z0-9]{40,}"),
    "github": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "slack": re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    "aws_access": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "google_api": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "stripe": re.compile(r"\b[rs]k_(?:live|test)_[0-9A-Za-z]{24,}\b"),
    "supabase_secret": re.compile(r"sb_secret_[A-Za-z0-9]{16,}"),
    "hivemind_contributor": re.compile(r"\bhm_[0-9a-f]{64}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
}

# Tier 3: credential assignment — credential-keyword LHS + high-entropy RHS.
_CREDENTIAL_KEYWORDS = (
    "api[_-]?key", "apikey", "secret", "password", "passwd", "token",
    "access[_-]?token", "refresh[_-]?token", "private[_-]?key",
    "client[_-]?secret", "authorization",
)
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?P<key>" + "|".join(_CREDENTIAL_KEYWORDS) + r")"
    r"\s*[:=]\s*['\"](?P<value>[^'\"\s]{16,})['\"]"
)

# Tier 4: credential-bearing URL (userinfo with a password).
_CREDENTIAL_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/@:]+:[^\s/@:]+@[^\s/@]+")

# Tier 5: AWS environment-variable shape.
_AWS_ENV_RE = re.compile(r"AWS_(?:ACCESS_KEY_ID|SECRET_ACCESS_KEY)\s*=\s*['\"]?[A-Za-z0-9/+=]{16,}")

#: Placeholder passwords that are not secrets (avoid noise on examples/docs).
_URL_PASSWORD_DENYLIST = {"password", "passwd", "secret", "changeme", "your-password", "x", "***", "redacted"}


def _shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


@dataclass(frozen=True)
class SecretFinding:
    reason_code: str
    # Offset/length of the match in the scanned text; the matched VALUE is never
    # stored, logged, serialized, or shown — only its position and reason.
    start: int
    length: int
    detail_kind: str  # e.g. provider name / 'pem' / 'assignment' — never the secret


def scan_secrets(python: str) -> list[SecretFinding]:
    """Deterministic, fail-closed secret scan of workflow Python.

    Returns one :class:`SecretFinding` per high-confidence credential match.
    A non-empty result quarantines the representation from public lexical /
    embedding / snippet / full-code retrieval (plan AD-4, task 0.8).

    Design (the task-0.3 trap fix): a bare high-entropy / base64 run with **no
    credential context** is never a reason. Matches require structure — a key
    block, a known provider prefix of credential length, a credential-keyword
    assignment with a high-entropy RHS, a ``user:pass@`` URL, or an AWS env var.
    This is why the 222 VibeComfy-generated bodies (large base64 literals, no
    credential keywords) scan clean while real secrets fail closed.
    """
    findings: list[SecretFinding] = []

    for m in _PRIVATE_KEY_RE.finditer(python):
        findings.append(SecretFinding("private_key_block", m.start(), len(m.group(0)), "pem_header"))

    for provider, pattern in _PROVIDER_TOKEN_RES.items():
        for m in pattern.finditer(python):
            findings.append(SecretFinding("provider_token_prefix", m.start(), len(m.group(0)), provider))

    for m in _CREDENTIAL_ASSIGNMENT_RE.finditer(python):
        value = m.group("value")
        # Require the RHS to actually look like a secret: long enough and either
        # high-entropy or a non-dictionary token charset.
        looks_secret = len(value) >= 16 and (
            _shannon_entropy(value) >= 3.0 or re.fullmatch(r"[A-Za-z0-9/_+\-=]{20,}", value)
        )
        if looks_secret:
            findings.append(SecretFinding("credential_assignment", m.start(), len(m.group(0)), "assignment"))

    for m in _CREDENTIAL_URL_RE.finditer(python):
        password = m.group(0).split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
        if password.lower() not in _URL_PASSWORD_DENYLIST and len(password) >= 6:
            findings.append(SecretFinding("credential_url", m.start(), len(m.group(0)), "url_userinfo"))

    for m in _AWS_ENV_RE.finditer(python):
        findings.append(SecretFinding("aws_env_var", m.start(), len(m.group(0)), "aws_env"))

    # Stable, deterministic order by position then reason.
    findings.sort(key=lambda f: (f.start, f.reason_code))
    return findings


def has_secret(python: str) -> bool:
    return bool(scan_secrets(python))


def public_state(python: str) -> str:
    """Return the frozen public-search state for a Python representation."""
    return PUBLIC_STATE_QUARANTINED if has_secret(python) else PUBLIC_STATE_SAFE


def redact_snippet(snippet: str, *, window: int = 240) -> str:
    """Defense-in-depth snippet redaction.

    Bound the snippet to ``window`` characters and mask any span that the scanner
    would flag, so a matched Python snippet can never leak a secret even if a
    higher layer mishandles it. The matched value is replaced with a placeholder.
    """
    if not snippet:
        return ""
    bounded = snippet if len(snippet) <= window else snippet[:window]
    findings = scan_secrets(bounded)
    # Apply right-to-left so offsets stay valid.
    masked = bounded
    for f in sorted(findings, key=lambda x: x.start, reverse=True):
        masked = masked[: f.start] + "[REDACTED:" + f.reason_code + "]" + masked[f.start + f.length:]
    return masked


# ---------------------------------------------------------------------------
# Deterministic chunking (plan AD-5 / task 0.8)
# ---------------------------------------------------------------------------

#: Frozen pilot chunk configurations (at least two per representation type).
#: ``target_tokens`` and ``overlap_tokens`` use the chars/4 estimate. The
#: production choice is plan task 2.14; these are the pilot candidates.
CHUNK_CONFIGS: dict[str, dict[str, Any]] = {
    "prose_512": {"mode": "prose", "target_tokens": 512, "overlap_tokens": 50},
    "prose_1024": {"mode": "prose", "target_tokens": 1024, "overlap_tokens": 100},
    "python_512": {"mode": "python", "target_tokens": 512, "overlap_tokens": 50},
    "python_2048": {"mode": "python", "target_tokens": 2048, "overlap_tokens": 100},
}


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    hash: str
    start_line: int
    end_line: int
    method: str          # 'paragraph' | 'sentence' | 'fixed_window' | 'ast' | 'ast_fallback'
    token_estimate: int


def _tokens(text: str) -> int:
    return estimate_tokens(text)


def _pack_paragraphs(paras: list[str], target_tokens: int) -> list[list[str]]:
    """Greedy-pack paragraphs up to ``target_tokens``; never split a paragraph."""
    packs: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for para in paras:
        pt = _tokens(para)
        if current and current_tokens + pt > target_tokens:
            packs.append(current)
            current = []
            current_tokens = 0
        current.append(para)
        current_tokens += pt
    if current:
        packs.append(current)
    return packs


def _split_long_paragraph(para: str, target_tokens: int) -> list[str]:
    """Fallback for a single paragraph larger than the budget: sentence, then window."""
    sentences = re.split(r"(?<=[.!?])\s+", para)
    if len(sentences) > 1:
        out: list[str] = []
        buf = ""
        for s in sentences:
            if _tokens(buf + " " + s) > target_tokens and buf:
                out.append(buf.strip())
                buf = s
            else:
                buf = (buf + " " + s).strip()
        if buf:
            out.append(buf.strip())
        # Any piece still over budget → fixed window.
        final: list[str] = []
        for piece in out:
            if _tokens(piece) > target_tokens * 1.5:
                final.extend(_fixed_window(piece, target_tokens))
            else:
                final.append(piece)
        return final
    return _fixed_window(para, target_tokens)


def _fixed_window(text: str, target_tokens: int) -> list[str]:
    target_chars = max(1, target_tokens * CHARS_PER_TOKEN)
    if len(text) <= target_chars:
        return [text] if text.strip() else []
    return [text[i:i + target_chars] for i in range(0, len(text), target_chars)]


def _apply_overlap(packs: list[str], overlap_tokens: int) -> list[str]:
    if overlap_tokens <= 0 or len(packs) <= 1:
        return packs
    overlap_chars = overlap_tokens * CHARS_PER_TOKEN
    out = [packs[0]]
    for prev, cur in zip(packs, packs[1:]):
        tail = prev[-overlap_chars:] if overlap_chars < len(prev) else prev
        out.append((tail + cur).strip() if tail else cur)
    return out


def chunk_prose(text: str, *, target_tokens: int = 512, overlap_tokens: int = 50) -> list[Chunk]:
    """Paragraph-aware prose chunker. Deterministic; repeated runs are identical."""
    norm = normalize_for_hash(text)
    if not norm:
        return []
    paras = [p.strip() for p in re.split(r"\n{2,}", norm) if p.strip()]
    # Normalize any paragraph that exceeds budget via sentence/window fallback.
    normed_paras: list[str] = []
    method = "paragraph"
    for p in paras:
        if _tokens(p) > target_tokens * 1.5:
            method = "sentence"
            normed_paras.extend(_split_long_paragraph(p, target_tokens))
        else:
            normed_paras.append(p)
    packs = _pack_paragraphs(normed_paras, target_tokens)
    pack_texts = ["\n\n".join(pack) for pack in packs if pack]
    pack_texts = _apply_overlap(pack_texts, overlap_tokens)
    return _to_chunks(pack_texts, method)


def chunk_python(text: str, *, target_tokens: int = 512, overlap_tokens: int = 50) -> list[Chunk]:
    """AST-aware code chunker. Never imports or executes the code (parse only).

    Aligns to top-level statements (imports, class/def, assignments, call
    blocks). If ``ast.parse`` fails or a single literal node dwarfs the budget
    (generated archives with huge literals), it falls back to a deterministic
    line/token window with overlap — never silent truncation.
    """
    norm = normalize_for_hash(text)
    if not norm:
        return []
    target_chars = max(1, target_tokens * CHARS_PER_TOKEN)
    lines = norm.split("\n")
    try:
        tree = ast.parse(norm)
    except SyntaxError:
        return _to_chunks(_line_window(lines, target_chars, overlap_tokens), "ast_fallback")

    # Collect (start_line, end_line) spans of top-level statements (1-based).
    spans: list[tuple[int, int]] = []
    for node in tree.body:
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start)
        spans.append((start, end))
    if not spans:
        return _to_chunks(_line_window(lines, target_chars, overlap_tokens), "ast_fallback")

    # Coverage spans: extend each statement span to begin right after the prior
    # statement, so inter-statement lines (standalone comments, blank lines) are
    # included and never silently dropped. Without this, comment-only lines are
    # not AST nodes and would be lost, failing the no-silent-truncation guard
    # (frozen §10) and hiding searchable comments. Trailing lines after the last
    # statement are appended as a final span. Every source line is covered once.
    # For contiguous statements (prev_end+1 == s) these are identical to `spans`,
    # so already-passing sources are chunked byte-identically.
    cov_spans: list[tuple[int, int]] = []
    prev_end = 0
    for (s, e) in spans:
        cov_spans.append((prev_end + 1, e))
        prev_end = e
    if prev_end < len(lines):
        cov_spans.append((prev_end + 1, len(lines)))

    method = "ast"
    groups: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for (s, e) in cov_spans:
        chunk_lines = lines[s - 1:e]
        block = "\n".join(chunk_lines)
        block_chars = len(block)
        if block_chars > target_chars * 1.5:
            # A single generated literal dwarfs the budget → window it, keep AST
            # alignment for the surrounding groups.
            if current:
                groups.append(current)
                current = []
                current_chars = 0
            method = "ast_fallback"
            groups.extend([ln] for ln in _fixed_window(block, target_tokens))
            continue
        if current and current_chars + block_chars > target_chars:
            groups.append(current)
            current = []
            current_chars = 0
        current.append(block)
        current_chars += block_chars
    if current:
        groups.append(current)

    pack_texts = ["\n".join(g) for g in groups if g]
    # Overlap by carrying the last block of the previous group forward.
    if overlap_tokens > 0 and len(pack_texts) > 1:
        overlap_chars = overlap_tokens * CHARS_PER_TOKEN
        out = [pack_texts[0]]
        for prev, cur in zip(pack_texts, pack_texts[1:]):
            tail = prev[-overlap_chars:] if overlap_chars < len(prev) else prev
            out.append((tail + "\n" + cur).strip() if tail.strip() else cur)
        pack_texts = out
    return _to_chunks(pack_texts, method)


def _line_window(lines: list[str], target_chars: int, overlap_tokens: int) -> list[str]:
    """Bounded line/token window (the parser-fallback path of :func:`chunk_python`).

    Packs consecutive lines up to ``target_chars``. A single line that ALONE
    exceeds the budget is windowed into ``<= target_chars`` pieces (CHUNKING v2
    bounded-fallback fix) rather than emitted whole, so an oversized or
    unparsable source still yields bounded chunks with complete coverage. The
    inner accumulation guard already refuses to absorb an over-long line into a
    non-empty pack (it flushes first), so this top-of-loop windowing is the only
    place an over-long line can land; normal-length lines accumulate unchanged,
    keeping an already-correct source byte-identical (the v1->v2 chunk-map no-op
    for real cohorts). Overlap is applied to the resulting packs.
    """

    packs: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        # A single line that dwarfs the budget: window it (bounded + complete
        # coverage). Reachable only here because the inner guard below flushes a
        # non-empty pack before it would absorb an over-long line.
        if len(ln) > target_chars:
            for piece in (
                ln[k:k + target_chars] for k in range(0, len(ln), target_chars)
            ):
                if piece.strip():
                    packs.append(piece)
            i += 1
            continue
        acc: list[str] = []
        acc_chars = 0
        j = i
        while j < n and acc_chars < target_chars:
            ln2 = lines[j]
            if acc_chars + len(ln2) > target_chars and acc:
                break
            acc.append(ln2)
            acc_chars += len(ln2) + 1
            j += 1
        if acc:
            packs.append("\n".join(acc))
        i = j if j > i else i + 1
    return _apply_overlap(packs, overlap_tokens)


def _to_chunks(texts: list[str], method: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    for idx, text in enumerate(texts):
        stripped = text.strip()
        if not stripped:
            continue
        chunks.append(
            Chunk(
                index=len(chunks),
                text=stripped,
                hash=chunk_hash(stripped),
                start_line=0,
                end_line=0,
                method=method,
                token_estimate=_tokens(stripped),
            )
        )
    return chunks


def chunk_text(
    text: str,
    *,
    mode: str = "prose",
    target_tokens: int = 512,
    overlap_tokens: int = 50,
) -> list[Chunk]:
    """Dispatch to :func:`chunk_prose` or :func:`chunk_python` by representation mode."""
    if mode == "python":
        return chunk_python(text, target_tokens=target_tokens, overlap_tokens=overlap_tokens)
    return chunk_prose(text, target_tokens=target_tokens, overlap_tokens=overlap_tokens)


def coverage_ok(text: str, chunks: list[Chunk]) -> bool:
    """No-silent-truncation guard: chunks must cover the full normalized source.

    Overlap characters are duplicated, so we check the *set of characters* of the
    source is a subset of the union of chunk texts (ignoring whitespace runs and
    the deterministic normalization). A drop or head-truncation fails this.
    """
    norm = normalize_for_hash(text)
    if not norm:
        return not chunks
    source_chars = re.sub(r"\s+", "", norm)
    joined = "".join(c.text for c in chunks)
    joined_chars = re.sub(r"\s+", "", joined)
    return set(source_chars) <= set(joined_chars) and len(joined_chars) >= len(source_chars)
