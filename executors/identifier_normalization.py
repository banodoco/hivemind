"""Frozen identifier-normalization + alias contract for Hivemind (plan task 1.4).

This module is the **authoritative Phase-1 reference contract** for normalizing
model / node / code identifiers across Unicode, case, and punctuation, and for
the explicit, bounded alias representation the exact-identifier candidate arm
(plan tasks 1.5–1.7) consumes. It implements plan task 1.4 against the frozen
task-1.1 lexical contract.

It is intentionally **pure, offline, stdlib-only, and dependency-free**. It does
not connect to a database, does not call an embedding provider, and does not
create indexes or RPCs (those are tasks 1.5–1.9; this is the *contract*, not the
candidate implementation). The behaviour pinned here was **confirmed
byte-for-byte against IMMUTABLE PostgreSQL 14 functions on a real, isolated
cluster** (see ``docs/hybrid-search/phase1-identifier-normalization.md`` §
"Evidence"); the normalization facts in this module's docstrings are observed
output, not memory.

Everything below is a frozen decision recorded in
``docs/hybrid-search/phase1-identifier-normalization.{md,json}`` dated
2026-07-28. Bumping any ``*_VERSION`` constant is a contract change that must
re-derive every indexed identifier column, generated column, and alias row
(plan AD-4); it is never a silent edit.

Relationship to the frozen task-1.1 contract
--------------------------------------------
``executors.lexical_contract`` already froze the three query/identifier helpers
this task builds on. They are **re-exported** here (not redefined) so there is
one source of truth:

  - :data:`normalize_query`        — NFC + whitespace-collapse (no lower); FTS text.
  - :data:`normalize_identifier`   — compact key (NFC + lower + strip + drop separators).
  - :data:`identifier_aliases`     — ordered (compact, spaced) forms.

This module adds the **punctuation-preserving** form as a first-class named
helper, the ordered multi-form view, the explicit casefold policy, the alias
representation with provenance/version/collision/priority/safe-update, and the
no-silent-natural-language-rewrite guarantee.

References (read-only, not imported at runtime):
- Frozen lexical contract: ``executors.lexical_contract`` (task 1.1).
- Pumpernickel ``app/services/retrieval.py``: query-normalize pattern (ported).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

# Single source of truth: re-export the frozen task-1.1 helpers unchanged.
from executors.lexical_contract import (  # noqa: F401  (re-exported public API)
    normalize_query,
    normalize_identifier,
    identifier_aliases,
)
from executors.lexical_contract import _IDENTIFIER_SEPARATOR_RE  # frozen spec detail

# ---------------------------------------------------------------------------
# Frozen contract versions
# ---------------------------------------------------------------------------

#: Bumped when the casefold policy, the separator set, the preserve rule, or the
#: alias form set changes. Part of every indexed identifier column / generated
#: column freshness check and every alias row. Mirrors the lexical-contract
#: versioning rule: a bump re-derives affected state (plan AD-4).
IDENTIFIER_NORMALIZATION_VERSION = 1

#: Bumped when the alias provenance vocabulary, priority weights, collision rule,
#: or safe-update rule changes. A live alias row carries the version at which it
#: was registered; rows whose version is below the current value are tombstoned
#: (``live = False``) during a controlled re-register, never silently dropped.
IDENTIFIER_ALIAS_VERSION = 1

#: The frozen Unicode + casefold policy, in one line for docs/validators.
#: "NFC then ASCII/Latin Unicode lowercase via ``str.lower()``" — explicitly NOT
#: ``str.casefold()``. See CASEFOLD_POLICY_RATIONALE and DISTINCT_CHARS.
CASEFOLD_POLICY = "NFC + str.lower() (not str.casefold())"

#: The explicit separator/punctuation character set removed by the compact form
#: (mirrors ``executors.lexical_contract._IDENTIFIER_SEPARATOR_RE``). Frozen so
#: the Python reference and the SQL ``hivemind_normalize_identifier`` agree on
#: exactly which characters are treated as identifier separators.
IDENTIFIER_SEPARATOR_CHARS: frozenset[str] = frozenset(
    " \t\n\r\x0b\x0c"  # ASCII whitespace (\s subset); Unicode whitespace handled by \s in both engines
    ".-/\\,:;(){}[]@\"'`_"
)

CASEFOLD_POLICY_RATIONALE = """
Two Unicode case-folding choices were considered for the exact-identifier arm:
``str.lower()`` (after NFC) and ``str.casefold()``.

