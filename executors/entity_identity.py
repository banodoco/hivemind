"""Hivemind entity-type / result-kind identity + exact snowflake string handling.

Plan task 2.4. This module is the single source of truth for the two vocabularies
that meet at every shared-index and JSON boundary, and for keeping Discord
snowflakes as exact strings so they survive JSON round-trips:

- ``entity_type`` — the small, stable INTERNAL vocabulary used by the shared
  embedding index, chunk collapse, deletion, and hydration (plan AD-2):
  ``message`` | ``resource`` | ``distillation``. A resource's concrete public
  kind (``article``, ``workflow``, ``transcript``, …) is a separate
  ``result_kind`` that never changes the embedding identity.
- ``result_kind`` — the concrete PUBLIC kind that appears as ``kind`` in
  ``unified_feed`` and in the search response. ``workflow`` is a
  backwards-compatible alias for ``resource`` (plan AD-1): both map to
  ``entity_type='resource'``.

Why two vocabularies: a workflow reclassified as an article (or vice versa) must
not invalidate its embeddings or change its shared-index identity; only
``result_kind`` moves. Embedding identity, chunk collapse, deletion, and
hydration key on the immutable ``(entity_type, item_id)``; filtering and
presentation may additionally use ``result_kind`` (plan AD-2).

Snowflakes: Discord message ids are ~18–19 digit integers stored as ``bigint``
and exceed the float64-safe integer range (2^53). A JSON number would silently
round them, so every item id travels as a **string** at the API, the shared
index (``content_embeddings.item_id`` is text), citations, and ``get_item``.
``unified_feed`` already casts ``message_id::text``; this module is the explicit,
tested boundary so no path reintroduces a numeric id.

Pure stdlib, offline, dependency-free. It does not touch the database; the
SQL-side identity helpers (schema/024) mirror these mappings for the lexical/
embedding SQL paths.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "ENTITY_MESSAGE",
    "ENTITY_RESOURCE",
    "ENTITY_DISTILLATION",
    "ENTITY_TYPES",
    "RESOURCE_GENERIC_KIND",
    "RESULT_KIND_MESSAGE",
    "RESULT_KIND_DISTILLATION",
    "RESULT_KIND_RESOURCE",
    "RESULT_KIND_WORKFLOW",
    "KNOWN_RESOURCE_KINDS",
    "CITE_ITEM_KINDS",
    "JSON_SAFE_INTEGER_MAX",
    "entity_type_for_result_kind",
    "result_kind_is_resource",
    "normalize_result_kind",
    "entity_type_for_cite_kind",
    "stringify_item_id",
    "is_json_safe_integer",
    "item_id_survives_json_roundtrip",
    "is_discord_snowflake",
    "embedding_identity_key",
]


# ---------------------------------------------------------------------------
# entity_type vocabulary (plan AD-2 — internal, stable, small)
# ---------------------------------------------------------------------------

ENTITY_MESSAGE = "message"
ENTITY_RESOURCE = "resource"
ENTITY_DISTILLATION = "distillation"

#: The complete internal entity_type vocabulary. Adding a value is a schema
#: change (the content_embeddings CHECK and the search SQL enumerate these).
ENTITY_TYPES: tuple[str, ...] = (ENTITY_MESSAGE, ENTITY_RESOURCE, ENTITY_DISTILLATION)


# ---------------------------------------------------------------------------
# result_kind vocabulary (plan AD-1 — concrete public kind in unified_feed)
# ---------------------------------------------------------------------------

#: The generic resource kind on the public surface (the `kind=resource` input).
RESOURCE_GENERIC_KIND = "resource"

#: Concrete public kinds that map to entity_type='resource'. Open in principle
#: (external_resources.kind is free text), but these are the known/frequent ones;
#: ``result_kind_is_resource`` treats any non-{message,distillation} kind as a
#: resource so a new resource kind needs no code change.
RESULT_KIND_MESSAGE = "message"
RESULT_KIND_DISTILLATION = "distillation"
RESULT_KIND_RESOURCE = "resource"
RESULT_KIND_WORKFLOW = "workflow"  # backwards-compatible alias for resource (AD-1)

KNOWN_RESOURCE_KINDS: tuple[str, ...] = (
    "article",
    "blog_post",
    "transcript",
    "workflow",
    "repo",
    "guide",
    "doc",
)

#: distillation_cites.item_kind vocabulary (schema/001): the cite polymorphic
#: kinds. They map 1:1 to entity_type (a cite never targets a concrete resource
#: sub-kind, only the generic resource entity).
CITE_ITEM_KINDS: tuple[str, ...] = (ENTITY_MESSAGE, ENTITY_RESOURCE, ENTITY_DISTILLATION)


# ---------------------------------------------------------------------------
# result_kind <-> entity_type mapping (the 2.4 completion signal: they agree)
# ---------------------------------------------------------------------------


def result_kind_is_resource(kind: Any) -> bool:
    """Return whether a public kind is a resource (any concrete resource kind).

    Every kind that is not ``message`` or ``distillation`` is a resource,
    including the generic ``resource`` and the ``workflow`` alias, and including
    resource kinds not yet enumerated. This is why adding a resource kind needs
    no identity change: it still maps to ``entity_type='resource'``.
    """

    if not isinstance(kind, str) or not kind:
        return False
    return kind not in (RESULT_KIND_MESSAGE, RESULT_KIND_DISTILLATION)


def entity_type_for_result_kind(kind: Any) -> str:
    """Map a public ``result_kind`` to the internal ``entity_type``.

    ``message`` → ``message``; ``distillation`` → ``distillation``; every other
    kind (``resource``, ``workflow``, ``article``, ``transcript``, …) →
    ``resource``. Raises ``ValueError`` on an empty/non-string kind so a
    malformed caller fails loudly rather than silently filing under resource.
    """

    if not isinstance(kind, str) or not kind.strip():
        raise ValueError(f"result_kind must be a non-empty string, got {kind!r}")
    if kind == RESULT_KIND_MESSAGE:
        return ENTITY_MESSAGE
    if kind == RESULT_KIND_DISTILLATION:
        return ENTITY_DISTILLATION
    return ENTITY_RESOURCE


def normalize_result_kind(kind: Any) -> str:
    """Return the canonical public result_kind, resolving the workflow alias.

    ``workflow`` stays ``workflow`` on the public surface (it is a real concrete
    kind in external_resources), but callers may also pass the generic
    ``resource``; both are valid resource kinds. This helper does not collapse
    ``workflow`` to ``resource`` — they are distinct public kinds that share one
    entity_type. It only validates and trims.
    """

    if not isinstance(kind, str):
        raise ValueError(f"result_kind must be a string, got {kind!r}")
    normalized = kind.strip()
    if not normalized:
        raise ValueError("result_kind must not be empty")
    return normalized


def entity_type_for_cite_kind(item_kind: Any) -> str:
    """Map a ``distillation_cites.item_kind`` to ``entity_type`` (1:1).

    The cite vocabulary (schema/001) is exactly {message, resource, distillation}
    — the same as entity_type — so a cite maps directly. Provided as a named
    boundary so the cite string conversion (Phase 4) plugs in here.
    """

    if item_kind not in CITE_ITEM_KINDS:
        raise ValueError(
            f"cite item_kind must be one of {CITE_ITEM_KINDS}, got {item_kind!r}"
        )
    return item_kind  # cite kinds ARE the entity_type vocabulary


# ---------------------------------------------------------------------------
# Snowflake-safe exact-string item-id handling
# ---------------------------------------------------------------------------

#: JavaScript's MAX_SAFE_INTEGER (2^53 - 1). Integers at or above 2^53 are not
#: all exactly representable as JSON numbers and round when parsed by a JS/edge
#: JSON.parse. Discord snowflakes (~18-19 digits) are far above this.
JSON_SAFE_INTEGER_MAX = 2 ** 53 - 1


def is_json_safe_integer(value: Any) -> bool:
    """Return whether *value* is an int exactly representable as a JSON number.

    False for non-ints and for ints above the float64-safe range. Used to decide
    whether an id may travel as a JSON number (never, for snowflakes) or must be
    a string. Hivemind always uses strings; this predicate documents *why*.
    """

    # bool is a subclass of int — exclude it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return -JSON_SAFE_INTEGER_MAX <= value <= JSON_SAFE_INTEGER_MAX


def is_discord_snowflake(value: Any) -> bool:
    """Return whether *value* looks like a Discord snowflake (big numeric id).

    A snowflake is a positive integer (typically 17–20 digits) that exceeds the
    JSON-safe range. This is informational — Hivemind stringifies EVERY item id
    regardless, so a non-snowflake id is also carried as a string for one
    consistent boundary.
    """

    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and value.isdigit():
        n = int(value)
    else:
        return False
    return n > JSON_SAFE_INTEGER_MAX


def stringify_item_id(value: Any) -> str:
    """Return *value* as an exact item-id string, never a float.

    Accepts a ``str`` or an ``int`` (e.g. a raw ``bigint`` message_id /
    external_resources.id / distillations.id). An int is rendered with
    :func:`str` (exact, no precision loss, no exponent). A float is rejected — a
    float id means precision was already lost upstream. A numeric string is
    returned verbatim (leading zeros preserved, since ids are opaque strings).
    Whitespace-only and empty values raise ``ValueError``.
    """

    if isinstance(value, bool):
        raise ValueError(f"item_id must not be a bool, got {value!r}")
    if isinstance(value, float):
        raise ValueError(
            "item_id must not be a float — a float id means JSON precision was "
            "already lost; pass the original bigint/str"
        )
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("item_id must not be empty/whitespace")
        return value
    raise ValueError(f"item_id must be str or int, got {type(value).__name__}")


def item_id_survives_json_roundtrip(value: Any) -> bool:
    """Return whether *value* survives a JSON round-trip as the SAME string.

    The contract: an item id carried as a JSON **string** round-trips exactly,
    even for snowflakes; carried as a JSON number it does not. This proves the
    boundary by encoding the stringified id and decoding it back.
    """

    string_id = stringify_item_id(value)
    decoded = json.loads(json.dumps(string_id))
    return decoded == string_id and isinstance(decoded, str)


# ---------------------------------------------------------------------------
# Embedding/shared-index identity key (plan AD-2; mirrors the SQL PK)
# ---------------------------------------------------------------------------


def embedding_identity_key(
    entity_type: str,
    item_id: Any,
    representation_type: str,
    chunk_index: int,
) -> tuple[str, str, str, int]:
    """Return the immutable/shared-index identity tuple for an embedding row.

    ``(entity_type, item_id_string, representation_type, chunk_index)``. Mirrors
    the ``content_embeddings`` primary key minus contract_id (the contract is the
    "which model/dimension" axis; this key is the "which content" axis). Used for
    chunk collapse and dedup before RRF.
    """

    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"entity_type must be one of {ENTITY_TYPES}, got {entity_type!r}")
    string_id = stringify_item_id(item_id)
    if not isinstance(chunk_index, int) or isinstance(chunk_index, bool) or chunk_index < 0:
        raise ValueError(f"chunk_index must be a non-negative int, got {chunk_index!r}")
    return (entity_type, string_id, representation_type, chunk_index)
