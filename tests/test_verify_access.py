"""Tests for ``scripts/verify_access.py`` — the Hivemind access probe.

Pure and offline: no network, no CLI, no secrets are printed. These tests pin
the redactor so it can never let a credential through to the audit report, and
they exercise the offline helpers (executor ref, linked-ref, dry-run parsing).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

import verify_access as va  # noqa: E402


# ---------------------------------------------------------------------------
# Redactor — the safety boundary; must mask every known secret shape
# ---------------------------------------------------------------------------


class RedactTests(unittest.TestCase):
    """Every secret shape must be fully masked by ``redact``."""

    def _assert_masked(self, secret: str) -> None:
        out = va.redact(f"prefix {secret} suffix")
        self.assertNotIn(secret, out, f"secret leaked through redact: {secret!r}")
        # And the masked form must carry a placeholder, not the raw value.
        self.assertTrue(any(tok in out for tok in ("<redacted>", "<connstr>", "<token>", "<sb-key>", "<contributor-key>", "<jwt>")))

    def test_masks_supabase_publishable_key(self):
        self._assert_masked("sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx")

    def test_masks_supabase_secret_key(self):
        self._assert_masked("sb_secret_abcdEfghIjklMnopqrstuvwxYZ0123456789")

    def test_masks_jwt(self):
        self._assert_masked("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SflKxwRJSMeKKF2QT4f")

    def test_masks_contributor_key(self):
        self._assert_masked("hm_" + "a" * 64)

    def test_masks_connection_string_with_password(self):
        self._assert_masked(
            "postgresql://postgres.ujlwuvkrxlvoswwkerdf:hunter2@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
        )

    def test_masks_pgpassword_assignment(self):
        self._assert_masked('PGPASSWORD="gLunEFoVilWmLxhBiQMyjjYdNU6rkcqz"')

    def test_masks_pgpassword_bare(self):
        self._assert_masked("PGPASSWORD=gLunEFoVilWmLxhBiQMyjjYdNU6rkcqz")

    def test_masks_export_line(self):
        line = 'export PGPASSWORD="gLunEFoVilWmLxhBiQMyjjYdNU6rkcqz"'
        out = va.redact(line)
        self.assertIn("<redacted>", out)
        self.assertNotIn("gLunEFoVilWmLxhBiQMyjjYdNU6rkcqz", out)

    def test_masks_32char_token(self):
        # Shape of the CLI login password observed in the dry-run script.
        self._assert_masked("gLunEFoVilWmLxhBiQMyjjYdNU6rkcqz")

    def test_masks_hex_digest(self):
        self._assert_masked("a" * 64)  # sha256-style

    def test_preserves_safe_identifiers(self):
        """Safe project identifiers, ports, function slugs survive redaction."""
        out = va.redact(
            "linked project ujlwuvkrxlvoswwkerdf region eu-central-1 "
            "function contribute port 5432 pg_trgm"
        )
        # 20-char refs are below the 32-char token threshold -> preserved.
        self.assertIn("ujlwuvkrxlvoswwkerdf", out)
        self.assertIn("contribute", out)
        self.assertIn("5432", out)
        self.assertIn("pg_trgm", out)

    def test_redacts_multiline_block(self):
        block = (
            "Using workdir\n"
            'export PGHOST="aws-0-eu-central-1.pooler.supabase.com"\n'
            'export PGPORT="5432"\n'
            'export PGUSER="cli_login_postgres.ujlwuvkrxlvoswwkerdf"\n'
            'export PGPASSWORD="gLunEFoVilWmLxhBiQMyjjYdNU6rkcqz"\n'
            "tail\n"
        )
        out = va.redact(block)
        self.assertNotIn("gLunEFoVilWmLxhBiQMyjjYdNU6rkcqz", out)
        self.assertIn("<redacted>", out)


# ---------------------------------------------------------------------------
# Offline helpers
# ---------------------------------------------------------------------------


class ExecutorRefTests(unittest.TestCase):
    def test_executor_ref_is_20_chars(self):
        ref = va.executor_ref()
        self.assertIsNotNone(ref)
        assert ref is not None  # for type checkers
        self.assertEqual(len(ref), 20)
        self.assertTrue(ref.isalnum())

    def test_executor_ref_matches_linked_temp_ref(self):
        """The baked-in executor ref must match supabase/.temp/project-ref."""
        linked = va.read_linked_ref()
        self.assertIsNotNone(linked, "supabase/.temp/project-ref missing")
        self.assertEqual(linked, va.executor_ref())


class DryRunParseTests(unittest.TestCase):
    def test_extracts_only_pg_env(self):
        script = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'export PGHOST="aws-0-eu-central-1.pooler.supabase.com"\n'
            'export PGPORT="5432"\n'
            'export PGUSER="cli_login_postgres.ujlwuvkrxlvoswwkerdf"\n'
            'export PGPASSWORD="gLunEFoVilWmLxhBiQMyjjYdNU6rkcqz"\n'
            'export PGDATABASE="postgres"\n'
            'export PATH="/usr/local/bin:$PATH"\n'
            "pg_dump --schema-only\n"
        )
        env = va.parse_dryrun_pg_env(script)
        self.assertEqual(env["PGHOST"], "aws-0-eu-central-1.pooler.supabase.com")
        self.assertEqual(env["PGPORT"], "5432")
        self.assertEqual(env["PGDATABASE"], "postgres")
        self.assertTrue(env["PGUSER"].startswith("cli_login_postgres."))
        self.assertEqual(env["PGPASSWORD"], "gLunEFoVilWmLxhBiQMyjjYdNU6rkcqz")
        # Non-PG exports are ignored.
        self.assertNotIn("PATH", env)

    def test_empty_script_returns_empty(self):
        self.assertEqual(va.parse_dryrun_pg_env(""), {})


if __name__ == "__main__":
    unittest.main()
