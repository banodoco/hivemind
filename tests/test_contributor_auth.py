"""Focused offline coverage for the shared contributor-auth seams."""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]

from cli import main as auth_main  # noqa: E402
from executors._common import (  # noqa: E402
    contributor_key_path,
    delete_local_contributor_key,
    hash_contributor_key,
    resolve_contributor_key,
    write_contributor_key,
)


KEY = "hm_" + "a" * 64


class KeyTests(unittest.TestCase):
    def test_hash_matches_sql_storage_contract_and_rejects_bad_keys(self):
        self.assertEqual(
            hash_contributor_key(KEY),
            "03345d29843e607f4b3f55d4d16c92ae18cefae19719d42e58a74f121468ff4d",
        )
        with self.assertRaises(ValueError):
            hash_contributor_key("hm_not-a-key")

    def test_key_file_is_owner_only_and_environment_wins(self):
        with tempfile.TemporaryDirectory() as home:
            path = write_contributor_key(KEY, home=home)
            self.assertEqual(path, contributor_key_path(home))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
            with mock.patch.dict(os.environ, {"HOME": home, "HIVEMIND_CONTRIBUTOR_KEY": "hm_env"}, clear=True):
                self.assertEqual(resolve_contributor_key(), "hm_env")
            with mock.patch.dict(os.environ, {"HOME": home}, clear=True):
                self.assertEqual(resolve_contributor_key(), KEY)
            self.assertTrue(delete_local_contributor_key(home=home))
            self.assertFalse(Path(path).exists())


class CliTests(unittest.TestCase):
    def test_status_is_redacted_and_uses_shared_resolver(self):
        with mock.patch("cli.resolve_contributor_key", return_value=KEY), \
             mock.patch("cli.auth_post", return_value={"status": "active", "key_id": "id-1"}), \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(auth_main(["auth", "status"]), 0)
        rendered = output.getvalue()
        self.assertNotIn(KEY, rendered)
        self.assertEqual(json.loads(rendered), {"key_id": "id-1", "source": "file", "status": "active"})

    def test_logout_only_removes_local_key(self):
        with mock.patch("cli.delete_local_contributor_key", return_value=True) as delete, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(auth_main(["auth", "logout"]), 0)
        delete.assert_called_once_with()
        self.assertEqual(json.loads(output.getvalue())["local_key_removed"], True)

    def test_login_prints_url_but_never_poll_secret_or_redeemed_key(self):
        redeemed = KEY
        responses = [
            {"approval_url": "https://www.banodoco.ai/connect/?request=opaque&code=ABCD1234"},
            {"status": "approved"},
            {"status": "redeemed", "key": redeemed},
        ]
        with mock.patch("cli.auth_post", side_effect=responses) as post, \
             mock.patch("cli.webbrowser.open", return_value=True), \
             mock.patch("cli.write_contributor_key", return_value="/tmp/.hivemind/key") as write, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(auth_main(["auth", "login", "--ttl", "60", "--interval", "0", "--timeout", "1"]), 0)
        rendered = output.getvalue()
        self.assertIn("https://www.banodoco.ai/connect/", rendered)
        self.assertNotIn(redeemed, rendered)
        create_payload = post.call_args_list[0].args[0]
        self.assertNotIn(create_payload["poll_secret"], rendered)
        self.assertNotIn(create_payload["poll_secret"], create_payload.get("approval_url", ""))
        write.assert_called_once_with(redeemed)
        self.assertEqual(post.call_count, 3)
        for call in post.call_args_list:
            self.assertNotIn(redeemed, json.dumps(call.args[0]))
            if call.args[0].get("action") != "create":
                self.assertNotIn("poll_secret", call.args[0].get("approval_url", ""))

    def test_login_attempts_redacted_cleanup_after_local_persistence_failure(self):
        poll_secret = "poll-secret-that-must-not-be-printed"
        responses = [
            {"approval_url": "https://www.banodoco.ai/connect/?request=opaque&approval_code=ABCD1234"},
            {"status": "approved"},
            {"status": "redeemed", "key": KEY},
            {"status": "cleaned_up", "revoked": True, "key_id": "key-id"},
        ]
        with mock.patch("cli.auth_post", side_effect=responses) as post, \
             mock.patch("cli.webbrowser.open", return_value=True), \
             mock.patch("cli.write_contributor_key", side_effect=OSError("disk full")), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(auth_main(["auth", "login", "--interval", "0", "--timeout", "1"]), 1)
        rendered = output.getvalue()
        self.assertNotIn(KEY, rendered)
        self.assertNotIn(poll_secret, rendered)
        result = json.loads(rendered.splitlines()[-1])
        self.assertEqual(result["cleanup"], "revoked_issued_key")
        self.assertIn("retry login", result["guidance"])
        self.assertEqual(post.call_args_list[-1].args[0]["action"], "cleanup")


class MigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = (REPO / "schema" / "040_contributor_auth.sql").read_text(encoding="utf-8")

    def test_migration_has_direct_fk_unique_and_explicit_transition(self):
        self.assertIn("auth_user_id uuid", self.sql)
        self.assertIn("references auth.users(id)", self.sql)
        self.assertIn("contributors_auth_user_id_key unique (auth_user_id)", self.sql)
        self.assertIn("auth_user_id_migration_pending", self.sql)
        self.assertIn("new contributors require auth.users identity", self.sql)

    def test_lookup_and_broker_are_hash_only_and_race_safe(self):
        self.assertIn("from public.contributor_keys", self.sql)
        self.assertIn("hivemind_secret_hash(p_key)", self.sql)
        self.assertNotIn("where api_key_hash =", self.sql)
        self.assertGreaterEqual(self.sql.count("for update"), 3)
        self.assertIn("consumed_at is not null", self.sql)
        self.assertIn("set consumed_at = now()", self.sql)
        self.assertIn("contributor_keys_hash_key unique", self.sql)

    def test_unlinked_legacy_keys_are_auditable_but_not_write_credentials(self):
        self.assertIn("issued_key_id uuid", self.sql)
        self.assertIn("hivemind_auth_cleanup_request(text,text)", self.sql)
        self.assertIn("when r.auth_user_id is null then 'claim_pending'", self.sql)
        self.assertIn("and c.auth_user_id is not null", self.sql)


class EdgeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.edge = (REPO / "supabase" / "functions" / "contributor-auth" / "index.ts").read_text(encoding="utf-8")
        cls.protocol = (REPO / "supabase" / "functions" / "contributor-auth" / "protocol.ts").read_text(encoding="utf-8")

    def test_edge_has_exact_cors_preflight_and_json_response_path(self):
        self.assertIn('"https://www.banodoco.ai"', self.protocol)
        self.assertIn('request.method === "OPTIONS"', self.edge)
        self.assertIn('"access-control-allow-origin"', self.edge)
        self.assertIn('"content-type": "application/json; charset=utf-8"', self.edge)
        self.assertNotIn('access-control-allow-origin", "*"', self.edge)

    def test_edge_requires_non_anonymous_discord_identity_without_members_gate(self):
        self.assertIn("verifiedDiscordUserId", self.edge)
        self.assertIn("is_anonymous === true", self.protocol)
        self.assertIn('identity.provider === "discord"', self.protocol)
        self.assertNotIn("members", self.edge)


if __name__ == "__main__":
    unittest.main()