Decision: NFC + ``str.lower()``. ``str.casefold()`` is more aggressive and is
designed for caseless matching, but it introduces multi-character expansions
('ß' -> 'ss', ligatures 'ﬁ' -> 'fi', 'ﬀ' -> 'ff') and combining-mark insertions
('İ' -> 'i' + U+0307) that PostgreSQL has no single built-in to reproduce, which
would break the byte-for-byte SQL/Python parity this contract requires.

NFC + ``str.lower()`` agrees with PostgreSQL ``lower(value COLLATE <icu-und>)``
across the BMP Latin / Latin-Extended / Greek / Cyrillic / CJK ranges that
constitute the Hivemind identifier corpus, verified on an isolated PostgreSQL 14
cluster built ``--with-icu`` (the locale-independent path; see § Evidence). For
the small set of code points where ``str.lower()`` and aggressive case folding
diverge, the folding is intentionally NOT applied — those forms remain distinct
(see DISTINCT_CHARS). This is the documented set of non-equivalences.
""".strip()

#: Characters / forms that are INTENTIONALLY kept distinct (NOT folded). This is
#: the explicit "characters/forms that intentionally remain distinct" list the
#: plan requires. Each entry documents WHY no collapse happens.
DISTINCT_CHARS: dict[str, str] = {
    "ß": "German eszett stays 'ß' (not folded to 'ss'); 'groß' != 'gross'.",
    "İ": "Turkish dotted capital I -> 'i' + U+0307 under lower(); not folded to plain 'i'. "
         "(Both Python lower() and ICU lower(value COLLATE und) agree on this.)",
    "ﬁﬂﬀﬃﬄ": "Latin ligatures stay composed (not expanded to fi/fl/ff); 'ﬁle' != 'file'. "
              "NFC (not NFKC) is the frozen policy, so compatibility decompositions are not applied.",
    "Ａ-Ｚａ-ｚ０-９": "Fullwidth Latin/digits stay fullwidth; 'ＡＢＣ' != 'ABC'. NFC does not "
                    "compatibility-fold fullwidth forms (NFKC would).",
    "⁰¹²³": "Superscript digits stay distinct; '²' != '2'. (NFC, not NFKC.)",
    "a/а/α": "Homoglyphs are NOT folded: Latin 'a' (U+0061), Cyrillic 'а' (U+0430), and Greek "
             "'α' (U+03B1) remain three distinct compact keys. Confusable folding is a separate, "
             "riskier transform that would merge unrelated identities; it is deliberately absent.",
}


# ---------------------------------------------------------------------------
# Punctuation-preserving form (the second frozen key)
# ---------------------------------------------------------------------------

_WHITESPACE_RUN_RE = re.compile(r"\s+")


def normalize_identifier_preserve(value: str) -> str:
    """Punctuation-preserving normalized form for the exact-identifier arm.

    NFC + ``str.lower()`` + strip + collapse all whitespace runs to single
    spaces. Unlike :func:`normalize_identifier` (the compact key), this KEEPS the
    identifier's punctuation (``.``, ``-``, ``_``, ``/``, ``:``, …) as visible
    token separators, so the exact FTS phrase arm (``phraseto_tsquery('simple')``)
    and a punctuation-aware trigram path can match the literal shape of a name.

    This is the second element of :func:`identifier_aliases`; exposed as a named
    function so the contract names both forms and the SQL mirror
    (``hivemind_normalize_identifier_preserve``) has a 1:1 Python reference.

    Examples (observed, frozen):

      ``"Wan 2.2"``              -> ``"wan 2.2"``   (dot + spacing kept)
      ``"  Wan   2.2  "``        -> ``"wan 2.2"``   (edges stripped, runs collapsed)
      ``"FLUX.1"``               -> ``"flux.1"``
      ``"lightx2v_I2V_14B"``     -> ``"lightx2v_i2v_14b"``
      ``"models//flux.safetensors"`` -> ``"models//flux.safetensors"``  (slashes kept)
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value)).lower().strip()
    return _WHITESPACE_RUN_RE.sub(" ", text)


