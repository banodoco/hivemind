#!/usr/bin/env python3
"""Deterministic validator for the Phase-1 lexical contract (plan task 1.1).

Checks:
  1. The machine-readable contract JSON exists, parses, and is internally
     consistent with the frozen reference module ``executors.lexical_contract``
     (single source of truth): config, versions, ts_rank flag, RRF K, weight
     multipliers, and every weighted-spec expression must match exactly.
  2. ``post_hoc_locked`` is true (the contract refuses to validate otherwise).
  3. The reference module behaves correctly on fixtures, including the OBSERVED
     simple-vs-english evidence captured on a real PostgreSQL 14 instance:
       - normalize_identifier collapses the Wan 2.2 / Wan2.2 / wan_2.2 variants.
       - query_arms routes multi-term natural queries to websearch, exact names
         and quoted phrases to phraseto, and always emits the ident arm.
       - the frozen weighted expressions are well-formed (config + setweight +
         coalesce, weight letters in A..C).

Offline only: no database, no network, no provider. Exit 0 on success, 1 on any
check failure (with a clear message).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import executors.lexical_contract as L  # noqa: E402

CONTRACT_JSON = REPO / "docs" / "hybrid-search" / "phase1-lexical-contract.json"
DECISION_MD = REPO / "docs" / "hybrid-search" / "phase1-lexical-contract.md"


class CheckError(Exception):
    pass


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise CheckError(msg)
    print(f"  ok: {msg}")


def check_contract_module_sync(contract: dict) -> None:
    print("[1/4] JSON contract <-> reference module sync")
    _check(contract["lexical_config"] == L.LEXICAL_CONFIG, f"lexical_config == {L.LEXICAL_CONFIG!r}")
    _check(
        contract["lexicalization_version"] == L.LEXICALIZATION_VERSION,
        f"lexicalization_version == {L.LEXICALIZATION_VERSION}",
    )
    _check(
        contract["ts_rank_normalization"] == L.TS_RANK_NORMALIZATION,
        f"ts_rank_normalization == {L.TS_RANK_NORMALIZATION}",
    )
    _check(contract["rrf_k"] == L.RRF_K, f"rrf_k == {L.RRF_K}")
    _check(
        contract["weight_multipliers"] == L.TS_RANK_WEIGHT_MULTIPLIERS,
        "weight_multipliers match module defaults (A=1.0 B=0.4 C=0.2 D=0.1)",
    )
    # Every weighted spec in the JSON must match the module's expression exactly.
    mod_expr = {
        (s["entity_type"], s["representation_type"]): s["expression"]
        for s in L.summarize()["specs"]
    }
    _check(len(contract["specs"]) == len(mod_expr), "spec count matches module")
    for spec in contract["specs"]:
        key = (spec["entity_type"], spec["representation_type"])
        _check(
            key in mod_expr,
            f"spec {key} present in module",
        )
        _check(
            spec["expression"] == mod_expr[key],
            f"spec {key} expression matches module byte-for-byte",
        )
        _check(spec["config"] == L.LEXICAL_CONFIG, f"spec {key} config == simple")
    # Message bare source + chunk identity shape.
    _check(contract["message_bare_source"] == L.MESSAGE_BARE_SOURCE, "message_bare_source matches")
    _check(
        contract["chunk_collapse"]["identity"] == list(L.LEXICAL_DOCUMENT_IDENTITY),
        "chunk collapse identity matches module",
    )
    _check(
        contract["chunk_collapse"]["one_identity"] == L.CHUNK_COLLAPSE_RULE.one_identity,
        "one-identity collapse rule matches",
    )


def check_contract_locked(contract: dict) -> None:
    print("[2/4] Post-hoc lock + shape")
    _check(bool(contract.get("post_hoc_locked")), "post_hoc_locked is true")
    _check(contract.get("version", "").startswith("lexical-contract/"), "contract version tagged")
    _check(contract.get("task") == "1.1", "task == 1.1")
    sv = contract["simple_vs_english_evidence"]
    _check(sv["config_mismatch"]["to_tsvector('english','WanVideoSampler') @@ websearch_to_tsquery('simple','WanVideoSampler')"] is False,
           "observed config-mismatch recorded as False")
    _check(sv["stemming"]["simple_WanVideoSampler"] == "wanvideosampler",
           "observed simple stem of WanVideoSampler recorded")
    _check(sv["stemming"]["english_WanVideoSampler"] == "wanvideosampl",
           "observed english stem of WanVideoSampler recorded (stemmer strips -er)")
    _check(sv["identifier_query_mismatch"]["match"] is False,
           "observed Wan2.2-vs-'Wan 2.2' mismatch recorded as False")
    _check(sv["identifier_query_mismatch"]["resolution"].endswith("'wan22'"),
           "normalize_identifier resolution recorded")


def check_reference_behaviour() -> None:
    print("[3/4] Reference-module behaviour on fixtures")
    # normalize_identifier collapses the golden spelling_variant family.
    variants = ["Wan 2.2", "Wan2.2", "wan_2.2", "WAN 2.2", "wan2.2"]
    normed = {L.normalize_identifier(v) for v in variants}
    _check(normed == {"wan22"}, f"normalize_identifier collapses {variants} -> 'wan22'")
    _check(L.normalize_identifier("FLUX.1") == "flux1", "FLUX.1 -> flux1")
    _check(L.normalize_identifier("LTX-Video") == "ltxvideo", "LTX-Video -> ltxvideo")
    _check(L.normalize_identifier("model.safetensors") == "modelsafetensors",
           "model.safetensors -> modelsafetensors")
    _check(L.normalize_identifier("lightx2v_I2V_14B.safetensors") == "lightx2vi2v14bsafetensors",
           "filename -> compact form")
    # normalize_query is NFC + whitespace collapse only (no lowercasing).
    _check(L.normalize_query("  Wan   2.2  ") == "Wan 2.2", "normalize_query collapses whitespace, no lowercase")
    _check(L.normalize_query(None) == "", "normalize_query(None) -> ''")
    _check(L.normalize_identifier(None) == "", "normalize_identifier(None) -> ''")
    # query_arms routing.
    arms = {a["arm"] for a in L.query_arms("controlnet settings")}
    _check(arms == {"fts", "ident"}, "multi-term natural query -> fts + ident (no phrase)")
    arms = {a["arm"] for a in L.query_arms('"block swap"')}
    _check(arms == {"fts", "phrase", "ident"}, 'quoted phrase -> fts + phrase + ident')
    arms = {a["arm"] for a in L.query_arms("WanVideoSampler")}
    _check(arms == {"fts", "phrase", "ident"}, "single bare name -> fts + phrase + ident")
    arms = {a["arm"] for a in L.query_arms("Wan 2.2")}
    _check("ident" in arms and "phrase" not in arms, "spaced version name -> fts + ident (phrase suppressed)")
    # config + query constructors are uniform and explicit.
    _check(L.QUERY_CONSTRUCTOR_DEFAULT == "websearch_to_tsquery", "default query constructor is websearch_to_tsquery")
    _check(L.QUERY_CONSTRUCTOR_PHRASE == "phraseto_tsquery", "phrase constructor is phraseto_tsquery")
    # Eligibility predicates are non-empty and encode the 0.2 gaps.
    msg_pred = " ".join(L.MESSAGE_ELIGIBILITY.predicates)
    _check("is_deleted = false" in msg_pred, "message eligibility encodes is_deleted=false (0.2 D5)")
    _check("author_optout_enabled" in msg_pred, "message eligibility binds author opt-out behind a flag (0.2 D6)")
    _check(
        "status IN ('pending', 'approved')" in " ".join(L.DISTILLATION_ELIGIBILITY.predicates),
        "distillation eligibility encodes pending/approved",
    )
    _check(
        "hivemind_workflow_python_state" in " ".join(L.WORKFLOW_PYTHON_ELIGIBILITY.predicates),
        "workflow_python eligibility encodes safe/quarantine gate (0.8)",
    )


def check_expressions_well_formed() -> None:
    print("[4/4] Frozen SQL expressions are well-formed")
    for spec in L.WEIGHTED_SPECS.values():
        expr = spec.expression()
        _check(f"'{L.LEXICAL_CONFIG}'::regconfig" in expr,
               f"{spec.entity_type}/{spec.representation_type} uses explicit 'simple'::regconfig")
        _check("setweight(" in expr, f"{spec.entity_type}/{spec.representation_type} is weighted")
        _check("coalesce(" in expr, f"{spec.entity_type}/{spec.representation_type} is null-safe (coalesce)")
        for arm in spec.arms:
            _check(arm.weight in L.WEIGHTS, f"weight {arm.weight!r} in A..D")
    # Message + rank + tie-break.
    _check(L.MESSAGE_BARE_SOURCE == "content", "message uses bare content field")
    _check(L.LEXICAL_RANK_SQL == f"ts_rank(tsv, tsq, {L.TS_RANK_NORMALIZATION})",
           "lexical rank uses frozen ts_rank normalization")
    _check(
        "lexical_rank DESC" in L.TIE_BREAK and "created_at DESC" in L.TIE_BREAK and "item_id ASC" in L.TIE_BREAK,
        "tie-break is rank-desc, created-desc, identity-asc",
    )
    # Snowflake boundary.
    _check(all("::text" in c for c in L.SNOWFLAKE_TEXT_COLUMNS),
           "snowflake columns cast to ::text at boundary")
    _check(L.ITEM_IDS_ARE_TEXT is True, "item_ids are text (no JSON-number coercion)")


def main() -> int:
    if not CONTRACT_JSON.exists():
        print(f"ERROR: contract JSON not found: {CONTRACT_JSON}", file=sys.stderr)
        return 1
    try:
        contract = json.loads(CONTRACT_JSON.read_text())
    except json.JSONDecodeError as exc:
        print(f"ERROR: contract JSON is invalid: {exc}", file=sys.stderr)
        return 1
    checks = [
        ("contract/module sync", lambda: check_contract_module_sync(contract)),
        ("contract locked + evidence", lambda: check_contract_locked(contract)),
        ("reference behaviour", check_reference_behaviour),
        ("expressions well-formed", check_expressions_well_formed),
    ]
    for name, fn in checks:
        try:
            fn()
        except CheckError as exc:
            print(f"\nFAIL [{name}]: {exc}", file=sys.stderr)
            return 1
    # Decision record mentions the decisive facts.
    md = DECISION_MD.read_text() if DECISION_MD.exists() else ""
    for needle in ("'simple'", "english", "WanVideoSampler", "wanvideosampl",
                   "bounded workflow-code", "is_deleted", "allow_content_sharing"):
        if needle not in md:
            print(f"\nFAIL [decision record]: missing {needle!r}", file=sys.stderr)
            return 1
    print("\nOK: lexical contract validated (module/JSON sync + observed evidence + behaviour).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
