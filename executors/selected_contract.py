"""Task 2.16 — the selected-contract identity surface (additive, stdlib-only).

Production contract ids (:mod:`executors.embedding_contract`) are DIMENSION-ONLY:
``provider + model + dimension + canonicalization_version + chunking_version``.
That is intentionally insufficient to distinguish the two 384-d candidates the
task-2.14 grid compared — ``384-small`` (prose 512/50, workflow_python 512/50)
and ``384-large`` (prose 1024/100, workflow_python 2048/100) both hash to the
SAME production bigint ``6368594834396668537`` (under chunking v2; the v1 chunking
dimension-only id was ``7571371577804399660``). The task-2.16 HNSW pilot needs an
unambiguous, frozen contract literal for its partial-index predicate
(``WHERE contract_id = <literal>``), so the small and large configurations can
never share one index.

This module freezes ONE deterministic selected-contract identity that EXTENDS
(rather than replaces) the production one, using the natural compatible preimage
already bound by the accepted task-2.14 evaluation: the production
``contract_identity_input`` of the selected base contract, joined by the same
unit separator to the frozen chunk-config identity. The selected bigint is then
derived with the EXACT same rule the production ``contract_id`` uses (SHA-256 of
the UTF-8 preimage, first 8 bytes big-endian, masked to a positive 63-bit
bigint) — only the preimage is longer (it carries chunk-config disambiguation).

Nothing here mutates the historical contract registry's identity semantics,
activates a contract, calls a provider, or touches production. It is a pure
deterministic helper consumed by schema/033, the rehearsal/benchmark scripts,
and the tests. ``executors`` remain stdlib-only (``hashlib`` only).
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass
from typing import Any, Mapping

from executors.embedding_contract import (
    ContractSpec,
    contract_identity_input,
    contract_id,
)

__all__ = [
    "SELECTED_CONTRACT_FIELD_SEP",
    "CHUNK_CONFIG_IDENTITY_VERSION",
    "SELECTED_PROVIDER",
    "SELECTED_MODEL",
    "SELECTED_DIMENSION",
    "SELECTED_CANONICALIZATION_VERSION",
    "SELECTED_CHUNKING_VERSION",
    "SELECTED_PROSE_TARGET_TOKENS",
    "SELECTED_PROSE_OVERLAP_TOKENS",
    "SELECTED_PYTHON_TARGET_TOKENS",
    "SELECTED_PYTHON_OVERLAP_TOKENS",
    "REP_PROSE",
    "REP_WORKFLOW_PYTHON",
    "selected_chunk_config_identity",
    "selected_contract_identity_input",
    "selected_contract_sha256",
    "selected_contract_id",
    "SELECTED_CHUNK_CONFIG_IDENTITY",
    "SELECTED_CONTRACT_PREIMAGE",
    "SELECTED_CONTRACT_SHA256_HEX",
    "SELECTED_CONTRACT_ID",
    "HISTORICAL_DIMENSION_ONLY_PREIMAGE",
    "HISTORICAL_DIMENSION_ONLY_ID",
    "EVAL_CONTRACT_ID_HEX",
    "SelectedContract",
    "SelectedContractResolutionError",
    "resolve_selected_contract_from_artifacts",
    "TASK_2_14_DECISION_PATH",
    "TASK_2_14_MANIFEST_PATH",
    "EXPECTED_GOLDEN_SHA256",
    "EXPECTED_CANDIDATE_GRID_HASH",
    "EXPECTED_DECISION_POLICY_HASH",
    "EXPECTED_EVAL3_COHORT_MANIFEST_HASH",
    "EXPECTED_DECISION_CORE_SHA256",
]

# Unit separator, identical to executors.embedding_contract._CONTRACT_FIELD_SEP
# and eval/retrieval/semantic._CFG_SEP. Re-declared (not imported as a private
# name) so this module's frozen identity does not depend on a private symbol.
SELECTED_CONTRACT_FIELD_SEP = "\x1f"

# ---------------------------------------------------------------------------
# The frozen selected contract (accepted task-2.14 winner: 384-small).
# ---------------------------------------------------------------------------

# The chunk-config IDENTITY SCHEME version. This tags the identity's axes
# (representation_type, target_tokens, overlap_tokens), NOT the chunker
# algorithm, and is a DIFFERENT axis from SELECTED_CHUNKING_VERSION below:
#   * SELECTED_CHUNKING_VERSION (== 2) is the chunker BEHAVIOR version; it was
#     bumped 1 -> 2 for the bounded oversized workflow-Python fallback fix.
#   * CHUNK_CONFIG_IDENTITY_VERSION (== 1) is the identity SCHEME version; the
#     fallback fix added no identity axis, so the scheme is unchanged and stays
#     1. The selected chunk-config identity is therefore
#     ``chunk_config\x1fv1\x1fprose#512/50\x1fworkflow_python#512/50``.
# Bumping this to 2 (a prior implementation) was an invalid double version bump
# and is reverted: chunking behavior is v2, but the chunk-config identity is v1.
CHUNK_CONFIG_IDENTITY_VERSION = 1

SELECTED_PROVIDER = "openai"
SELECTED_MODEL = "text-embedding-3-small"
SELECTED_DIMENSION = 384
SELECTED_CANONICALIZATION_VERSION = 1
SELECTED_CHUNKING_VERSION = 2

REP_PROSE = "prose"
REP_WORKFLOW_PYTHON = "workflow_python"

SELECTED_PROSE_TARGET_TOKENS = 512
SELECTED_PROSE_OVERLAP_TOKENS = 50
SELECTED_PYTHON_TARGET_TOKENS = 512
SELECTED_PYTHON_OVERLAP_TOKENS = 50

# The production ContractSpec of the selected BASE contract (dimension-only).
SELECTED_BASE_CONTRACT = ContractSpec(
    provider=SELECTED_PROVIDER,
    model=SELECTED_MODEL,
    dimension=SELECTED_DIMENSION,
    canonicalization_version=SELECTED_CANONICALIZATION_VERSION,
    chunking_version=SELECTED_CHUNKING_VERSION,
)


def _chunk_spec_identity(representation_type: str, target_tokens: int, overlap_tokens: int) -> str:
    """Mirror eval/retrieval/semantic.ChunkConfigSpec.identity byte-for-byte."""

    return f"{representation_type}#{target_tokens}/{overlap_tokens}"


def selected_chunk_config_identity(
    *,
    prose_target_tokens: int = SELECTED_PROSE_TARGET_TOKENS,
    prose_overlap_tokens: int = SELECTED_PROSE_OVERLAP_TOKENS,
    python_target_tokens: int = SELECTED_PYTHON_TARGET_TOKENS,
    python_overlap_tokens: int = SELECTED_PYTHON_OVERLAP_TOKENS,
    version: int = CHUNK_CONFIG_IDENTITY_VERSION,
) -> str:
    """The frozen chunk-config identity.

    Mirrors eval/retrieval/semantic.ChunkConfigIdentity.identity byte-for-byte:
    ``chunk_config\\x1fv<version>\\x1fprose#<t>/<o>\\x1fworkflow_python#<t>/<o>``.
    For the selected 384-small contract this is the literal bound by the accepted
    task-2.14 decision artifact
    (``chunk_config\\u001fv1\\u001fprose#512/50\\u001fworkflow_python#512/50``).
    """

    return SELECTED_CONTRACT_FIELD_SEP.join(
        (
            "chunk_config",
            f"v{version}",
            _chunk_spec_identity(REP_PROSE, prose_target_tokens, prose_overlap_tokens),
            _chunk_spec_identity(
                REP_WORKFLOW_PYTHON, python_target_tokens, python_overlap_tokens
            ),
        )
    )


def selected_contract_identity_input(
    *,
    provider: str = SELECTED_PROVIDER,
    model: str = SELECTED_MODEL,
    dimension: int = SELECTED_DIMENSION,
    canonicalization_version: int = SELECTED_CANONICALIZATION_VERSION,
    chunking_version: int = SELECTED_CHUNKING_VERSION,
    chunk_config_identity: str | None = None,
) -> str:
    """The full selected-contract preimage.

    Equals the production ``contract_identity_input`` of the base contract, plus
    the unit separator, plus the chunk-config identity — i.e. exactly the
    ``eval_contract_identity_input`` the accepted task-2.14 evaluation hashed
    into ``eval_contract_id``. Joining with the same separator (rather than a
    new one) is what makes the extension natural and backward-compatible: the
    selected identity is a strict superset of the production base preimage.
    """

    base = contract_identity_input(
        ContractSpec(
            provider=provider,
            model=model,
            dimension=dimension,
            canonicalization_version=canonicalization_version,
            chunking_version=chunking_version,
        )
    )
    cfg = chunk_config_identity if chunk_config_identity is not None else selected_chunk_config_identity()
    return SELECTED_CONTRACT_FIELD_SEP.join((base, cfg))


def selected_contract_sha256(preimage: str) -> str:
    """SHA-256 hex of a selected-contract preimage (UTF-8)."""

    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def selected_contract_id(preimage: str) -> int:
    """Derive the selected-contract bigint from a full preimage.

    Same rule as production ``contract_id``: SHA-256, first 8 bytes big-endian,
    masked with ``0x7fffffffffffffff`` so the result is a positive PostgreSQL
    bigint. Only the preimage differs (it is longer; it embeds chunk-config).
    """

    digest = hashlib.sha256(preimage.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


# ---------------------------------------------------------------------------
# Frozen literals for the selected 384-small contract (computed once, asserted
# against the accepted task-2.14 literals so any drift is a loud failure).
# ---------------------------------------------------------------------------

SELECTED_CHUNK_CONFIG_IDENTITY = selected_chunk_config_identity()
SELECTED_CONTRACT_PREIMAGE = selected_contract_identity_input(
    chunk_config_identity=SELECTED_CHUNK_CONFIG_IDENTITY
)
SELECTED_CONTRACT_SHA256_HEX = selected_contract_sha256(SELECTED_CONTRACT_PREIMAGE)
SELECTED_CONTRACT_ID = selected_contract_id(SELECTED_CONTRACT_PREIMAGE)

# The accepted task-2.14 evaluation contract id (sha256 of the SAME full
# preimage, first 16 hex chars). Used only as an independent corroboration that
# the full preimage above is byte-identical to the evaluator's frozen identity.
EVAL_CONTRACT_ID_HEX = SELECTED_CONTRACT_SHA256_HEX[:16]

# The historical dimension-only production id of the 384-d base contract. It is
# explicitly INSUFFICIENT to distinguish small vs large chunk configs (both 384-d
# candidates share it) and MUST NOT be the selected-literal HNSW contract id.
HISTORICAL_DIMENSION_ONLY_PREIMAGE = contract_identity_input(SELECTED_BASE_CONTRACT)
HISTORICAL_DIMENSION_ONLY_ID = contract_id(SELECTED_BASE_CONTRACT)


def _assert_frozen_literals() -> None:
    """Guard: the computed selected literals must match the accepted 2.14 values.

    These are the cross-language anchors. If derivation ever drifts from the
    accepted task-2.14 decision, fail loudly at import rather than silently
    selecting a different contract.

    NOTE: this guard uses explicit ``raise`` statements (not ``assert``) so it
    still fires under ``python -O`` / ``PYTHONOPTIMIZE``, where bare ``assert``
    statements are stripped and a drift would pass silently.
    """

    expected_chunk_config_identity = (
        "chunk_config\x1fv1\x1fprose#512/50\x1fworkflow_python#512/50"
    )
    expected_preimage = (
        "openai\x1ftext-embedding-3-small\x1f384\x1f1\x1f2"
        "\x1fchunk_config\x1fv1\x1fprose#512/50\x1fworkflow_python#512/50"
    )
    expected_sha256 = (
        "12e19cdb566b87445ab2d3563e6cb948f58801f78f8395878fc9e0c2457d5462"
    )
    expected_selected_id = 1360541028304258884
    expected_eval_hex = "12e19cdb566b8744"
    expected_historical_id = 6368594834396668537

    if SELECTED_CHUNK_CONFIG_IDENTITY != expected_chunk_config_identity:
        raise RuntimeError(
            "selected chunk-config identity drift: "
            f"{SELECTED_CHUNK_CONFIG_IDENTITY!r} != {expected_chunk_config_identity!r}"
        )
    if SELECTED_CONTRACT_PREIMAGE != expected_preimage:
        raise RuntimeError(
            "selected contract preimage drift: "
            f"{SELECTED_CONTRACT_PREIMAGE!r} != {expected_preimage!r}"
        )
    if SELECTED_CONTRACT_SHA256_HEX != expected_sha256:
        raise RuntimeError(
            "selected contract sha256 drift: "
            f"{SELECTED_CONTRACT_SHA256_HEX!r} != {expected_sha256!r}"
        )
    if SELECTED_CONTRACT_ID != expected_selected_id:
        raise RuntimeError(
            "selected contract id drift: "
            f"{SELECTED_CONTRACT_ID!r} != {expected_selected_id!r}"
        )
    if EVAL_CONTRACT_ID_HEX != expected_eval_hex:
        raise RuntimeError(
            "eval contract id hex drift: "
            f"{EVAL_CONTRACT_ID_HEX!r} != {expected_eval_hex!r}"
        )
    if HISTORICAL_DIMENSION_ONLY_ID != expected_historical_id:
        raise RuntimeError(
            "historical dimension-only id drift: "
            f"{HISTORICAL_DIMENSION_ONLY_ID!r} != {expected_historical_id!r}"
        )
    # The selected literal must NOT equal the dimension-only id.
    if SELECTED_CONTRACT_ID == HISTORICAL_DIMENSION_ONLY_ID:
        raise RuntimeError(
            "selected contract id must not equal the dimension-only id: "
            f"{SELECTED_CONTRACT_ID!r}"
        )


_assert_frozen_literals()


# ---------------------------------------------------------------------------
# Task 2.17 — artifact-derived selected-contract resolution (fail-closed).
#
# The constants above are the frozen selected contract. T1 requires the selected
# config to be DERIVED from the accepted task-2.14 artifacts (decision JSON +
# frozen manifest) and validated, fail-closed, against those constants — never
# taken on faith. ``resolve_selected_contract_from_artifacts`` parses the two
# accepted artifacts, resolves exactly one selected config, reproduces the full
# identity, and raises on ANY field mismatch, a missing field, a non-winner, a
# 1536 dimension, ``production_activated=true``, the ambiguous legacy bigint
# being treated as sufficient, or a tampered manifest hash. It does NOT mutate
# activation state; it is a read-only resolver consumed by the task-2.17 tests
# and rehearsal. Importing this module never touches the artifacts (the resolver
# is called explicitly), so a missing artifact file is not an import-time fault.
# ---------------------------------------------------------------------------

#: Repository + accepted artifact paths (read-only; the resolver takes overrides).
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TASK_2_14_DECISION_PATH = _REPO_ROOT / "docs" / "hybrid-search" / "task-2.14-embedding-decision.json"
TASK_2_14_MANIFEST_PATH = _REPO_ROOT / "docs" / "hybrid-search" / "task-2.14-frozen-manifest.json"

#: The accepted task-2.14 frozen-manifest hashes (independent anchors; a tampered
#: or corrupt artifact fails closed here). Mirrors the preflight's binding list.
EXPECTED_GOLDEN_SHA256 = "d29eb3e1867c5721d662faacda3d684444efd25b9cb8194737c8b8fde135a9be"
EXPECTED_CANDIDATE_GRID_HASH = "1aa27a54e9f3edd340dfbab7b2847c302a5ebf13909f4202175b873f5e701074"
EXPECTED_DECISION_POLICY_HASH = "7d2c3dcb51ae99727fbea8e9520718523243bf036b3d9d59a2e2c0dfe21094ad"
EXPECTED_EVAL3_COHORT_MANIFEST_HASH = "0ae1c3f4deef42322d10deb53af1d97d6aec75e90424a9a4da3bf735e94a9b34"
EXPECTED_DECISION_CORE_SHA256 = "05caa124d4b47d5863439e33bcf301842a838b52ba391cb58cb211faffb9d360"


@dataclass(frozen=True)
class SelectedContract:
    """The one resolved selected contract, derived + validated from artifacts.

    Every field is validated against the frozen constants in this module, so a
    ``SelectedContract`` instance is proof the artifacts and the production
    surfaces agree. ``production_activated`` is recorded (and must be False) but
    is never flipped here — activation is a later, separate rollout decision.
    """

    candidate_name: str
    provider: str
    model: str
    dimension: int
    canonicalization_version: int
    chunking_version: int
    chunk_config_version: int
    prose_target_tokens: int
    prose_overlap_tokens: int
    python_target_tokens: int
    python_overlap_tokens: int
    chunk_config_identity: str
    eval_contract_id_hex: str
    selected_contract_id: int
    selected_contract_sha256_hex: str
    historical_dimension_only_id: int
    production_activated: bool
    golden_sha256: str
    candidate_grid_hash: str
    decision_policy_hash: str
    eval3_cohort_manifest_hash: str
    decision_core_sha256: str


class SelectedContractResolutionError(RuntimeError):
    """Raised when the accepted artifacts do not resolve to the one selected contract."""


def _require(cond: bool, what: str) -> None:
    if not cond:
        raise SelectedContractResolutionError(f"selected-contract resolution failed: {what}")


def _load_json(path: pathlib.Path) -> Mapping[str, Any]:
    if not path.exists():
        raise SelectedContractResolutionError(f"artifact not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SelectedContractResolutionError(f"unreadable artifact {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise SelectedContractResolutionError(f"artifact {path} is not a JSON object")
    return data


def resolve_selected_contract_from_artifacts(
    decision_path: pathlib.Path | str | None = None,
    manifest_path: pathlib.Path | str | None = None,
) -> SelectedContract:
    """Parse the accepted task-2.14 artifacts and resolve exactly one selected config.

    Fail-closed on: a missing/unreadable artifact; no selection block; a missing
    field; more than one (or zero) selected candidate; a selected candidate that
    is not the recorded winner; a 1536 dimension; ``production_activated=true``;
    any field drift vs the frozen constants; the ambiguous legacy dimension-only
    bigint being equal to the selected id; or a tampered manifest hash. Returns
    the validated :class:`SelectedContract`.
    """

    decision = _load_json(pathlib.Path(decision_path) if decision_path else TASK_2_14_DECISION_PATH)
    manifest = _load_json(pathlib.Path(manifest_path) if manifest_path else TASK_2_14_MANIFEST_PATH)

    selection = decision.get("selection")
    _require(isinstance(selection, Mapping), "decision.selection missing")
    _require(selection.get("production_activated") is False, "production_activated is not false")

    dim = selection.get("selected_production_dimension")
    _require(dim == SELECTED_DIMENSION, f"selected dimension {dim!r} != {SELECTED_DIMENSION}")

    prose = selection.get("selected_prose_chunk_contract") or {}
    python = selection.get("selected_workflow_python_chunk_contract") or {}
    _require(prose.get("target_tokens") == SELECTED_PROSE_TARGET_TOKENS, "prose target drift")
    _require(prose.get("overlap_tokens") == SELECTED_PROSE_OVERLAP_TOKENS, "prose overlap drift")
    _require(python.get("target_tokens") == SELECTED_PYTHON_TARGET_TOKENS, "python target drift")
    _require(python.get("overlap_tokens") == SELECTED_PYTHON_OVERLAP_TOKENS, "python overlap drift")

    cfg_identity = selection.get("selected_chunk_config_identity")
    _require(cfg_identity == SELECTED_CHUNK_CONFIG_IDENTITY, "chunk_config_identity drift")

    eval_id = selection.get("selected_evaluation_contract_id")
    _require(eval_id == EVAL_CONTRACT_ID_HEX, f"eval_contract_id {eval_id!r} != {EVAL_CONTRACT_ID_HEX}")

    handoff = selection.get("production_chunking_version_handoff") or {}
    _require(handoff.get("canonicalization_version") == SELECTED_CANONICALIZATION_VERSION, "canon version drift")
    _require(handoff.get("chunking_version") == SELECTED_CHUNKING_VERSION, "chunking version drift")
    _require(handoff.get("chunk_config_version") == CHUNK_CONFIG_IDENTITY_VERSION, "chunk-config version drift")

    legacy = selection.get("selected_base_contract_id_dimension_only")
    _require(str(legacy) == str(HISTORICAL_DIMENSION_ONLY_ID), "legacy dimension-only id drift")
    # The ambiguous legacy bigint must NEVER equal the selected full id.
    _require(int(legacy) != SELECTED_CONTRACT_ID, "legacy bigint must not equal the selected id")

    # Exactly one selected candidate, and it is the recorded winner (no 1536).
    candidates = decision.get("candidates") or []
    _require(isinstance(candidates, list) and candidates, "decision.candidates missing")
    selected_names = [
        c["candidate"]["name"]
        for c in candidates
        if isinstance(c, Mapping) and (c.get("selection_state") or {}).get("selected") is True
    ]
    _require(len(selected_names) == 1, f"expected exactly one selected candidate, got {selected_names}")
    winner = decision.get("winner") or {}
    _require(winner.get("name") == selected_names[0], "selected candidate is not the recorded winner")
    _require(winner.get("dimension") == SELECTED_DIMENSION, "winner dimension drift")
    selected_candidate = next(
        c["candidate"] for c in candidates
        if isinstance(c, Mapping) and (c.get("selection_state") or {}).get("selected") is True
    )
    # Bind every identity-bearing field from the candidate artifact, rather
    # than trusting only the duplicated selection summary literals.
    for field, expected in (
        ("provider", SELECTED_PROVIDER),
        ("model", SELECTED_MODEL),
        ("dimension", SELECTED_DIMENSION),
        ("canonicalization_version", SELECTED_CANONICALIZATION_VERSION),
        ("chunking_version", SELECTED_CHUNKING_VERSION),
        ("chunk_config_version", CHUNK_CONFIG_IDENTITY_VERSION),
        ("chunk_config_identity", SELECTED_CHUNK_CONFIG_IDENTITY),
        ("eval_contract_id", EVAL_CONTRACT_ID_HEX),
    ):
        _require(selected_candidate.get(field) == expected, f"selected candidate {field} drift")
    _require(selected_candidate.get("prose") == {
        "target_tokens": SELECTED_PROSE_TARGET_TOKENS,
        "overlap_tokens": SELECTED_PROSE_OVERLAP_TOKENS,
    }, "selected candidate prose config drift")
    _require(selected_candidate.get("python") == {
        "target_tokens": SELECTED_PYTHON_TARGET_TOKENS,
        "overlap_tokens": SELECTED_PYTHON_OVERLAP_TOKENS,
    }, "selected candidate python config drift")
    # Non-winners must remain inactive/evaluation-only (no parallel activation).
    for c in candidates:
        cand = c.get("candidate") or {}
        st = c.get("selection_state") or {}
        if cand.get("name") != selected_names[0]:
            _require(st.get("selected") is not True, f"non-winner {cand.get('name')!r} is selected")
            _require(
                cand.get("dimension") != SELECTED_DIMENSION or st.get("active") is not True,
                f"non-winner {cand.get('name')!r} is active",
            )

    # Manifest hashes (tamper/corruption anchors).
    golden = (manifest.get("golden") or {}).get("sha256")
    _require(golden == EXPECTED_GOLDEN_SHA256, "golden sha256 drift")
    grid = manifest.get("candidate_grid_hash")
    _require(grid == EXPECTED_CANDIDATE_GRID_HASH, "candidate_grid_hash drift")
    policy = manifest.get("decision_policy_hash")
    _require(policy is not None and policy == EXPECTED_DECISION_POLICY_HASH, "decision_policy_hash drift")
    cohort = manifest.get("eval3_cohort_manifest_hash")
    _require(cohort == EXPECTED_EVAL3_COHORT_MANIFEST_HASH, "eval3_cohort_manifest_hash drift")
    core = manifest.get("decision_deterministic_core_sha256")
    _require(core == EXPECTED_DECISION_CORE_SHA256, "decision_core_sha256 drift")

    # The derived full identity must reproduce the frozen selected literal.
    derived_id = selected_contract_id(SELECTED_CONTRACT_PREIMAGE)
    _require(derived_id == SELECTED_CONTRACT_ID, "derived selected id drift")
    _require(
        SELECTED_CONTRACT_SHA256_HEX[:16] == EVAL_CONTRACT_ID_HEX,
        "selected sha256/eval-id corroboration drift",
    )

    return SelectedContract(
        candidate_name=selected_names[0],
        provider=SELECTED_PROVIDER,
        model=SELECTED_MODEL,
        dimension=SELECTED_DIMENSION,
        canonicalization_version=SELECTED_CANONICALIZATION_VERSION,
        chunking_version=SELECTED_CHUNKING_VERSION,
        chunk_config_version=CHUNK_CONFIG_IDENTITY_VERSION,
        prose_target_tokens=SELECTED_PROSE_TARGET_TOKENS,
        prose_overlap_tokens=SELECTED_PROSE_OVERLAP_TOKENS,
        python_target_tokens=SELECTED_PYTHON_TARGET_TOKENS,
        python_overlap_tokens=SELECTED_PYTHON_OVERLAP_TOKENS,
        chunk_config_identity=SELECTED_CHUNK_CONFIG_IDENTITY,
        eval_contract_id_hex=EVAL_CONTRACT_ID_HEX,
        selected_contract_id=SELECTED_CONTRACT_ID,
        selected_contract_sha256_hex=SELECTED_CONTRACT_SHA256_HEX,
        historical_dimension_only_id=HISTORICAL_DIMENSION_ONLY_ID,
        production_activated=False,
        golden_sha256=golden,
        candidate_grid_hash=grid,
        decision_policy_hash=policy,
        eval3_cohort_manifest_hash=cohort,
        decision_core_sha256=core,
    )
