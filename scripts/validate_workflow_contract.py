#!/usr/bin/env python3
"""Validate the frozen workflow representation contract (plan task 0.8).

Offline, pure, deterministic. This script does NOT touch the database, does NOT
call an embedding provider, does NOT import VibeComfy, does NOT execute stored
workflow Python, and does NOT read source corpus content. It checks:

1. The machine-readable contract JSON is well-formed and internally consistent
   (cohort counts, gate shapes, deferred/task mapping).
2. The JSON matches the frozen reference module
   ``executors/workflow_representation.py`` (versions, delimiters, reason codes)
   and that the rejected trap reason ``high_entropy_base64`` is absent.
3. The reference module behaves per the contract on representative fixtures:
   precedence (payload/both/body/recoverable/unavailable), no-duplication, the
   secret-scanner trap fix (generated VibeComfy code scans clean while planted
   secrets fail closed with correct reason codes), and chunk determinism with the
   no-silent-truncation guard on the measured worst-case Python size.

Reproduce:
    python3 scripts/validate_workflow_contract.py
    python3 scripts/validate_workflow_contract.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from executors import workflow_representation as wr  # noqa: E402

CONTRACT_PATH = REPO_ROOT / "docs" / "hybrid-search" / "phase0-workflow-representation-contract.json"


# ---------------------------------------------------------------------------
# Check harness
# ---------------------------------------------------------------------------

class Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.ok = True

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed:
            self.ok = False

    def summary(self) -> dict[str, Any]:
        passed = sum(1 for c in self.checks if c["passed"])
        return {
            "ok": self.ok,
            "total": len(self.checks),
            "passed": passed,
            "failed": len(self.checks) - passed,
            "checks": self.checks,
        }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Representative VibeComfy-generated source: large base64/JSON literals, CamelCase
# node classes, model filenames, kwargs — NO credential context. This is the exact
# shape that caused task-0.3's high_entropy_base64 false positive on all 222 rows.
GENERATED_VIBECOMFY = (
    "# vibecomfy: generated scratchpad\n"
    '"""Auto-generated VibeComfy scratchpad."""\n'
    "from vibecomfy import workflow as _w\n\n"
    'sampler = WanVideoSampler(model="wan2.2_animate_14B_bf16.safetensors",\n'
    '    lora=("detailer/ltx-2-19b-ic-lora-detailer.safetensors", 0.8))\n\n'
    '# Large generated literal (NOT a credential — no keyword context):\n'
    'blob = "eJxrYK5mn6egoaOjaGZqYWBmZmBgamKQnJqSpK2RkZPpkJ6ekZYSkA==" * 200\n'
    'workflow_json = {"nodes": [{"class_type": "BerniniConditioning"}]}\n'
)

# One fixture per reason code, value-bearing; used to prove fail-closed behavior.
PLANTED_SECRETS: list[tuple[str, str]] = [
    ("private_key_block", "k = 'x'\n-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----\n"),
    ("provider_token_prefix", 'openai = "sk-' + "Aa0" * 20 + '"\n'),
    ("provider_token_prefix", 'gh = "ghp_' + "a" * 36 + '"\n'),
    ("provider_token_prefix", 'a = "AKIAIOSFODNN7EXAMPLE"\n'),
    ("provider_token_prefix", 'h = "hm_' + "0" * 64 + '"\n'),
    ("credential_assignment", 'client_secret = "9xH7vQ2pZ4rT1wY8nK3mB6cD5eF0aLm"\n'),
    ("credential_url", 'engine = "postgresql://u:S3cretPassw0rd@db.example.com:5432/p"\n'),
    ("aws_env_var", "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"),
]


# ---------------------------------------------------------------------------
# 1. Contract JSON structural checks
# ---------------------------------------------------------------------------

def check_contract_structure(report: Report, contract: dict[str, Any]) -> None:
    report.check("schema is workflow-representation-contract",
                 contract.get("schema") == "hivemind/workflow-representation-contract")
    report.check("status is frozen", contract.get("status") == "frozen")
    report.check("evidence_date present", bool(contract.get("evidence_date")))
    report.check("decision_record path present", bool(contract.get("decision_record")))

    v = contract.get("versions", {})
    report.check("versions match reference module",
                 v.get("canonicalization") == wr.CANONICALIZATION_VERSION
                 and v.get("delimiter_grammar") == wr.DELIMITER_GRAMMAR_VERSION
                 and v.get("secret_scan") == wr.SECRET_SCAN_VERSION
                 and v.get("chunking") == wr.CHUNKING_VERSION
                 and v.get("semantics_projection") == wr.SEMANTICS_PROJECTION_VERSION)

    delims = tuple(contract.get("delimiters", {}).get("recognized", []))
    report.check("delimiters match reference module", delims == wr.BODY_DELIMITERS,
                 f"json={delims} module={wr.BODY_DELIMITERS}")

    codes = set(contract.get("secret_scanner", {}).get("reason_codes", []))
    report.check("reason codes match reference module", codes == set(wr.SECRET_REASON_CODES))
    # The trap-fix prose may NAME the rejected code to explain why it is absent;
    # the invariant is that it is not an actionable reason code.
    report.check("high_entropy_base64 is not an actionable reason code",
                 "high_entropy_base64" not in codes)

    cohorts = contract.get("cohorts", {})
    counts = cohorts.get("counts_2026_07_28", {})
    total = counts.get("total_workflows")
    observed_sum = (counts.get("payload_python_including_both", 0)
                    + counts.get("body_python_only", 0)
                    + counts.get("neither_recoverable_or_unavailable", 0))
    report.check("cohort counts sum to total (observable cohorts)",
                 total == observed_sum == 2757,
                 f"total={total} observed_sum={observed_sum}")
    report.check("both_duplicate equals payload cohort",
                 counts.get("both_duplicate") == counts.get("payload_python_including_both") == 222)

    # Golden set counts.
    g = contract.get("golden_set", {})
    report.check("golden set has 112 cases", g.get("n_cases") == 112)
    report.check("golden categories all 23 families present", g.get("categories_present") == 23)

    # Gates: every gate has the required keys and is post-hoc locked.
    gates = contract.get("gates", {})
    report.check("gates are post-hoc locked", gates.get("post_hoc_locked") is True)
    required_keys = {"id", "metric", "comparator", "threshold"}
    all_well_formed = True
    for family in ("quality", "security", "coverage", "capacity_cost"):
        for gate in gates.get(family, []):
            if not required_keys.issubset(gate):
                all_well_formed = False
    report.check("all gates well-formed", all_well_formed)

    # Rollback invariants present.
    rb = contract.get("rollback", {})
    report.check("rollback invariants include additive-only + source-untouched",
                 "schema additive only" in rb.get("invariants", [])
                 and any("never modified or deleted" in s for s in rb.get("invariants", [])))
    for stage in ("lexical", "embedding_pilot", "workflow_remediation", "hnsw", "edge", "canary", "production_backfill"):
        report.check(f"rollback stage defined: {stage}", stage in rb.get("stages", {}))


# ---------------------------------------------------------------------------
# 2. Behavioural checks via the reference module
# ---------------------------------------------------------------------------

def check_precedence(report: Report) -> None:
    both = {
        "body": "desc\n\nPython scratchpad source:\nx = 1\n",
        "payload": {"python_source": "x = 1\n"},
    }
    res = wr.resolve_authoritative_python(both)
    report.check("precedence: payload wins on both-cohort", res.cohort == wr.COHORT_PAYLOAD_PYTHON and res.source.strip() == "x = 1")
    report.check("precedence: both-cohort flagged body_duplicate", res.body_duplicate is True)

    body_only = {"body": "d\n\nPython ready-template source:\ny = 2\n", "payload": {}}
    res = wr.resolve_authoritative_python(body_only)
    report.check("precedence: body_python when no payload", res.cohort == wr.COHORT_BODY_PYTHON and res.source.strip() == "y = 2")

    recoverable = {"body": "d", "payload": {"corpus_path": "x.json"}}
    res = wr.resolve_authoritative_python(recoverable, recoverer=lambda r: "# vibecomfy: generated scratchpad\nz=9\n")
    report.check("precedence: recoverable via injected recoverer", res.cohort == wr.COHORT_RECOVERABLE and res.available)

    unavailable = {"body": "d", "payload": {}}
    res = wr.resolve_authoritative_python(unavailable)
    report.check("precedence: unavailable when nothing resolves", res.cohort == wr.COHORT_UNAVAILABLE and not res.available)


def check_no_duplication(report: Report) -> None:
    row = {
        "title": "T",
        "body": "Prose here.\n\nPython scratchpad source:\nx = 1",
        "payload": {"python_source": "x = 1\n"},
        "metadata": {"workflow_semantics": {"media_type": "video"}},
    }
    prose = wr.build_workflow_prose(row)
    python = wr.build_workflow_python(row)
    report.check("no-duplication: python absent from prose", "x = 1" not in prose and "scratchpad" not in prose)
    report.check("no-duplication: python present once as workflow_python", python.strip() == "x = 1")
    report.check("no-duplication: prose carries title + semantics", "T" in prose and "video" in prose)


def check_scanner_trap_fix(report: Report) -> None:
    findings = wr.scan_secrets(GENERATED_VIBECOMFY)
    report.check("scanner trap-fix: generated VibeComfy code scans clean (0 findings)",
                 findings == [], f"unexpected findings: {findings}")


def check_scanner_fail_closed(report: Report) -> None:
    for expected, source in PLANTED_SECRETS:
        reasons = {f.reason_code for f in wr.scan_secrets(source)}
        report.check(f"scanner fail-closed: {expected}", expected in reasons, f"reasons={reasons}")


def check_scanner_no_leakage(report: Report) -> None:
    source = 'token = "sk-' + "A" * 50 + '"\n'
    for f in wr.scan_secrets(source):
        blob = json.dumps({"reason": f.reason_code, "start": f.start, "length": f.length, "detail": f.detail_kind})
        report.check("scanner: finding object carries no secret value", "sk-" not in blob)


def check_chunking(report: Report) -> None:
    text = "\n\n".join(f"paragraph {i} about wan video." for i in range(40))
    a = [c.text for c in wr.chunk_prose(text, target_tokens=64, overlap_tokens=10)]
    b = [c.text for c in wr.chunk_prose(text, target_tokens=64, overlap_tokens=10)]
    report.check("chunking: deterministic across runs", a == b)
    report.check("chunking: two configs produce different counts",
                 len(wr.chunk_prose(text, target_tokens=64, overlap_tokens=8))
                 > len(wr.chunk_prose(text, target_tokens=256, overlap_tokens=16)))

    # No silent truncation on the measured worst-case Python size (~1.4M chars).
    huge = 'workflow_json = "' + ("A" * 1_400_000) + '"\n'
    chunks = wr.chunk_python(huge, target_tokens=512, overlap_tokens=50)
    report.check("chunking: worst-case size chunked into >1 chunk", len(chunks) > 1)
    report.check("chunking: no silent truncation (coverage_ok)", wr.coverage_ok(huge, chunks))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", default=str(CONTRACT_PATH), help="Path to the contract JSON")
    parser.add_argument("--json", dest="out_json", help="Write a machine-readable report to this path")
    parser.add_argument("--quiet", action="store_true", help="Suppress the human summary")
    args = parser.parse_args(argv)

    contract_path = Path(args.contract)
    if not contract_path.exists():
        print(f"ERROR: contract not found at {contract_path}", file=sys.stderr)
        return 2
    contract = json.loads(contract_path.read_text())

    report = Report()
    check_contract_structure(report, contract)
    check_precedence(report)
    check_no_duplication(report)
    check_scanner_trap_fix(report)
    check_scanner_fail_closed(report)
    check_scanner_no_leakage(report)
    check_chunking(report)

    summary = report.summary()

    if args.out_json:
        out = {
            "schema": "hivemind/workflow-representation-contract-validation",
            "contract": str(contract_path),
            "reference_module": "executors/workflow_representation.py",
            **summary,
        }
        Path(args.out_json).write_text(json.dumps(out, indent=2, sort_keys=True))

    if not args.quiet:
        for c in summary["checks"]:
            mark = "OK  " if c["passed"] else "FAIL"
            print(f"{mark} {c['name']}" + (f"  ({c['detail']})" if c["detail"] else ""))
        print(f"\n{summary['passed']}/{summary['total']} checks passed; "
              f"{'PASS' if summary['ok'] else 'FAIL'}")

    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