def identifier_forms(value: str) -> tuple[str, ...]:
    """Ordered, de-duplicated identifier forms to index for ``value``.

    Always yields the **compact** key (:func:`normalize_identifier`) first, then
    the **punctuation-preserving** key (:func:`normalize_identifier_preserve`)
    when it is non-empty and distinct. This is the frozen multi-form view the
    exact-identifier side index stores; it is contract-equal to the frozen
    :func:`identifier_aliases` (both produce the same ordered, de-duplicated
    pair), asserted by the validator/tests.

    Storing both forms means ``Wan 2.2``, ``Wan2.2``, ``wan_2.2`` and ``WAN 2.2``
    all resolve to the same rows through the compact key, while a query that
    preserves the literal shape (``"wan 2.2"``) still matches through the
    preserve key + a punctuation-aware index.
    """
    compact = normalize_identifier(value)
    preserve = normalize_identifier_preserve(value)
    forms: list[str] = []
    for form in (compact, preserve):
        if form and form not in forms:
            forms.append(form)
    return tuple(forms)


def separator_chars() -> frozenset[str]:
    """The frozen separator/punctuation character set (for docs/tests)."""
    return IDENTIFIER_SEPARATOR_CHARS


# ---------------------------------------------------------------------------
# Alias representation (explicit, bounded; for the candidate arm)
# ---------------------------------------------------------------------------
#
# Design intent (plan 1.4): aliases let a query written one way surface an item
# known another way (e.g. query "controlnet" -> the workflow whose
# ``searchable_aliases`` includes "control net"). They are an explicit, vetted,
# versioned CANDIDATE-EXPANSION structure — never a free-text rewrite layer. The
# two safety properties below are the whole point:
#
#   1. NO SILENT NATURAL-LANGUAGE REWRITE. Aliases only ADD candidate identity
#      edges for the exact-identifier / trigram arm. They are NEVER substituted
#      into FTS query text, NEVER spliced into a prose tsvector, and NEVER used
#      to relabel one item's canonical identity as another's. (The bounded B-arm
#      projection of ``searchable_aliases`` into resource prose is task 1.2's
#      separate, frozen projection; this is the parallel exact-identifier index.)
#
#   2. COLLISIONS ARE REPORTED, NOT SILENTLY MERGED. When two distinct canonical
#      identities share an alias compact key, both stay valid candidates ordered
#      by deterministic priority, and the collision is surfaced for operator
#      disambiguation. No identity is ever dropped to "resolve" an alias.

ENTITY_RESOURCE = "resource"
ENTITY_DISTILLATION = "distillation"
ENTITY_MESSAGE = "message"
ALIAS_ENTITY_TYPES = (ENTITY_RESOURCE, ENTITY_DISTILLATION, ENTITY_MESSAGE)

#: Provenance vocabulary (frozen). An alias records exactly where it came from;
#: there is no "unknown" provenance. ``derived_canonical`` is the canonical name's
#: own compact/preserve forms auto-derived at index time (so a name is always
#: discoverable by its own normalization), distinct from human/semantics aliases.
PROV_WORKFLOW_SEARCHABLE_ALIASES = "workflow_semantics.searchable_aliases"
PROV_NODE_CLASS = "workflow_semantics.node_class"
PROV_MODEL_FILENAME = "workflow_semantics.models"
PROV_CURATED = "curated"
PROV_DERIVED_CANONICAL = "derived_canonical"
PROVENANCE_VOCABULARY: tuple[str, ...] = (
    PROV_CURATED,
    PROV_WORKFLOW_SEARCHABLE_ALIASES,
    PROV_NODE_CLASS,
    PROV_MODEL_FILENAME,
    PROV_DERIVED_CANONICAL,
)

