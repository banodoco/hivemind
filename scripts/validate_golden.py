#!/usr/bin/env python3
"""Deterministic validator + opt-in live identity check for the golden set (task 0.6).

Offline (default) checks the production golden set + evidence snapshot for:
  * minimum case count (>= 100) and judged/no-hit balance;
  * required category coverage (every plan/task-required family present);
  * unique case ids and no duplicate (query, filters) cases;
  * grade/rubric validity (grades in 0..3; primary/strong/marginal all used);
  * filter validity (item_ids => exactly one kind; modes/since well-formed);
  * snapshot integrity (every judged identity is in the snapshot; every snapshot
    id is anchored in the live evidence; snowflakes are strings in the file);
  * no-hit consistency.

Opt-in live check (``--live`` or ``HIVEMIND_EVAL_NETWORK=1``) re-validates every
distinct judged identity against the live public corpus with bounded read-only
GETs on ``unified_feed`` (existence only; no bodies). This is the read-only live
identity validation the task requires; it is OFF by default so the offline test
suite never touches the network.

Exit code 0 when there are no problems; 2 when there are. All output is redacted.

Run::

    python3 scripts/validate_golden.py                       # offline validation
    python3 scripts/validate_golden.py --live                # + live identity check
    python3 scripts/validate_golden.py --json report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SCRIPTS = REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from eval.retrieval.loader import load_corpus, load_golden_set  # noqa: E402
from eval.retrieval.schema import normalize_kind  # noqa: E402

import verify_access as va  # noqa: E402
from executors._common import resolve_anon_key, resolve_endpoint  # noqa: E402

redact = va.redact

GOLDEN_DIR = REPO / "eval" / "retrieval" / "golden" if (REPO := REPO_ROOT) else None
GOLDEN_DIR = REPO_ROOT / "eval" / "retrieval" / "golden"
DEFAULT_GOLDEN = GOLDEN_DIR / "golden-v1.json"
DEFAULT_CORPUS = GOLDEN_DIR / "corpus-v1.json"
DEFAULT_EVIDENCE = GOLDEN_DIR / "evidence-v1.json"

MIN_CASES = 100
MIN_JUDGED = 90

# Every family the plan (Golden set section) and the task brief require.
REQUIRED_CATEGORIES = {
    "exact_name", "workflow_code", "multi_term", "paraphrase", "settings",
    "named_author", "channel_scoped", "time_scoped", "cross_source",
    "best_is_distillation", "best_is_message", "best_is_resource",
    "long_resource_chunk", "workflow_only", "single_workflow",
    "spelling_variant", "no_hit", "workflow_python_evidence", "pending_status",
    "timeout_prone", "selective_filter", "code_fragment", "snowflake",
}

VALID_GRADES = {0, 1, 2, 3}


# ---------------------------------------------------------------------------
# Offline validation (pure; unit-tested)
# ---------------------------------------------------------------------------


def _records(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _golden_cases(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict) and isinstance(raw.get("cases"), list):
        return raw["cases"]
    if isinstance(raw, list):
        return raw
    raise ValueError("golden file must be a list of cases or {cases: [...]}")


def validate_offline(golden_path: Path, corpus_path: Path, evidence_path: Path | None) -> dict[str, Any]:
    """Run all offline checks; return {problems: [...], summary: {...}}."""
    problems: list[str] = []
    raw_g = _records(golden_path)
    cases = _golden_cases(raw_g)

    # 1. minimum count
    n = len(cases)
    if n < MIN_CASES:
        problems.append(f"only {n} cases (minimum {MIN_CASES})")
    judged = [c for c in cases if not c.get("expect_no_hit")]
    no_hit = [c for c in cases if c.get("expect_no_hit")]
    if len(judged) < MIN_JUDGED:
        problems.append(f"only {len(judged)} judged cases (minimum {MIN_JUDGED})")
    if not no_hit:
        problems.append("no expect_no_hit cases present")

    # 2. category coverage
    present: set[str] = set()
    for c in cases:
        present.update(c.get("categories", []) or [])
    missing = REQUIRED_CATEGORIES - present
    if missing:
        problems.append("missing required categories: " + ", ".join(sorted(missing)))

    # 3. unique ids
    ids = [c.get("id") for c in cases]
    dup_ids = sorted({i for i in ids if ids.count(i) > 1})
    if dup_ids:
        problems.append("duplicate case ids: " + ", ".join(dup_ids))

    # 4. no duplicate (query, filters) cases
    seen: dict[str, str] = {}
    for c in cases:
        key = json.dumps({"q": c.get("query"), "f": c.get("filters", {})}, sort_keys=True)
        if key in seen:
            problems.append(f"duplicate (query, filters) between {seen[key]} and {c.get('id')}")
        seen[key] = c.get("id")
    # query-text reuse is allowed (selective filters) but reported as info
    qcounts: dict[str, int] = {}
    for c in cases:
        qcounts[c.get("query", "")] = qcounts.get(c.get("query", ""), 0) + 1
    reused = {q: n for q, n in qcounts.items() if n > 1}

    # 5. grades + rubric buckets
    grade_counts = {0: 0, 1: 0, 2: 0, 3: 0}
    bad_grades: list[str] = []
    for c in judged:
        if not c.get("expected"):
            problems.append(f"{c.get('id')}: judged case has no expected items")
            continue
        for j in c["expected"]:
            g = j.get("grade")
            if g not in VALID_GRADES:
                bad_grades.append(f"{c.get('id')}:{j.get('kind')}:{j.get('item_id')} grade={g!r}")
            else:
                grade_counts[g] += 1
    if bad_grades:
        problems.append("grades outside 0..3: " + "; ".join(bad_grades[:10]))
    # rubric must distinguish primary/strong/marginal
    if grade_counts[3] < 20:
        problems.append(f"only {grade_counts[3]} primary (grade 3) judgments; rubric under-exercised")
    if grade_counts[2] < 5:
        problems.append(f"only {grade_counts[2]} strong (grade 2) judgments; rubric under-exercised")
    if grade_counts[1] < 2:
        problems.append(f"only {grade_counts[1]} marginal (grade 1) judgments; rubric under-exercised")
    # every judged case must have >=1 relevant (grade>=1)
    for c in judged:
        if not any(j.get("grade", 0) >= 1 for j in c.get("expected", [])):
            problems.append(f"{c.get('id')}: judged case has no grade>=1 item")

    # 6. filter validity (item_ids => exactly one kind)
    for c in cases:
        f = c.get("filters") or {}
        if f.get("item_ids") is not None:
            kinds = f.get("kinds")
            if not isinstance(kinds, list) or len(kinds) != 1:
                problems.append(f"{c.get('id')}: item_ids requires exactly one kinds value (AD-1)")

    # 7. no-hit consistency
    for c in no_hit:
        if c.get("expected"):
            problems.append(f"{c.get('id')}: expect_no_hit but expected is non-empty")

    # 8. snowflakes are strings in the FILE (raw, before any coercion)
    non_str_ids: list[str] = []
    for c in cases:
        for j in c.get("expected", []) or []:
            iid = j.get("item_id")
            if not isinstance(iid, str):
                non_str_ids.append(f"{c.get('id')} expected.item_id={iid!r}")
        if c.get("filters", {}).get("item_ids") is not None:
            for iid in c["filters"]["item_ids"]:
                if not isinstance(iid, str):
                    non_str_ids.append(f"{c.get('id')} filters.item_ids={iid!r}")
    if non_str_ids:
        problems.append("non-string item ids in golden file: " + "; ".join(non_str_ids[:10]))

    # 9. snapshot integrity (if corpus provided)
    snapshot_summary: dict[str, Any] = {}
    if corpus_path and corpus_path.exists():
        raw_c = _records(corpus_path)
        snap_items = raw_c.get("items") if isinstance(raw_c, dict) else raw_c
        snap_keys = set()
        snap_non_str = 0
        for it in snap_items:
            iid = it.get("item_id")
            if not isinstance(iid, str):
                snap_non_str += 1
            snap_keys.add((normalize_kind(it.get("kind")), str(iid)))
        if snap_non_str:
            problems.append(f"{snap_non_str} non-string item ids in corpus snapshot")
        judged_keys = set()
        for c in judged:
            for j in c.get("expected", []) or []:
                judged_keys.add((normalize_kind(j.get("kind")), str(j.get("item_id"))))
        missing_in_snap = sorted(judged_keys - snap_keys)
        if missing_in_snap:
            problems.append("judged identities not in snapshot: " +
                            ", ".join(f"{k}:{i}" for k, i in missing_in_snap[:20]))
        snapshot_summary = {"n_items": len(snap_items), "n_judged_anchored": len(judged_keys & snap_keys),
                            "n_judged": len(judged_keys)}

    # 10. evidence anchor (if provided): every snapshot id known to live evidence
    evidence_summary: dict[str, Any] = {}
    if evidence_path and evidence_path.exists():
        ev = _records(evidence_path)
        ev_ids: set[tuple[str, str]] = set()
        for w in ev.get("workflows", {}).get("items", []):
            ev_ids.add(("resource", str(w.get("item_id"))))
        for d in ev.get("distillations", {}).get("items", []):
            ev_ids.add(("distillation", str(d.get("item_id"))))
        for term, hits in ev.get("messages", {}).get("map", {}).items():
            for h in hits:
                if "error" not in h and h.get("item_id"):
                    ev_ids.add(("message", str(h["item_id"])))
        if corpus_path and corpus_path.exists():
            raw_c = _records(corpus_path)
            snap_items = raw_c.get("items") if isinstance(raw_c, dict) else raw_c
            unanchored = sorted({(normalize_kind(it.get("kind")), str(it.get("item_id")))
                                 for it in snap_items
                                 if (normalize_kind(it.get("kind")), str(it.get("item_id"))) not in ev_ids})
            # distractors are intentionally allowed to be outside evidence only if flagged; otherwise flag
            real_unanchored = []
            for it in snap_items:
                k = (normalize_kind(it.get("kind")), str(it.get("item_id")))
                if k not in ev_ids and not (it.get("metadata") or {}).get("distractor"):
                    real_unanchored.append(k)
            if real_unanchored:
                problems.append("snapshot ids not anchored in live evidence: " +
                                ", ".join(f"{k}:{i}" for k, i in real_unanchored[:20]))
            evidence_summary = {"n_evidence_ids": len(ev_ids),
                                "n_snapshot_unanchored": len(unanchored),
                                "captured_at_utc": ev.get("captured_at_utc"),
                                "endpoint_ref": ev.get("endpoint_ref")}

    summary = {
        "n_cases": n,
        "n_judged": len(judged),
        "n_no_hit": len(no_hit),
        "categories_present": sorted(present),
        "categories_missing": sorted(REQUIRED_CATEGORIES - present),
        "grade_counts": {str(k): v for k, v in grade_counts.items()},
        "query_text_reuse": reused,
        "snapshot": snapshot_summary,
        "evidence": evidence_summary,
    }
    return {"problems": problems, "summary": summary}


# ---------------------------------------------------------------------------
# Opt-in live identity validation (read-only; existence checks only)
# ---------------------------------------------------------------------------


def _live_exists(kind: str, item_id: str, *, endpoint: str, anon_key: str, timeout: float) -> bool:
    """Confirm (kind, item_id) is currently in the public unified_feed."""
    ekind = normalize_kind(kind)
    feed_kind = {"resource": "workflow", "message": "message", "distillation": "distillation"}[ekind]
    # resources surface as kind=workflow/transcript/article; match by item_id only to be safe.
    params = {"select": "item_id", "item_id": f"eq.{item_id}", "limit": "1"}
    url = f"{endpoint.rstrip('/')}/unified_feed?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"apikey": anon_key, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        rows = json.loads(resp.read().decode("utf-8"))
    return bool(rows)


def validate_live(golden_path: Path, *, endpoint: str, anon_key: str, timeout: float,
                  sleep: float) -> dict[str, Any]:
    """Re-validate every distinct judged identity against the live corpus now."""
    cases = _golden_cases(_records(golden_path))
    identities: set[tuple[str, str]] = set()
    for c in cases:
        for j in c.get("expected", []) or []:
            identities.add((j.get("kind"), str(j.get("item_id"))))
    ok, missing, errors = 0, [], []
    for kind, item_id in sorted(identities):
        try:
            exists = _live_exists(kind, item_id, endpoint=endpoint, anon_key=anon_key, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{kind}:{item_id} -> {type(exc).__name__}")
            if sleep:
                time.sleep(sleep)
            continue
        if exists:
            ok += 1
        else:
            missing.append(f"{kind}:{item_id}")
        if sleep:
            time.sleep(sleep)
    return {"n_identities": len(identities), "n_ok": ok, "missing": missing,
            "errors": errors, "endpoint_ref": _endpoint_ref(endpoint)}


def _endpoint_ref(endpoint: str) -> str | None:
    import re

    m = re.search(r"https://([a-z0-9]{20})\.supabase\.co", endpoint)
    return m.group(1) if m else None


def network_enabled() -> bool:
    return os.environ.get("HIVEMIND_EVAL_NETWORK") in ("1", "true", "True")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Validate the Hivemind golden set (offline + opt-in live).")
    p.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    p.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    p.add_argument("--live", action="store_true", help="also re-validate judged ids against the live corpus")
    p.add_argument("--json", type=Path, help="write the validation report to this path")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--sleep", type=float, default=0.2)
    args = p.parse_args(argv)

    # Load through the task-0.5 schema/loader too (proves reportability + catches schema errors).
    try:
        load_corpus(args.corpus)
        load_golden_set(args.golden)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: schema/loader rejected the set: {exc}", file=sys.stderr)
        return 2

    result = validate_offline(args.golden, args.corpus, args.evidence if args.evidence.exists() else None)
    report: dict[str, Any] = {"offline": result}
    if args.live or network_enabled():
        report["live"] = validate_live(args.golden, endpoint=resolve_endpoint(), anon_key=resolve_anon_key(),
                                       timeout=args.timeout, sleep=args.sleep)
        if report["live"]["missing"] or report["live"]["errors"]:
            result["problems"].append("live identity check found missing/errored identities (see live)")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    s = result["summary"]
    print(redact(f"golden validation: {s['n_cases']} cases ({s['n_judged']} judged, {s['n_no_hit']} no-hit)"))
    print(redact(f"  grades: {s['grade_counts']}"))
    print(redact(f"  categories missing: {s['categories_missing'] or 'none'}"))
    if s.get("snapshot"):
        print(redact(f"  snapshot: {s['snapshot']}"))
    if s.get("evidence"):
        print(redact(f"  evidence anchor: {s['evidence']}"))
    if report.get("live"):
        lv = report["live"]
        print(redact(f"  LIVE: {lv['n_ok']}/{lv['n_identities']} identities present at {lv['endpoint_ref']}"))
        if lv["missing"] or lv["errors"]:
            print(redact(f"  LIVE missing/errors: {lv['missing'][:10]} {lv['errors'][:10]}"))

    if result["problems"]:
        print("\nPROBLEMS:", file=sys.stderr)
        for prob in result["problems"]:
            print(redact(f"  - {prob}"), file=sys.stderr)
        return 2
    print("OK: no problems.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
