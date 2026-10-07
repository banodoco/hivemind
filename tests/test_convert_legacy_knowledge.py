from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import convert_legacy_knowledge as converter  # noqa: E402


class ProductionGuardTests(unittest.TestCase):
    REF = converter.PRODUCTION_PROJECT_REF
    DIRECT = f"postgresql://postgres:secret@db.{REF}.supabase.co:5432/postgres"
    POOLER = f"postgresql://postgres.{REF}:secret@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"

    def test_default_guard_still_rejects_remote_targets(self):
        with self.assertRaisesRegex(ValueError, "non-disposable"):
            converter.checked_url(self.DIRECT)

    def test_explicit_ref_accepts_matching_direct_host(self):
        self.assertEqual(converter.checked_url(self.DIRECT, production_project_ref=self.REF), self.DIRECT)

    def test_explicit_ref_accepts_matching_pooler_identity(self):
        self.assertEqual(converter.checked_url(self.POOLER, production_project_ref=self.REF), self.POOLER)

    def test_explicit_ref_rejects_wrong_project_or_host(self):
        with self.assertRaisesRegex(ValueError, "unknown production"):
            converter.checked_url(self.DIRECT, production_project_ref="aaaaaaaaaaaaaaaaaaaa")
        wrong_host = self.POOLER.replace("pooler.supabase.com", "example.com")
        with self.assertRaisesRegex(ValueError, "does not match"):
            converter.checked_url(wrong_host, production_project_ref=self.REF)

    def test_production_apply_requires_a_backup_path(self):
        with mock.patch.dict(os.environ, {"HIVEMIND_TEST_DATABASE_URL": self.POOLER}, clear=False):
            with self.assertRaises(SystemExit) as raised:
                converter.main(["--apply", "--production-project-ref", self.REF])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