#: Deterministic provenance -> priority weight. Higher wins ties. Curated (human)
#: aliases outrank automatically-derived ones; within semantics, the
#: searchable_aliases field outranks node/model lists. Ties after this are broken
#: by stable canonical identity order (see AliasRegistry.resolve_alias_candidates).
PROVENANCE_PRIORITY: dict[str, int] = {
    PROV_CURATED: 100,
    PROV_WORKFLOW_SEARCHABLE_ALIASES: 80,
    PROV_NODE_CLASS: 70,
    PROV_MODEL_FILENAME: 60,
    PROV_DERIVED_CANONICAL: 40,
}


def provenance_priority(provenance: str) -> int:
    """Frozen deterministic priority for a provenance label (0 for unknown)."""
    return PROVENANCE_PRIORITY.get(provenance, 0)


def _identity(kind: str, item_id: str) -> str:
    return f"{kind}:{item_id}"


@dataclass(frozen=True)
class AliasEntry:
    """One explicit, bounded, versioned alias of a canonical identity.

    ``alias_compact`` / ``alias_preserve`` are the indexed forms; ``canonical_*``
    is the true identity an alias expands TO. An alias row never carries the
    matched secret/credential-bearing bytes — provenance_detail is non-secret
    (a workflow id or curation batch label).
    """

    canonical_kind: str            # one of ALIAS_ENTITY_TYPES
    canonical_id: str              # text identity (snowflake-safe)
    canonical_name: str            # authoritative display name
    alias_text: str                # alias as written
    alias_compact: str             # normalize_identifier(alias_text)
    alias_preserve: str            # normalize_identifier_preserve(alias_text)
    provenance: str                # one of PROVENANCE_VOCABULARY
    provenance_detail: str = ""    # non-secret provenance ref (workflow id / batch)
    provenance_version: int = 1
    alias_version: int = IDENTIFIER_ALIAS_VERSION
    priority: int = 0
    live: bool = True

    @property
    def canonical_key(self) -> str:
        return normalize_identifier(self.canonical_name)

    @property
    def identity(self) -> str:
        return _identity(self.canonical_kind, self.canonical_id)


class AliasValidationError(ValueError):
    """Raised when an alias cannot be registered under the frozen rules."""


