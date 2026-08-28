import os
import sys
import unittest
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import sqlite3
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

import database
from app import app
import main
from hh_browser_client import HHBrowserClient
from llm_analyzer import LLMAnalyzer, VacancyAnalysis

# Используем унікальную тестовую БД для backend тестов
TEST_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_backend_db.db")
os.environ["HH_DB_PATH"] = TEST_DB_PATH
database.DB_PATH = TEST_DB_PATH

class TestHHApplierComprehensive(unittest.TestCase):
    
    @classmethod
    def setUpClass(cls):
        database.DB_PATH = TEST_DB_PATH
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except Exception:
                pass
        database.init_db()
        cls.client = TestClient(app)
        
    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except Exception:
                pass

    def setUp(self):
        database.DB_PATH = TEST_DB_PATH
        # Очищаем таблицы перед каждым тестом
        conn = sqlite3.connect(TEST_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM processed_vacancies")
        cursor.execute("DELETE FROM app_config")
        conn.commit()
        conn.close()

    def test_01_settings_api(self):
        """Тестирование получения и обновления настроек."""
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("queries", data)
        self.assertIn("dry_run", data)

        # Сохранение новых настроек
        payload = {
            "queries": ["Python Developer", "Data Engineer"],
            "area_id": "1",
            "threshold": 85,
            "resume_id": "Python Resume Test",
            "dry_run": True
        }
        post_res = self.client.post("/api/settings", json=payload)
        self.assertEqual(post_res.status_code, 200)
        
        # Проверяем, что сохранилось
        get_res = self.client.get("/api/settings")
        saved = get_res.json()
        self.assertEqual(saved["queries"], ["Python Developer", "Data Engineer"])
        self.assertEqual(saved["threshold"], 85)
        self.assertTrue(saved["dry_run"])

    def test_02_jobs_pagination_and_sorting(self):
        """Тестирование пагинации и сортировки (старые вакансии первыми)."""
        conn = sqlite3.connect(TEST_DB_PATH)
        cursor = conn.cursor()
        # Вставляем 3 записи с разным временем
        cursor.execute(
            "INSERT INTO processed_vacancies (id, title, company, status, match_score, processed_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("vac_1", "Senior Python", "Yandex", "new", 90, "2026-08-12 10:00:00")
        )
        cursor.execute(
            "INSERT INTO processed_vacancies (id, title, company, status, match_score, processed_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("vac_2", "Middle Python", "Sber", "failed", 0, "2026-08-12 11:00:00")
        )
        cursor.execute(
            "INSERT INTO processed_vacancies (id, title, company, status, match_score, processed_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("vac_3", "Lead Python", "VK", "ignored", 40, "2026-08-12 12:00:00")
        )
        conn.commit()
        conn.close()

        # Запрос пагинации (limit=2, offset=0)
        res = self.client.get("/api/jobs?limit=2&offset=0")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        
        self.assertEqual(len(body["jobs"]), 2)
        # Самая новая vac_3 должна быть первой (ORDER BY processed_at DESC)
        self.assertEqual(body["jobs"][0]["id"], "vac_3")
        self.assertEqual(body["jobs"][1]["id"], "vac_2")
        
        # Проверяем счетчики в статистике
        stats = body["stats"]
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["ignored"], 1)

    def test_03_reanalyze_endpoints(self):
        """Тестирование повторной переоценки вакансий с ошибками."""
        # Вставляем вакансию в статусе failed
        database.save_vacancy(
            vacancy_id="failed_vac_100",
            title="Python Developer",
            company="FailCorp",
            status="failed",
            match_score=0,
            analysis_reason="Test LLM Error"
        )
        database.set_config_value("dry_run", "true")
        
        mock_analysis = VacancyAnalysis(
            match_score=88,
            is_match=True,
            reasoning="Отлично подходит!",
            cover_letter="Добрый день, заинтересовала вакансия..."
        )
        mock_details = {
            "id": "failed_vac_100",
            "title": "Python Developer",
            "company": "FailCorp",
            "description": "Python, FastAPI, PostgreSQL"
        }
        
        mock_hh_client = MagicMock()
        mock_hh_client.get_vacancy_details.return_value = mock_details
        mock_hh_client.get_resume.return_value = None
        mock_hh_client.get_vacancy_questions.return_value = []

        with patch("app.HHBrowserClient", return_value=mock_hh_client), \
             patch("main.load_resume_text", return_value="Senior Python Engineer"), \
             patch.object(LLMAnalyzer, "analyze_vacancy", return_value=mock_analysis):
            
            # Запускаем переоценку одной вакансии
            res = self.client.post("/api/reanalyze/failed_vac_100")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["status"], "ok")
            
            # Проверяем в БД, что статус изменился на "new"
            updated = database.get_vacancy("failed_vac_100")
            self.assertIsNotNone(updated)
            self.assertEqual(updated[3], "new") # status = 'new'
            self.assertEqual(updated[4], 88)   # score = 88

    def test_04_batch_reanalyze_all_failed(self):
        """Тестирование вызова пакетной переоценки всех ошибочных вакансий."""
        database.save_vacancy("err_1", "Backend Dev", "CompA", "failed", 0)
        database.save_vacancy("err_2", "Fullstack Dev", "CompB", "failed", 0)

        res = self.client.post("/api/reanalyze-all-failed")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "started")
        self.assertIn("Запущена переоценка 2", data["message"])

    def test_05_mistral_settings_api(self):
        """Тестирование сохранения и загрузки настроек Mistral через API."""
        payload = {
            "queries": ["Python Lead"],
            "area_id": "113",
            "threshold": 80,
            "resume_id": "res_123",
            "dry_run": True,
            "gemini_api_keys": "gemini_key_1,gemini_key_2",
            "gemini_model": "gemini-3.6-flash",
            "mistral_api_keys": "mistral_key_abc",
            "mistral_model": "mistral-small-latest"
        }
        res = self.client.post("/api/settings", json=payload)
        self.assertEqual(res.status_code, 200)

        get_res = self.client.get("/api/settings")
        self.assertEqual(get_res.status_code, 200)
        data = get_res.json()
        self.assertEqual(data["mistral_api_keys"], "mistral_key_abc")
        self.assertEqual(data["mistral_model"], "mistral-small-latest")
        self.assertEqual(data["gemini_api_keys"], "gemini_key_1,gemini_key_2")

    def test_06_mistral_direct_and_fallback(self):
        """Тестирование прямого вызова Mistral и fallback при исчерпании Gemini."""
        from llm_analyzer import LLMAnalyzer, QuotaExceededError
        import requests

        analyzer = LLMAnalyzer(
            gemini_api_keys=["invalid_gemini_key"],
            mistral_api_keys=["valid_mistral_key"]
        )

        mock_mistral_response = MagicMock()
        mock_mistral_response.status_code = 200
        mock_mistral_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"match_score": 92, "is_match": true, "reasoning": "Отличный опыт Python и AI", "cover_letter": "Здравствуйте! Заинтересовала ваша вакансия..."}'
                    }
                }
            ]
        }

        # Симулируем 429 ошибку на Gemini и успешный ответ на Mistral
        with patch.object(analyzer, "_call_gemini", side_effect=QuotaExceededError("Gemini 429")), \
             patch("requests.post", return_value=mock_mistral_response):
            
            res = analyzer.analyze_vacancy(
                resume_text="Senior Python Developer, FastAPI, LLM",
                vacancy={"title": "Python AI Engineer", "company": "TechLab", "salary": "300k", "skills": ["Python", "LLM"], "description": "Looking for Python AI engineer"},
                threshold=75
            )

            self.assertEqual(res.match_score, 92)
            self.assertTrue(res.is_match)
            self.assertIn("Отличный опыт", res.reasoning)

    def test_07_model_status_endpoint(self):
        """Тестирование эндпоинта статуса моделей."""
        res = self.client.get("/api/model-status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("status", data)
        self.assertIn("gemini", data)
        self.assertIn("mistral", data)

    def test_08_mistral_only_mode(self):
        """Тестирование работы анализатора, когда настроен только Mistral."""
        from llm_analyzer import LLMAnalyzer
        import requests

        analyzer = LLMAnalyzer(
            gemini_api_keys=[],
            mistral_api_keys=["mistral_key_single"]
        )

        mock_mistral_response = MagicMock()
        mock_mistral_response.status_code = 200
        mock_mistral_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"match_score": 85, "is_match": true, "reasoning": "Подходит по стеку", "cover_letter": "Добрый день!"}'
                    }
                }
            ]
        }

        with patch("requests.post", return_value=mock_mistral_response):
            res = analyzer.analyze_vacancy(
                resume_text="Python Backend Engineer",
                vacancy={"title": "Python Dev", "company": "Co", "salary": "200k", "skills": ["Python"], "description": "Good job"},
                threshold=70
            )
            self.assertEqual(res.match_score, 85)
            self.assertTrue(res.is_match)

    def test_09_mistral_key_rotation_on_429(self):
        """Тестирование ротации ключей Mistral при ошибке 429."""
        from llm_analyzer import LLMAnalyzer
        import requests

        analyzer = LLMAnalyzer(
            gemini_api_keys=[],
            mistral_api_keys=["mistral_key_1", "mistral_key_2"]
        )

        # Первый ответ 429, второй 200
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.text = "Rate limit reached"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"match_score": 77, "is_match": true, "reasoning": "Норм", "cover_letter": "Привет!"}'
                    }
                }
            ]
        }

        with patch("requests.post", side_effect=[resp_429, resp_200]):
            res = analyzer.analyze_vacancy(
                resume_text="Python",
                vacancy={"title": "Dev", "company": "C", "skills": ["Python"], "description": "Desc"},
                threshold=75
            )
            self.assertEqual(res.match_score, 77)

    def test_10_system_settings_db_and_api(self):
        """Тестирование чтения/записи таблицы system_settings через API."""
        payload = {
            "system_prompt": "CUSTOM PROMPT FOR {vacancy_title} AND {company}",
            "primary_provider": "mistral",
            "fallback_enabled": False,
            "temperature": 0.5,
            "gemini_model": "gemini-3.6-flash",
            "mistral_model": "mistral-large-latest"
        }
        res = self.client.post("/api/system-settings", json=payload)
        self.assertEqual(res.status_code, 200)

        get_res = self.client.get("/api/system-settings")
        self.assertEqual(get_res.status_code, 200)
        data = get_res.json()
        self.assertEqual(data["system_prompt"], "CUSTOM PROMPT FOR {vacancy_title} AND {company}")
        self.assertEqual(data["primary_provider"], "mistral")
        self.assertFalse(data["fallback_enabled"])
        self.assertEqual(data["temperature"], 0.5)

    def test_11_custom_prompt_variable_substitution(self):
        """Тестирование подстановки переменных в кастомный системный промпт."""
        from llm_analyzer import LLMAnalyzer

        custom_prompt = "Оцени {resume_text} для {vacancy_title} в {company}, навыки: {skills}, порог {threshold}"
        database.set_system_setting("system_prompt", custom_prompt)

        analyzer = LLMAnalyzer()
        built = analyzer._build_prompt(
            resume_text="Senior Pythonista",
            vacancy={"title": "Lead Backend", "company": "SuperCorp", "skills": ["Python", "FastAPI"]},
            match_threshold=85
        )

        self.assertIn("Senior Pythonista", built)
        self.assertIn("Lead Backend", built)
        self.assertIn("SuperCorp", built)
        self.assertIn("Python, FastAPI", built)
        self.assertIn("85", built)

    def test_12_reset_system_prompt_api(self):
        """Тестирование сброса системного промпта через API."""
        database.set_system_setting("system_prompt", "MODIFIED PROMPT")
        self.assertEqual(database.get_system_setting("system_prompt"), "MODIFIED PROMPT")

        res = self.client.post("/api/system-settings/reset-prompt")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("Вы — профессиональный IT-рекрутер", data["system_prompt"])
        self.assertEqual(database.get_system_setting("system_prompt"), database.DEFAULT_SYSTEM_PROMPT)

    def test_13_primary_provider_mistral_priority(self):
        """Тестирование приоритета провайдера: когда Mistral выбран основным, он вызывается первым."""
        from llm_analyzer import LLMAnalyzer
        import requests

        database.set_system_setting("primary_provider", "mistral")
        database.set_system_setting("fallback_enabled", "true")

        analyzer = LLMAnalyzer(
            gemini_api_keys=["gemini_key"],
            mistral_api_keys=["mistral_key"]
        )

        mock_mistral_response = MagicMock()
        mock_mistral_response.status_code = 200
        mock_mistral_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"match_score": 95, "is_match": true, "reasoning": "Mistral primary success", "cover_letter": "Letter"}'
                    }
                }
            ]
        }

        # _call_gemini НЕ должен вызываться, так как Mistral основной и вернул 200
        with patch.object(analyzer, "_call_gemini") as mock_gemini, \
             patch("requests.post", return_value=mock_mistral_response):
            
            res = analyzer.analyze_vacancy(
                resume_text="Python Lead",
                vacancy={"title": "Team Lead", "company": "BigTech", "skills": ["Python"]},
                threshold=80
            )

            self.assertEqual(res.match_score, 95)
            self.assertEqual(res.reasoning, "Mistral primary success")
            mock_gemini.assert_not_called()

    def test_14_multi_resume_llm_selection(self):
        """Тестирование автоматического выбора лучшего резюме через LLM из списка."""
        from llm_analyzer import LLMAnalyzer
        import requests

        analyzer = LLMAnalyzer(
            gemini_api_keys=[],
            mistral_api_keys=["mistral_key"]
        )

        resumes = [
            {"id": "res_python", "title": "Senior Python Backend Developer", "text": "Python, Django, FastAPI, PostgreSQL, Redis"},
            {"id": "res_react", "title": "Senior Frontend React Developer", "text": "TypeScript, React, Next.js, Redux, Tailwind"}
        ]

        mock_mistral_response = MagicMock()
        mock_mistral_response.status_code = 200
        mock_mistral_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"match_score": 96, "is_match": true, "reasoning": "Резюме Python Developer идеально подходит под требования FastAPI", "cover_letter": "Здравствуйте! Мой опыт Python...", "selected_resume_id": "res_python", "selected_resume_title": "Senior Python Backend Developer"}'
                    }
                }
            ]
        }

        with patch("requests.post", return_value=mock_mistral_response):
            res = analyzer.analyze_vacancy(
                resumes=resumes,
                vacancy={"title": "Backend Python Developer", "company": "Tech", "skills": ["Python", "FastAPI"]},
                threshold=75
            )

            self.assertEqual(res.match_score, 96)
            self.assertTrue(res.is_match)
            self.assertEqual(res.selected_resume_id, "res_python")
            self.assertEqual(res.selected_resume_title, "Senior Python Backend Developer")

    def test_15_database_applied_resume_columns(self):
        """Тестирование сохранения и извлечения applied_resume_id и applied_resume_title в БД."""
        database.save_vacancy(
            vacancy_id="vac_multi_1",
            title="AI Engineer",
            company="NeuralCo",
            status="applied",
            match_score=94,
            analysis_reason="Отличное совпадение",
            cover_letter="Письмо...",
            applied_resume_id="res_ml_ai",
            applied_resume_title="Machine Learning Engineer"
        )

        jobs_res = self.client.get("/api/jobs")
        self.assertEqual(jobs_res.status_code, 200)
        jobs = jobs_res.json()["jobs"]
        self.assertTrue(any(j["id"] == "vac_multi_1" for j in jobs))
        target_job = next(j for j in jobs if j["id"] == "vac_multi_1")
        self.assertEqual(target_job["applied_resume_id"], "res_ml_ai")
        self.assertEqual(target_job["applied_resume_title"], "Machine Learning Engineer")

if __name__ == "__main__":
    unittest.main()
