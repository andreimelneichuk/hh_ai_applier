import os
import sys
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.db.database as database
import src.api.state as state
from src.api.app import app
from src.api.security import API_TOKEN, TOKEN_HEADER

TEST_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_settings_consistency_db.db")

BASE_SETTINGS = {
    "queries": ["Python"],
    "area_id": "113",
    "threshold": 70,
    "resume_id": "all",
    "dry_run": True,
    "openai_provider_preset": "groq",
}


class TestSettingsKeysConsistency(unittest.TestCase):
    """Ключи провайдеров не должны перезаписываться устаревшими значениями из основной формы."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app, base_url="http://127.0.0.1", headers={TOKEN_HEADER: API_TOKEN})

    def setUp(self):
        database.DB_PATH = TEST_DB_PATH
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        database.init_db()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)

    def test_settings_without_keys_keep_provider_keys(self):
        self.client.post("/api/providers/gemini", json={"api_keys": ["gem_new_1", "gem_new_2"]})
        res = self.client.post("/api/settings", json=BASE_SETTINGS)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(database.get_provider_config("gemini")["api_keys"], ["gem_new_1", "gem_new_2"])
        self.assertEqual(self.client.get("/api/settings").json()["gemini_api_keys"],
                         ",".join(database.mask_api_key(k) for k in ["gem_new_1", "gem_new_2"]))

    def test_openai_keys_belong_to_selected_preset(self):
        """Сохранение ключей OpenRouter не должно подменять ключи выбранного пресета Groq."""
        self.client.post("/api/settings", json={**BASE_SETTINGS, "openai_api_keys": "gsk_groq_key"})
        self.client.post("/api/providers/openrouter", json={"api_keys": ["sk-or-key"]})
        data = self.client.get("/api/settings").json()
        self.assertEqual(data["openai_provider_preset"], "groq")
        self.assertEqual(data["openai_api_keys"], database.mask_api_key("gsk_groq_key"))


GEM_KEY_1 = "AIzaSyA-first-secret-key-0001"
GEM_KEY_2 = "AIzaSyB-second-secret-key-0002"


class TestApiKeysMasked(unittest.TestCase):
    """Ключи не покидают сервер в открытом виде, а маски из UI не затирают сохранённые ключи."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app, base_url="http://127.0.0.1", headers={TOKEN_HEADER: API_TOKEN})

    def setUp(self):
        database.DB_PATH = TEST_DB_PATH
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        database.init_db()
        self.client.post("/api/providers/gemini", json={"api_keys": [GEM_KEY_1, GEM_KEY_2]})

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)

    def _assert_no_raw_keys(self, res):
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(GEM_KEY_1, res.text)
        self.assertNotIn(GEM_KEY_2, res.text)

    def test_responses_do_not_contain_raw_keys(self):
        self._assert_no_raw_keys(self.client.get("/api/settings"))
        self._assert_no_raw_keys(self.client.get("/api/providers"))
        self._assert_no_raw_keys(self.client.get("/api/providers/gemini"))
        self._assert_no_raw_keys(self.client.post("/api/providers/gemini", json={"api_keys": [GEM_KEY_1, GEM_KEY_2]}))
        self._assert_no_raw_keys(self.client.get("/api/system-settings"))

    def test_masked_keys_round_trip_through_settings(self):
        """Форма присылает маски обратно вместе с новым ключом — сохранённые ключи остаются настоящими."""
        masked = self.client.get("/api/settings").json()["gemini_api_keys"]
        res = self.client.post("/api/settings", json={**BASE_SETTINGS, "gemini_api_keys": masked + ",AIzaSyC-new-key-0003"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(database.get_provider_config("gemini")["api_keys"], [GEM_KEY_1, GEM_KEY_2, "AIzaSyC-new-key-0003"])

    def test_masked_keys_round_trip_through_provider_modal(self):
        masked = self.client.get("/api/providers/gemini").json()["api_keys"]
        # Удаляем первый ключ в модалке и сохраняем
        self.client.post("/api/providers/gemini", json={"api_keys": masked[1:]})
        self.assertEqual(database.get_provider_config("gemini")["api_keys"], [GEM_KEY_2])

    def test_unknown_mask_is_not_saved_as_key(self):
        self.client.post("/api/providers/gemini", json={"api_keys": ["AIzaSy••••••••dead"]})
        self.assertEqual(database.get_provider_config("gemini")["api_keys"], [])

    def test_probe_accepts_masked_key(self):
        groq_key = "gsk_real-groq-secret-0001"
        self.client.post("/api/providers/groq", json={"api_keys": [groq_key]})
        with patch("src.api.routes.settings.requests.post") as post:
            post.return_value.status_code = 200
            res = self.client.post("/api/providers/groq/probe", json={"api_key": database.mask_api_key(groq_key)})
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(groq_key, res.text)
        self.assertIn(groq_key, str(post.call_args))


class TestPipelineClaim(unittest.TestCase):
    """Два одновременных запуска не должны стартовать две фоновые задачи."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app, base_url="http://127.0.0.1", headers={TOKEN_HEADER: API_TOKEN})

    def tearDown(self):
        state.release_pipeline()

    def test_claim_is_exclusive(self):
        self.assertTrue(state.try_claim_pipeline())
        self.assertFalse(state.try_claim_pipeline())
        state.release_pipeline()
        self.assertTrue(state.try_claim_pipeline())

    def test_second_search_rejected(self):
        with patch("src.api.routes.pipeline.run_pipeline_task") as task:
            self.assertTrue(state.try_claim_pipeline())
            res = self.client.post("/api/search")
            self.assertEqual(res.json()["status"], "error")
            task.assert_not_called()

    def test_stop_when_idle_does_not_leave_flag(self):
        """«Стоп» без запущенной задачи не должен остановить следующий запуск."""
        res = self.client.post("/api/stop")
        self.assertEqual(res.json()["status"], "ok")
        self.assertFalse(state.is_stop_requested())

    def test_claim_resets_previous_results(self):
        state.update_pipeline(last_error="old", last_status="success", last_run_stats={"processed": 5})
        self.assertTrue(state.try_claim_pipeline())
        snap = state.pipeline_snapshot()
        self.assertIsNone(snap["last_error"])
        self.assertIsNone(snap["last_run_stats"])
        # Снимок — копия: изменения состояния после чтения на него не влияют
        state.update_pipeline(currently_processing={"id": "1"})
        self.assertIsNone(snap["currently_processing"])


class TestErrorsAreJson(unittest.TestCase):
    def test_unhandled_error_returns_json_detail(self):
        client = TestClient(app, base_url="http://127.0.0.1", headers={TOKEN_HEADER: API_TOKEN},
                            raise_server_exceptions=False)
        with patch("src.api.routes.vacancies.database.get_processed_paginated", side_effect=RuntimeError("db is broken")):
            res = client.get("/api/jobs")
        self.assertEqual(res.status_code, 500)
        self.assertEqual(res.json()["detail"], "db is broken")


class TestResumeLoading(unittest.TestCase):
    def test_resumes_carry_name_and_gender(self):
        """Имя и пол из резюме доходят до ИИ во всех сценариях (раньше — только в сканировании)."""
        from unittest.mock import MagicMock
        from src.pipeline.runner import load_candidate_resumes
        hh = MagicMock()
        hh.get_my_resumes.return_value = [{"id": "r1", "title": "Python Dev"}]
        hh.get_resume.return_value = {"title": "Python Dev", "first_name": "Андрей", "last_name": "М", "gender": "Мужской",
                                      "experience": [], "skill_set": []}
        resumes = load_candidate_resumes(hh, "all")
        self.assertEqual(resumes[0]["first_name"], "Андрей")
        self.assertEqual(resumes[0]["gender"], "Мужской")


if __name__ == "__main__":
    unittest.main()