class AliasRegistry:
    """Deterministic, in-memory mirror of the ``identifier_aliases`` reference.

    Pure and offline: it is the testable reference for the SQL representation in
    ``schema/005_identifier_normalization.sql``. Registration is idempotent on
    (canonical identity, alias compact, provenance); collisions are detected and
    reported, never silently merged; resolution returns deterministic candidate
    identity lists — never a rewritten natural-language string.
    """

    def __init__(self) -> None:
        # keyed by (identity, alias_compact, provenance) for idempotent upsert
        self._by_key: dict[tuple[str, str, str], AliasEntry] = {}

    # -- registration (idempotent; safe-update aware) ------------------------
    def register(
        self,
        *,
        canonical_kind: str,
        canonical_id: str,
        canonical_name: str,
        alias_text: str,
        provenance: str,
        provenance_detail: str = "",
        provenance_version: int = 1,
        priority: int | None = None,
        alias_version: int | None = None,
        live: bool = True,
    ) -> AliasEntry:
        """Register one alias. Idempotent on (identity, compact, provenance).

        Validates entity type and provenance, derives the compact/preserve forms,
        assigns the current ``IDENTIFIER_ALIAS_VERSION`` (unless overridden for a
        replay), and defaults priority from the provenance weight. Re-registering
        the same logical alias refreshes its version/priority/liveness (hash-skip
        if unchanged). Safe-update: superseding an alias is done by re-registering
        the live set at a new version; stale-version rows become ``live=False``
        tombstones, they are never deleted in place.
        """
        if canonical_kind not in ALIAS_ENTITY_TYPES:
            raise AliasValidationError(f"canonical_kind not in {ALIAS_ENTITY_TYPES}: {canonical_kind!r}")
        if provenance not in PROVENANCE_VOCABULARY:
            raise AliasValidationError(f"provenance not in vocabulary: {provenance!r}")
        if not str(canonical_id).strip():
            raise AliasValidationError("canonical_id is required")
        if not str(canonical_name).strip():
            raise AliasValidationError("canonical_name is required")

        alias_compact = normalize_identifier(alias_text)
        alias_preserve = normalize_identifier_preserve(alias_text)
        if not alias_compact:
            raise AliasValidationError(
                f"alias normalizes to an empty compact key (rejected): {alias_text!r}"
            )
        if normalize_identifier(canonical_name) == "":
            raise AliasValidationError(
                f"canonical_name normalizes to an empty compact key (rejected): {canonical_name!r}"
            )

        prio = provenance_priority(provenance) if priority is None else int(priority)
        entry = AliasEntry(
            canonical_kind=canonical_kind,
            canonical_id=str(canonical_id),
            canonical_name=str(canonical_name),
            alias_text=str(alias_text),
            alias_compact=alias_compact,
            alias_preserve=alias_preserve,
            provenance=provenance,
            provenance_detail=provenance_detail,
            provenance_version=provenance_version,
            alias_version=IDENTIFIER_ALIAS_VERSION if alias_version is None else int(alias_version),
            priority=prio,
            live=bool(live),
        )
        key = (entry.identity, entry.alias_compact, entry.provenance)
        self._by_key[key] = entry
        return entry

    def register_canonical_forms(
        self,
        *,
        canonical_kind: str,
        canonical_id: str,
        canonical_name: str,
        provenance_detail: str = "",
    ) -> tuple[AliasEntry, ...]:
        """Auto-derive the canonical name's own alias forms (provenance derived_canonical).

        Guarantees an item is discoverable by its own normalization even if no
        human/semantics alias names it.
        """
        out: list[AliasEntry] = []
        for form in identifier_forms(canonical_name):
            out.append(
                self.register(
                    canonical_kind=canonical_kind,
                    canonical_id=canonical_id,
                    canonical_name=canonical_name,
                    alias_text=form,
                    provenance=PROV_DERIVED_CANONICAL,
                    provenance_detail=provenance_detail,
                )
            )
        return tuple(out)

    # -- introspection -------------------------------------------------------
    @property
    def entries(self) -> list[AliasEntry]:
        """All registered entries in deterministic order (identity, compact, provenance)."""
        return sorted(
            self._by_key.values(),
            key=lambda e: (e.identity, e.alias_compact, e.provenance),
        )

    @property
    def live_entries(self) -> list[AliasEntry]:
        return [e for e in self.entries if e.live]

    def collisions(self) -> dict[str, list[AliasEntry]]:
        """Alias compact keys shared by >=2 DISTINCT canonical identities.

        A collision is REPORTED for operator disambiguation; it is never silently
        resolved by dropping an identity. Returns ``{alias_compact: [entries]}``
        for every live alias_compact owned by more than one distinct identity.
        """
        by_compact: dict[str, set[str]] = {}
        entries_by_compact: dict[str, list[AliasEntry]] = {}
        for e in self.live_entries:
            by_compact.setdefault(e.alias_compact, set()).add(e.identity)
            entries_by_compact.setdefault(e.alias_compact, []).append(e)
        return {
            compact: sorted(entries_by_compact[compact], key=lambda e: (e.identity, e.provenance))
            for compact, ids in by_compact.items()
            if len(ids) > 1
        }

    def resolve_alias_candidates(self, alias_compact: str) -> list[AliasEntry]:
        """Deterministic candidate entries for a compact key (NO NL rewrite).

        Returns live entries whose ``alias_compact`` equals the query, ordered by
        a deterministic, locale-independent tie-break: priority DESC, provenance
        weight DESC, then stable identity ASC. The candidate layer (task 1.7)
        consumes these as ADDITIONAL identity filters; it never rewrites the
        query string or the canonical identity of any item.
        """
        key = normalize_identifier(alias_compact)
        hits = [e for e in self.live_entries if e.alias_compact == key]
        hits.sort(key=lambda e: (-e.priority, -provenance_priority(e.provenance), e.identity))
        return hits

    def expand_query_identifiers(self, query: str) -> frozenset[str]:
        """Additional candidate identities a query should surface via aliases.

        Implements the no-silent-natural-language-rewrite guarantee: returns a
        SET OF CANONICAL IDENTITIES ("kind:id"), never a rewritten query string.
        If the query's compact key is itself a registered alias, the
        exact-identifier arm expands to all identities that alias resolves to;
        otherwise nothing is added. Aliases are never substituted into FTS query
        text or the prose tsvector, and arbitrary natural language is never
        synthesized into an alias.
        """
        key = normalize_identifier(query)
        if not key:
            return frozenset()
        # Only expand when the query's compact form is a known alias key; we never
        # synthesize aliases from arbitrary natural language.
        if key not in {e.alias_compact for e in self.live_entries}:
            return frozenset()
        return frozenset(e.identity for e in self.resolve_alias_candidates(key))


