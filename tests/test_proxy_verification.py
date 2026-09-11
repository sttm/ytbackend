import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ["PRODUCERSCENTER_BACKEND_DATABASE_URL"] = "sqlite:///:memory:"

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app import database
from app.models import Proxy
from app.services.proxy_checker import check_proxy_fast
from app.services.proxy_store import apply_check_result, best_proxies


class FastCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_ping_is_not_audio_verification(self):
        with patch("app.services.proxy_checker._http_get", new=AsyncMock(return_value=(True, 50, "HTTP 204"))):
            result = await check_proxy_fast("http://proxy.test:8080")
        self.assertEqual(result["status"], "reachable")


class ProxyVerificationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Proxy.__table__.create(self.engine)
        self.db = Session(self.engine)
        self.proxy = Proxy(proxy_url="http://proxy.test:8080")
        self.db.add(self.proxy)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_only_real_audio_check_admits_proxy_and_timeout_removes_it(self):
        apply_check_result(self.db, self.proxy, {"status": "reachable", "layer": "fast-ping"})
        self.assertEqual(best_proxies(self.db), [])
        self.assertEqual(self.proxy.fail_count, 0)
        apply_check_result(self.db, self.proxy, {"status": "verified", "layer": "audio-byte", "download_ms": 100})
        self.assertEqual(best_proxies(self.db), [self.proxy])
        apply_check_result(self.db, self.proxy, {"status": "timeout"})
        self.assertEqual(best_proxies(self.db), [])
        self.assertEqual(self.proxy.timeout_count, 1)
        self.assertIsNotNone(self.proxy.cooldown_until)

    def test_legacy_verified_rows_require_recheck(self):
        self.proxy.is_verified = True
        self.proxy.status = "verified"
        self.db.commit()
        self.assertEqual(best_proxies(self.db), [])

    def test_url_resolution_cannot_claim_audio_verification(self):
        apply_check_result(self.db, self.proxy, {"status": "verified", "download_ms": 100})
        self.assertFalse(self.proxy.is_verified)
        self.assertEqual(best_proxies(self.db), [])

    def test_schema_upgrade_is_repeatable_and_preserves_existing_rows(self):
        legacy = create_engine("sqlite:///:memory:")
        try:
            with legacy.begin() as connection:
                connection.execute(text("CREATE TABLE proxies (id INTEGER PRIMARY KEY)"))
                connection.execute(text("CREATE TABLE stream_cache (id INTEGER PRIMARY KEY)"))
                connection.execute(text("INSERT INTO proxies (id) VALUES (1)"))
            with patch.object(database, "engine", legacy):
                database.ensure_schema()
                database.ensure_schema()
            self.assertIn("audio_verified_at", {column["name"] for column in inspect(legacy).get_columns("proxies")})
            with legacy.connect() as connection:
                self.assertEqual(connection.execute(text("SELECT id, audio_verified_at FROM proxies")).one(), (1, None))
        finally:
            legacy.dispose()
