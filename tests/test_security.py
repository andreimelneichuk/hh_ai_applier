import os
import sys
import unittest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.app import app
from src.api.security import API_TOKEN, TOKEN_HEADER
from src.clients.browser import HHBrowserClient, BrowserBusyError


class TestApiSecurity(unittest.TestCase):
    """Локальный API не должен быть доступен сторонним сайтам."""

    def test_api_requires_token(self):
        client = TestClient(app, base_url="http://127.0.0.1")
        self.assertEqual(client.get("/api/settings").status_code, 403)
        self.assertEqual(client.get("/api/settings", headers={TOKEN_HEADER: "wrong"}).status_code, 403)
        self.assertEqual(client.get("/api/openai-presets", headers={TOKEN_HEADER: API_TOKEN}).status_code, 200)

    def test_foreign_host_rejected(self):
        """DNS rebinding: чужой домен, указывающий на 127.0.0.1, не получает ни страницу, ни API."""
        client = TestClient(app, base_url="http://evil.example.com")
        self.assertEqual(client.get("/").status_code, 403)
        self.assertEqual(client.get("/api/settings", headers={TOKEN_HEADER: API_TOKEN}).status_code, 403)

    def test_index_contains_token_and_no_cors(self):
        client = TestClient(app, base_url="http://localhost:8000")
        res = client.get("/", headers={"Origin": "https://evil.example.com"})
        self.assertEqual(res.status_code, 200)
        self.assertIn(API_TOKEN, res.text)
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in res.headers.keys()})


class TestBrowserProfileLock(unittest.TestCase):
    """Второй клиент не может открыть профиль Chrome, пока им владеет первый."""

    def test_second_client_gets_busy_error(self):
        first = HHBrowserClient()
        second = HHBrowserClient()
        second.PROFILE_WAIT_TIMEOUT = 0.1
        first._acquire_profile()
        try:
            self.assertTrue(HHBrowserClient.is_busy())
            with self.assertRaises(BrowserBusyError):
                second.start()
            self.assertFalse(second._owns_profile)
        finally:
            first.stop()
        self.assertFalse(HHBrowserClient.is_busy())


if __name__ == "__main__":
    unittest.main()