# ---------------------------------------------------------------------------
# Bounded alias form set per canonical name (the side-index payload)
# ---------------------------------------------------------------------------


def canonical_alias_forms(name: str) -> tuple[str, ...]:
    """All normalized forms a canonical name should index (derived_canonical).

    Convenience alias for :func:`identifier_forms`; documents that the canonical
    name's own side-index payload is exactly its (compact, preserve) forms.
    """
    return identifier_forms(name)


def summarize() -> dict:
    """Compact dict of the frozen contract (for the validator / docs)."""
    return {
        "identifier_normalization_version": IDENTIFIER_NORMALIZATION_VERSION,
        "identifier_alias_version": IDENTIFIER_ALIAS_VERSION,
        "casefold_policy": CASEFOLD_POLICY,
        "separator_chars": sorted(IDENTIFIER_SEPARATOR_CHARS),
        "distinct_chars": dict(DISTINCT_CHARS),
        "provenance_vocabulary": list(PROVENANCE_VOCABULARY),
        "provenance_priority": dict(PROVENANCE_PRIORITY),
        "alias_entity_types": list(ALIAS_ENTITY_TYPES),
        "forms_example": {
            "wan22": identifier_forms("Wan 2.2"),
            "flux1": identifier_forms("FLUX.1"),
            "filename": identifier_forms("lightx2v_I2V_14B.safetensors"),
        },
    }


__all__ = [
    # versions / policy
    "IDENTIFIER_NORMALIZATION_VERSION",
    "IDENTIFIER_ALIAS_VERSION",
    "CASEFOLD_POLICY",
    "CASEFOLD_POLICY_RATIONALE",
    "DISTINCT_CHARS",
    "IDENTIFIER_SEPARATOR_CHARS",
    "separator_chars",
    # frozen task-1.1 re-exports (single source of truth)
    "normalize_query",
    "normalize_identifier",
    "identifier_aliases",
    # task-1.4 forms
    "normalize_identifier_preserve",
    "identifier_forms",
    "canonical_alias_forms",
    # alias representation
    "ENTITY_RESOURCE",
    "ENTITY_DISTILLATION",
    "ENTITY_MESSAGE",
    "ALIAS_ENTITY_TYPES",
    "PROV_WORKFLOW_SEARCHABLE_ALIASES",
    "PROV_NODE_CLASS",
    "PROV_MODEL_FILENAME",
    "PROV_CURATED",
    "PROV_DERIVED_CANONICAL",
    "PROVENANCE_VOCABULARY",
    "PROVENANCE_PRIORITY",
    "provenance_priority",
    "AliasEntry",
    "AliasRegistry",
    "AliasValidationError",
    "summarize",
]
