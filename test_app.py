import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import create_app, debug_enabled


class WalletApiTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.folder.name, "test.db")
        self.app = create_app(self.database, rate_limit="1000 per minute")
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.folder.cleanup()

    def create_account(self, owner, balance):
        response = self.client.post(
            "/accounts",
            json={"owner": owner, "initial_balance": balance},
        )
        self.assertEqual(response.status_code, 201)
        return response.get_json()

    def test_replay_does_not_wait_for_write_lock(self):
        sender = self.create_account("Alice", 1000)
        receiver = self.create_account("Bob", 0)
        data = {
            "from_account": sender["id"],
            "to_account": receiver["id"],
            "amount": 250,
        }
        headers = {"Idempotency-Key": "payment-1"}

        created = self.client.post("/transfers", json=data, headers=headers)
        self.assertEqual(created.status_code, 201)

        lock_db = sqlite3.connect(self.database, timeout=1)
        lock_db.execute("BEGIN IMMEDIATE")
        try:
            replayed = self.client.post("/transfers", json=data, headers=headers)
        finally:
            lock_db.rollback()
            lock_db.close()

        self.assertEqual(replayed.status_code, 200)
        self.assertEqual(replayed.headers["Idempotent-Replayed"], "true")
        self.assertEqual(replayed.get_json()["id"], created.get_json()["id"])

    def test_request_size_and_rate_limits(self):
        too_large = self.client.post(
            "/accounts",
            data=b"x" * (16 * 1024 + 1),
            content_type="application/json",
        )
        self.assertEqual(too_large.status_code, 413)

        limited_app = create_app(
            os.path.join(self.folder.name, "limited.db"),
            rate_limit="2 per minute",
        )
        limited_app.config["TESTING"] = True
        client = limited_app.test_client()

        self.assertEqual(client.get("/accounts/9999").status_code, 404)
        self.assertEqual(client.get("/accounts/9999").status_code, 404)
        response = client.get("/accounts/9999")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.get_json()["error"], "Rate limit exceeded")

    def test_debug_flag_comes_from_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(debug_enabled())
        with patch.dict(os.environ, {"FLASK_DEBUG": "1"}, clear=True):
            self.assertTrue(debug_enabled())
        with patch.dict(os.environ, {"FLASK_DEBUG": "true"}, clear=True):
            self.assertTrue(debug_enabled())

if __name__ == "__main__":
    unittest.main()