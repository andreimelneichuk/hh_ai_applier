import os
import sys
import json
import unittest
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import sqlite3
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

import src.db.database as database
from src.api.app import app
import src.pipeline.runner as main
from src.clients.browser import HHBrowserClient
from src.clients.llm import LLMAnalyzer, VacancyAnalysis

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
        database.init_db()
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

        with patch("src.api.routes.vacancies.HHBrowserClient", return_value=mock_hh_client), \
             patch("src.api.routes.vacancies.load_resume_text", return_value="Senior Python Engineer"), \
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
        from src.clients.llm import LLMAnalyzer, QuotaExceededError
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
        from src.clients.llm import LLMAnalyzer
        import requests

        database.set_system_setting("primary_provider", "mistral")
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
        from src.clients.llm import LLMAnalyzer
        import requests

        database.set_system_setting("primary_provider", "mistral")
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
        from src.clients.llm import LLMAnalyzer

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
        from src.clients.llm import LLMAnalyzer
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
        from src.clients.llm import LLMAnalyzer
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

    def test_16_classification_prompt_grade_and_experience_variables(self):
        """Тестирование наличия строгих правил грейдов в DEFAULT_SYSTEM_PROMPT и подстановки новых переменных."""
        from src.clients.llm import LLMAnalyzer

        self.assertIn("ГРЕЙД И УРОВЕНЬ ОТВЕТСТВЕННОСТИ", database.DEFAULT_SYSTEM_PROMPT)
        self.assertIn("РЕЛЕВАНТНЫЙ коммерческий опыт", database.DEFAULT_SYSTEM_PROMPT)
        self.assertIn("ФОРМАТ РАБОТЫ, ЛОКАЦИЯ И УСЛОВИЯ", database.DEFAULT_SYSTEM_PROMPT)
        self.assertIn("РЕЛЕВАНТНЫЙ ОПЫТ И СТАЖ В ГОДАХ", database.DEFAULT_SYSTEM_PROMPT)
        self.assertIn("{experience}", database.DEFAULT_SYSTEM_PROMPT)
        self.assertIn("{employment}", database.DEFAULT_SYSTEM_PROMPT)
        self.assertIn("{schedule}", database.DEFAULT_SYSTEM_PROMPT)
        self.assertIn("{location}", database.DEFAULT_SYSTEM_PROMPT)

        custom_prompt = (
            "Вакансия: {vacancy_title}, Опыт: {experience}, Занятость: {employment}, "
            "График: {schedule}, Локация: {location}, Кандидат: {resume_text}"
        )
        database.set_system_setting("system_prompt", custom_prompt)

        analyzer = LLMAnalyzer()
        built = analyzer._build_prompt(
            resume_text="Junior Python Developer (1 год опыта)",
            vacancy={
                "title": "Senior Python Engineer",
                "experience": "от 3 до 6 лет",
                "employment": "Полная занятость",
                "schedule": "Удаленная работа",
                "location": "Москва"
            },
            match_threshold=80
        )

        self.assertIn("Senior Python Engineer", built)
        self.assertIn("от 3 до 6 лет", built)
        self.assertIn("Полная занятость", built)
        self.assertIn("Удаленная работа", built)
        self.assertIn("Москва", built)
        self.assertIn("Junior Python Developer (1 год опыта)", built)

    def test_17_format_hh_resume_experience_and_location(self):
        """Тестирование включения общего стажа, локации и периодов работы в format_hh_resume_to_text."""
        from src.pipeline.runner import format_hh_resume_to_text

        resume_data = {
            "first_name": "Иван",
            "last_name": "Иванов",
            "title": "Junior Backend Разработчик",
            "total_experience": "1 год 4 месяца",
            "location": "Пермь, не готов к переезду",
            "employment": "Полная занятость",
            "schedule": "Удаленная работа",
            "skills": "Python, SQL, Django",
            "experience": [
                {
                    "company": "Стартап",
                    "position": "Junior Python Разработчик",
                    "period": "Январь 2025 — по настоящее время (1 год)",
                    "description": "Разработка REST API на FastAPI"
                }
            ]
        }

        formatted = format_hh_resume_to_text(resume_data)
        self.assertIn("Общий стаж работы: 1 год 4 месяца", formatted)
        self.assertIn("Город / Локация: Пермь, не готов к переезду", formatted)
        self.assertIn("Предпочитаемая занятость: Полная занятость", formatted)
        self.assertIn("Предпочитаемый график: Удаленная работа", formatted)
        self.assertIn("Январь 2025 — по настоящее время (1 год)", formatted)

    def test_18_cover_letter_postfix_saving_and_application(self):
        """Тестирование сохранения и применения постфикса (подписи) к сопроводительному письму."""
        from src.clients.llm import LLMAnalyzer

        # 1. Проверяем сохранение через API системных настроек
        postfix_text = "Telegram: @my_telegram | GitHub: github.com/test"
        res = self.client.post("/api/system-settings", json={
            "cover_letter_postfix": postfix_text
        })
        self.assertEqual(res.status_code, 200)

        get_res = self.client.get("/api/system-settings")
        self.assertEqual(get_res.status_code, 200)
        self.assertEqual(get_res.json()["cover_letter_postfix"], postfix_text)

        # 2. Проверяем добавление постфикса в анализаторе
        analyzer = LLMAnalyzer()
        res_analysis = analyzer._mock_analysis(
            vacancy={"title": "Python Developer", "company": "TechLab", "skills": ["Python"]},
            match_threshold=40
        )
        self.assertTrue(res_analysis.is_match)
        self.assertIn(postfix_text, res_analysis.cover_letter)
        self.assertTrue(res_analysis.cover_letter.strip().endswith(postfix_text))

    def test_19_llm_analyzer_generate_cover_letter(self):
        """Тестирование генерации сопроводительного письма методом generate_cover_letter."""
        from src.clients.llm import LLMAnalyzer

        analyzer = LLMAnalyzer()
        resume_text = "Иван Иванов\nPython разработчик, 3 года опыта. FastAPI, PostgreSQL, Docker."
        vacancy = {
            "title": "Backend Python Developer",
            "company": "Tech Innovations",
            "skills": ["Python", "FastAPI"]
        }

        letter = analyzer.generate_cover_letter(resume_text=resume_text, vacancy=vacancy)
        self.assertIsInstance(letter, str)
        self.assertTrue(len(letter) > 20)
        self.assertIn("Backend Python Developer", letter)

    @patch("src.api.routes.vacancies.HHBrowserClient")
    def test_20_generate_cover_letter_endpoint(self, mock_hh_class):
        """Тестирование эндпоинта POST /api/generate-cover-letter/{vacancy_id} для отсеянной вакансии."""
        # Мокаем HHBrowserClient
        mock_hh = MagicMock()
        mock_hh_class.return_value = mock_hh
        mock_hh.get_my_resumes.return_value = [
            {"id": "res_1", "title": "Python Developer", "text": "Python Developer 3 года опыта. FastAPI, Docker."}
        ]
        mock_hh.get_vacancy_details.return_value = {
            "title": "Middle Python Developer",
            "company": "Acme Corp",
            "description": "Ищем сильного разработчика",
            "skills": ["Python", "FastAPI"]
        }

        # Добавляем вакансию со статусом ignored и пустым cover_letter
        conn = sqlite3.connect(TEST_DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO processed_vacancies (id, title, company, status, match_score, analysis_reason, cover_letter, processed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("vac_ignored_1", "Middle Python Developer", "Acme Corp", "ignored", 35, "Недостаточно стажа", "", "2026-09-04 12:00:00")
        )
        conn.commit()
        conn.close()

        # Вызываем эндпоинт генерации письма
        res = self.client.post("/api/generate-cover-letter/vac_ignored_1")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["vacancy_id"], "vac_ignored_1")
        self.assertTrue(len(data["cover_letter"]) > 10)

        # Проверяем, что письмо сохранилось в базе данных
        conn = sqlite3.connect(TEST_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT cover_letter FROM processed_vacancies WHERE id = ?", ("vac_ignored_1",))
        row = cursor.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], data["cover_letter"])

    def test_21_relocation_and_cities_formatting_and_fallback(self):
        """Тестирование корректного включения города проживания, готовности к переезду и целевых городов."""
        from src.pipeline.runner import format_hh_resume_to_text

        # 1. Резюме с явным указанием переезда и списка городов
        resume_data_1 = {
            "first_name": "Алексей",
            "last_name": "Петров",
            "title": "Backend Python Developer",
            "location": "Пермь",
            "relocation": "готов к переезду (Москва, Санкт-Петербург), готов к редким командировкам",
            "relocation_cities": ["Москва", "Санкт-Петербург"]
        }
        text_1 = format_hh_resume_to_text(resume_data_1)
        self.assertIn("Город / Локация: Пермь", text_1)
        self.assertIn("Готовность к переезду: готов к переезду (Москва, Санкт-Петербург), готов к редким командировкам", text_1)
        self.assertIn("Города, куда готов переехать: Москва, Санкт-Петербург", text_1)

        # 2. Проверка фолбека на user_profile_answers, если в самом резюме поле переезда пустое
        database.set_user_profile_answer("relocation_cities", "Города для переезда / релокации", "Готов к переезду: Казань, Екатеринбург")
        resume_data_2 = {
            "first_name": "Алексей",
            "last_name": "Петров",
            "title": "Backend Python Developer",
            "location": "Пермь"
        }
        text_2 = format_hh_resume_to_text(resume_data_2)
        self.assertIn("Город / Локация: Пермь", text_2)
        self.assertIn("Готовность к переезду: Готов к переезду: Казань, Екатеринбург", text_2)

    def test_22_vacancy_analysis_dict_coercion(self):
        """Тестирование автоматического преобразования вложенных словарей от LLM в строки."""
        from src.clients.llm import VacancyAnalysis, QuestionAnswer, CoverLetterResult

        # 1. Валидация VacancyAnalysis с вложенными словарями
        json_str = '''{
            "match_score": 88,
            "is_match": true,
            "reasoning": {
                "опыт_и_грейд": "4 года в разработке LLM-агентов",
                "стек": "FastAPI, PostgreSQL"
            },
            "cover_letter": {
                "text": "Здравствуйте! Меня зовут... С уважением, Кандидат."
            }
        }'''
        analysis = VacancyAnalysis.model_validate_json(json_str)
        self.assertEqual(analysis.match_score, 88)
        self.assertTrue(analysis.is_match)
        self.assertIn("4 года в разработке LLM-агентов", analysis.reasoning)
        self.assertEqual(analysis.cover_letter, "Здравствуйте! Меня зовут... С уважением, Кандидат.")

        # 2. Валидация QuestionAnswer со словарем в ответе
        qa = QuestionAnswer(
            id="q1",
            question_text="Опыт работы?",
            answer={"text": "Более 5 лет"},
            confidence=95,
            requires_user_input=False,
            reasoning={"note": "Из резюме"}
        )
        self.assertEqual(qa.answer, "Более 5 лет")
        self.assertIn("Из резюме", qa.reasoning)

        # 3. Валидация CoverLetterResult со словарем
        cl = CoverLetterResult.model_validate({"cover_letter": {"letter": "Приветственное письмо"}})
        self.assertEqual(cl.cover_letter, "Приветственное письмо")

    def test_23_pipeline_stop_conditions(self):
        """Тестирование сохранения настроек автоостановки и их работы в run_pipeline."""
        from src.pipeline.runner import run_pipeline
        from src.clients.llm import VacancyAnalysis

        # 1. Проверка сохранения настроек через API
        payload = {
            "queries": ["python"],
            "area_id": "113",
            "threshold": 80,
            "resume_id": "res_123",
            "dry_run": True,
            "stop_condition": "applications",
            "limit_applications": 5,
            "limit_processed": 15
        }
        res = self.client.post("/api/settings", json=payload)
        self.assertEqual(res.status_code, 200)

        get_res = self.client.get("/api/settings")
        self.assertEqual(get_res.status_code, 200)
        data = get_res.json()
        self.assertEqual(data["stop_condition"], "applications")
        self.assertEqual(data["limit_applications"], 5)
        self.assertEqual(data["limit_processed"], 15)

        # 2. Проверка работы автоостановки в run_pipeline по лимиту оцененных вакансий
        mock_browser = MagicMock()
        mock_browser.is_logged_in.return_value = True
        mock_browser.get_my_resumes.return_value = [{"id": "r1", "title": "Python Dev"}]
        mock_browser.get_resume.return_value = {"title": "Python Dev", "total_experience": "3 года"}
        mock_browser.get_recommended_vacancies.return_value = [
            {"id": "v101", "title": "Backend 1", "company": "Co1"},
            {"id": "v102", "title": "Backend 2", "company": "Co2"},
            {"id": "v103", "title": "Backend 3", "company": "Co3"}
        ]
        mock_browser.get_vacancy_details.return_value = {"title": "Backend", "company": "Co"}

        mock_analyzer = MagicMock()
        mock_analyzer.analyze_vacancy.return_value = VacancyAnalysis(
            match_score=50,
            is_match=False,
            reasoning="Недостаточно опыта",
            cover_letter=""
        )

        with patch("src.pipeline.runner.HHBrowserClient", return_value=mock_browser), \
             patch("src.pipeline.runner.LLMAnalyzer", return_value=mock_analyzer):
            
            # Запуск с лимитом 1 обработанная вакансия
            result = run_pipeline(
                queries=[],
                area_id="113",
                threshold=80,
                resume_id="r1",
                dry_run=True,
                stop_condition="processed",
                limit_processed=1,
                limit_applications=10
            )

            self.assertEqual(result["stats"]["processed"], 1)
            self.assertEqual(result["stats"]["stopped_reason"], "limit_processed")

            # 3. Проверка работы автоостановки в run_pipeline по лимиту откликов
            mock_analyzer.analyze_vacancy.return_value = VacancyAnalysis(
                match_score=90,
                is_match=True,
                reasoning="Отличное совпадение",
                cover_letter="Письмо"
            )
            mock_browser.get_recommended_vacancies.return_value = [
                {"id": "v201", "title": "Lead 1", "company": "Co1"},
                {"id": "v202", "title": "Lead 2", "company": "Co2"},
                {"id": "v203", "title": "Lead 3", "company": "Co3"}
            ]

            result_apps = run_pipeline(
                queries=[],
                area_id="113",
                threshold=80,
                resume_id="r1",
                dry_run=True,
                stop_condition="applications",
                limit_processed=50,
                limit_applications=2
            )

            # В dry_run режиме 2 подходящие вакансии активируют лимит откликов
            self.assertEqual(result_apps["stats"]["matched"], 2)
            self.assertEqual(result_apps["stats"]["stopped_reason"], "limit_applications")

    def test_24_openai_presets_and_system_settings_api(self):
        """Тестирование получения пресетов OpenAI и сохранения системных настроек OpenAI."""
        # 1. Проверяем эндпоинт /api/openai-presets
        presets_res = self.client.get("/api/openai-presets")
        self.assertEqual(presets_res.status_code, 200)
        presets_data = presets_res.json()
        self.assertIn("presets", presets_data)
        preset_ids = [p["id"] for p in presets_data["presets"]]
        self.assertIn("groq", preset_ids)
        self.assertIn("openrouter", preset_ids)
        self.assertIn("github", preset_ids)
        self.assertIn("cerebras", preset_ids)
        self.assertIn("custom", preset_ids)

        groq_preset = next(p for p in presets_data["presets"] if p["id"] == "groq")
        self.assertEqual(groq_preset["base_url"], "https://api.groq.com/openai/v1")
        self.assertIn("console.groq.com", groq_preset["get_key_url"])

        # 2. Проверяем сохранение настроек через /api/system-settings
        save_res = self.client.post("/api/system-settings", json={
            "primary_provider": "openai",
            "openai_provider_preset": "openrouter",
            "openai_base_url": "https://openrouter.ai/api/v1",
            "openai_model": "meta-llama/llama-3.3-70b-instruct:free"
        })
        self.assertEqual(save_res.status_code, 200)

        get_res = self.client.get("/api/system-settings")
        self.assertEqual(get_res.status_code, 200)
        data = get_res.json()
        self.assertEqual(data["primary_provider"], "openai")
        self.assertEqual(data["openai_provider_preset"], "openrouter")
        self.assertEqual(data["openai_base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(data["openai_model"], "meta-llama/llama-3.3-70b-instruct:free")
        self.assertIn("openai_presets", data)

    def test_25_openai_keys_settings_and_model_status(self):
        """Тестирование сохранения ключей OpenAI и отображения в /api/model-status."""
        # 1. Сохраняем ключи через /api/settings
        res = self.client.post("/api/settings", json={
            "queries": ["Python"],
            "area_id": "113",
            "threshold": 75,
            "resume_id": "res_test",
            "dry_run": True,
            "openai_api_keys": "gsk_test_key_1,gsk_test_key_2",
            "openai_provider_preset": "groq",
            "openai_base_url": "https://api.groq.com/openai/v1",
            "openai_model": "llama-3.3-70b-versatile"
        })
        self.assertEqual(res.status_code, 200)

        # 2. Проверяем /api/settings
        get_res = self.client.get("/api/settings")
        self.assertEqual(get_res.status_code, 200)
        settings_data = get_res.json()
        self.assertEqual(settings_data["openai_api_keys"], "gsk_test_key_1,gsk_test_key_2")

        # 3. Проверяем /api/model-status
        status_res = self.client.get("/api/model-status")
        self.assertEqual(status_res.status_code, 200)
        status_data = status_res.json()
        self.assertIn("openai", status_data)
        self.assertEqual(status_data["openai"]["total"], 2)
        self.assertEqual(status_data["openai"]["preset"], "groq")

    def test_26_openai_direct_analysis_and_fallback(self):
        """Тестирование анализа вакансии через OpenAI-совместимый эндпоинт и каскадного fallback."""
        from src.clients.llm import LLMAnalyzer, QuotaExceededError
        import requests

        analyzer = LLMAnalyzer(
            gemini_api_keys=["gemini_fallback_key"],
            openai_api_keys=["gsk_test_key"]
        )

        mock_openai_response = MagicMock()
        mock_openai_response.status_code = 200
        mock_openai_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({
                            "reasoning": "Отличное совпадение по стеку FastAPI и Python",
                            "scores": {
                                "stack_score": 28,
                                "experience_score": 22,
                                "grade_score": 18,
                                "domain_score": 14,
                                "format_score": 10
                            },
                            "has_hard_blocker": False,
                            "blocker_reason": None,
                            "match_score": 92,
                            "is_match": True,
                            "cover_letter": "Здравствуйте! Заинтересовала ваша вакансия Backend разработчика."
                        })
                    }
                }
            ]
        }

        database.set_system_setting("primary_provider", "openai")
        database.set_system_setting("fallback_enabled", "true")

        with patch("requests.post", return_value=mock_openai_response):
            result = analyzer.analyze_vacancy(
                resume_text="Senior Python Developer, 5 лет опыта, FastAPI, PostgreSQL",
                vacancy={
                    "title": "Senior Python Developer",
                    "company": "FastTech",
                    "description": "Ищем Senior Python разработчика на FastAPI",
                    "skills": ["Python", "FastAPI"]
                },
                threshold=75
            )

            self.assertTrue(result.is_match)
            self.assertEqual(result.match_score, 92)
            self.assertIn("Заинтересовала ваша вакансия", result.cover_letter)

        # Тестируем fallback: OpenAI возвращает 429, система переключается на Gemini
        expected_gemini_ans = VacancyAnalysis(
            match_score=85,
            is_match=True,
            reasoning="Ответ от Gemini fallback",
            cover_letter="Письмо от Gemini"
        )
        with patch.object(analyzer, "_call_openai_compatible", side_effect=QuotaExceededError("Groq 429 Quota Exceeded")), \
             patch.object(analyzer, "_call_gemini", return_value=expected_gemini_ans):
            fallback_res = analyzer.analyze_vacancy(
                resume_text="Python Developer",
                vacancy={"title": "Python Dev", "company": "Co", "skills": ["Python"]},
                threshold=70
            )
            self.assertEqual(fallback_res.match_score, 85)
            self.assertEqual(fallback_res.reasoning, "Ответ от Gemini fallback")

    def test_27_openai_answer_questions_and_cover_letter(self):
        """Тестирование генерации ответов на вопросы и сопроводительного письма через OpenAI провайдер."""
        from src.clients.llm import LLMAnalyzer, QuestionAnswer, QuestionsAnalysisResult

        analyzer = LLMAnalyzer(openai_api_keys=["gsk_test_key"])
        database.set_system_setting("primary_provider", "openai")

        mock_qa_response = MagicMock()
        mock_qa_response.status_code = 200
        mock_qa_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({
                            "answers": [
                                {
                                    "id": "q1",
                                    "question_text": "Какой опыт с Python?",
                                    "answer": "Более 4 лет коммерческой разработки",
                                    "confidence": 95,
                                    "requires_user_input": False,
                                    "reasoning": "Указано в резюме"
                                }
                            ],
                            "all_confident": True
                        })
                    }
                }
            ]
        }

        with patch("requests.post", return_value=mock_qa_response):
            qa_res = analyzer.answer_questions(
                resume_text="Python Developer, 4 года опыта",
                vacancy={"title": "Backend", "company": "Tech"},
                questions=[{"id": "q1", "text": "Какой опыт с Python?", "type": "text"}]
            )
            self.assertIsInstance(qa_res, QuestionsAnalysisResult)
            self.assertEqual(len(qa_res.answers), 1)
            self.assertEqual(qa_res.answers[0].answer, "Более 4 лет коммерческой разработки")

        # Тестирование generate_cover_letter
        mock_cl_response = MagicMock()
        mock_cl_response.status_code = 200
        mock_cl_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "Здравствуйте! Мой стек FastAPI и PostgreSQL идеально подходит для вашей роли."
                    }
                }
            ]
        }

        with patch("requests.post", return_value=mock_cl_response):
            cl_res = analyzer.generate_cover_letter(
                resume_text="Python Developer, 4 года опыта",
                vacancy={"title": "Backend", "company": "Tech"}
            )
            self.assertIn("FastAPI", cl_res)

    def test_28_unified_providers_endpoint(self):
        """Тестирование получения списка единых провайдеров /api/providers."""
        res = self.client.get("/api/providers")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("providers", data)
        providers = data["providers"]
        provider_ids = [p["id"] for p in providers]
        self.assertIn("gemini", provider_ids)
        self.assertIn("mistral", provider_ids)
        self.assertIn("groq", provider_ids)
        self.assertIn("openrouter", provider_ids)
        self.assertIn("github", provider_ids)
        self.assertIn("cerebras", provider_ids)

        groq_p = next(p for p in providers if p["id"] == "groq")
        self.assertEqual(groq_p["protocol"], "openai")
        self.assertIn("LPU", groq_p["description"])

        # Тест получения детальной информации по провайдеру
        detail_res = self.client.get("/api/providers/gemini")
        self.assertEqual(detail_res.status_code, 200)
        detail_data = detail_res.json()
        self.assertEqual(detail_data["id"], "gemini")
        self.assertIn("temperature", detail_data)

        # Тест обновления температуры и модели провайдера
        update_res = self.client.post("/api/providers/gemini", json={"temperature": 0.45, "active_model": "gemini-2.5-flash"})
        self.assertEqual(update_res.status_code, 200)
        self.assertEqual(update_res.json()["provider"]["temperature"], 0.45)

        # Тест создания кастомного провайдера
        create_res = self.client.post("/api/providers/custom", json={
            "name": "Local Ollama",
            "base_url": "http://localhost:11434/v1",
            "active_model": "llama3.2",
            "temperature": 0.7
        })
        self.assertEqual(create_res.status_code, 200)
        custom_id = create_res.json()["provider"]["id"]
        self.assertTrue(custom_id.startswith("custom_"))

        # Проверяем, что кастомный провайдер появился в общем списке
        providers_res2 = self.client.get("/api/providers")
        p_ids2 = [p["id"] for p in providers_res2.json()["providers"]]
        self.assertIn(custom_id, p_ids2)

        # Тест probe
        probe_res = self.client.post(f"/api/providers/{custom_id}/probe")
        self.assertEqual(probe_res.status_code, 200)

        # Тест удаления кастомного провайдера
        del_res = self.client.delete(f"/api/providers/custom/{custom_id}")
        self.assertEqual(del_res.status_code, 200)

    def test_29_provider_models_db_and_catalog(self):
        """Тестирование сохранения и извлечения каталога моделей в БД."""
        models = [
            {
                "model_id": "test-model-1",
                "display_name": "Test Model 1",
                "description": "Fast free model",
                "context_window": "128k",
                "is_free": True,
                "is_default": True
            },
            {
                "model_id": "test-model-2",
                "display_name": "Test Model 2",
                "description": "Large paid model",
                "context_window": "200k",
                "is_free": False,
                "is_default": False
            }
        ]
        database.save_provider_models("groq", models)

        # Проверяем чтение из базы
        saved = database.get_provider_models("groq")
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0]["model_id"], "test-model-1")
        self.assertTrue(saved[0]["is_free"])

        # Проверяем фильтр только бесплатных
        free_only = database.get_provider_models("groq", free_only=True)
        self.assertEqual(len(free_only), 1)
        self.assertEqual(free_only[0]["model_id"], "test-model-1")

        # Проверяем выбор активной модели
        database.set_active_provider_model("groq", "test-model-2")
        active = database.get_active_provider_model("groq")
        self.assertEqual(active, "test-model-2")

        # Проверяем API /api/models?catalog=true
        res = self.client.get("/api/models?provider=groq&catalog=true")
        self.assertEqual(res.status_code, 200)
        catalog = res.json().get("catalog", [])
        self.assertEqual(len(catalog), 2)

    def test_30_model_sync_and_select_endpoints(self):
        """Тестирование синхронизации моделей с API и выбора активной модели."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [
                {"id": "llama-3.3-70b-versatile", "context_window": 131072},
                {"id": "mixtral-8x7b-32768", "context_window": 32768}
            ]
        }

        with patch("requests.get", return_value=mock_response):
            sync_res = self.client.post("/api/models/sync", json={"provider": "groq"})
            self.assertEqual(sync_res.status_code, 200)
            data = sync_res.json()
            self.assertEqual(data["status"], "ok")
            self.assertIn("groq", data["synced"])
            self.assertGreaterEqual(data["synced"]["groq"], 2)

        # Выбираем активную модель
        sel_res = self.client.post("/api/models/select", json={"provider": "groq", "model_id": "llama-3.3-70b-versatile"})
        self.assertEqual(sel_res.status_code, 200)
        self.assertEqual(sel_res.json()["active_model"], "llama-3.3-70b-versatile")
        self.assertEqual(database.get_active_provider_model("groq"), "llama-3.3-70b-versatile")

    def test_34_provider_operational_statuses(self):
        """Тестирование отображения статусов: активные, лимиты исчерпаны (429), не настроены."""
        from src.clients.llm import LLMAnalyzer
        
        # 1. Проверяем статус 'unconfigured' для провайдера без ключей
        res = self.client.get("/api/providers")
        self.assertEqual(res.status_code, 200)
        providers = res.json()["providers"]
        
        # Находим провайдер с ключами и без
        for p in providers:
            self.assertIn("operational_status", p)
            self.assertIn("status_info", p)
            self.assertIn("status_label", p)
            self.assertIn("status_color", p)
            if p["keys_count"] == 0:
                self.assertEqual(p["operational_status"], "unconfigured")
            elif p["is_enabled"] and p["keys_count"] > 0:
                self.assertIn(p["operational_status"], ["active", "rate_limited", "error"])

        # 2. Симулируем 429 Quota Exceeded для ключа
        test_key = "AIzaSy_fake_quota_exhausted_key"
        LLMAnalyzer.record_key_status(
            key=test_key,
            status="rate_limited",
            reason="rate_limited",
            detail="Resource has been exhausted (e.g. check quota)",
            provider_id="gemini"
        )
        
        # Детальный эндпоинт провайдера возвращает keys_detail со статусом
        st = LLMAnalyzer.get_provider_status("gemini", [test_key], is_enabled=True)
        self.assertEqual(st["status"], "rate_limited")
        self.assertEqual(st["rate_limited_keys_count"], 1)

        # 3. Тестируем POST /api/providers/probe-all
        probe_all_res = self.client.post("/api/providers/probe-all")
        self.assertEqual(probe_all_res.status_code, 200)
        probe_data = probe_all_res.json()
        self.assertIn("providers", probe_data)

    def test_35_provider_key_deletion_and_persistence(self):
        """Тестирование полного удаления API-ключей и отсутствия их воскрешения при перезапуске."""
        from src.db import database
        from src.clients.llm import LLMAnalyzer
        import sqlite3

        # 1. Добавляем ключ для openrouter
        test_key = "sk-or-v1-persistencetestkey999"
        res = self.client.post("/api/providers/openrouter", json={"api_keys": [test_key]})
        self.assertEqual(res.status_code, 200)
        prov = res.json()["provider"]
        self.assertEqual(prov["api_keys"], [test_key])

        # Проверяем запись в БД
        saved_cfg = database.get_provider_config("openrouter")
        self.assertEqual(saved_cfg["api_keys"], [test_key])

        # 2. Удаляем ключ (передаем пустой массив api_keys: [])
        del_res = self.client.post("/api/providers/openrouter", json={"api_keys": []})
        self.assertEqual(del_res.status_code, 200)
        del_prov = del_res.json()["provider"]
        self.assertEqual(del_prov["api_keys"], [])

        # Проверяем, что в providers_config и app_config ключи пусты
        saved_cfg2 = database.get_provider_config("openrouter")
        self.assertEqual(saved_cfg2["api_keys"], [])
        self.assertEqual(database.get_config_value("openrouter_api_key") or "", "")
        self.assertEqual(database.get_config_value("openrouter_api_keys") or "", "")

        # 3. Симулируем перезапуск приложения: вызываем _init_default_providers
        conn = sqlite3.connect(database.DB_PATH)
        cursor = conn.cursor()
        database._init_default_providers(cursor)
        conn.commit()
        conn.close()

        # 4. Проверяем, что ключ НЕ воскрес после перезапуска / реинициализации
        reloaded_cfg = database.get_provider_config("openrouter")
        self.assertEqual(reloaded_cfg["api_keys"], [], "Ключ воскрес после перезапуска/реинициализации БД!")

        # 5. Проверяем статус провайдера через API
        detail_res = self.client.get("/api/providers/openrouter")
        self.assertEqual(detail_res.status_code, 200)
        detail_data = detail_res.json()
        self.assertEqual(detail_data["keys_count"], 0)
        self.assertEqual(detail_data["operational_status"], "unconfigured")
        self.assertEqual(detail_data["keys_detail"], [])

    def test_36_model_tracking_and_location_non_blocking(self):
        """Тестирование трекинга моделей, мужского пола, честных ответов и сброса блокеров локации."""
        from src.pipeline.runner import format_hh_resume_to_text
        from src.clients.llm import LLMAnalyzer, VacancyAnalysis, EvaluationScores

        # 1. Проверка сохранения и выдачи analyzed_by_provider и analyzed_by_model
        database.save_vacancy(
            vacancy_id="999001",
            title="Senior Python / AI Engineer",
            company="NeuralCo",
            status="applied",
            match_score=94,
            analysis_reason="Отличный стек",
            cover_letter="Здравствуйте! Рад пообщаться. С уважением, Андрей",
            analyzed_by_provider="Mistral AI",
            analyzed_by_model="open-mistral-nemo"
        )

        res = self.client.get("/api/jobs")
        self.assertEqual(res.status_code, 200)
        jobs = res.json()["jobs"]
        saved_job = next((j for j in jobs if j["id"] == "999001"), None)
        self.assertIsNotNone(saved_job)
        self.assertEqual(saved_job["analyzed_by_provider"], "Mistral AI")
        self.assertEqual(saved_job["analyzed_by_model"], "open-mistral-nemo")

        # 2. Проверка динамического форматирования резюме: имя и пол берутся из резюме/профиля
        resume_mock_f = {"first_name": "Елена", "last_name": "Иванова", "gender": "Женский", "title": "Python Developer"}
        text_f = format_hh_resume_to_text(resume_mock_f)
        self.assertIn("ФИО: Елена Иванова", text_f)
        self.assertIn("Пол: Женский", text_f)

        resume_mock_m = {"first_name": "Алексей", "last_name": "Смирнов", "gender": "Мужской", "title": "Data Scientist"}
        text_m = format_hh_resume_to_text(resume_mock_m)
        self.assertIn("ФИО: Алексей Смирнов", text_m)
        self.assertIn("Пол: Мужской", text_m)

        # 3. Проверка промпта вопросов: женский и мужской род формируются динамически
        analyzer = LLMAnalyzer()
        q_prompt_f = analyzer._build_questions_prompt(
            resume_text="ФИО: Елена Иванова\nПол: Женский\nSenior Python",
            vacancy={"title": "Data Engineer"},
            questions=[{"id": "q1", "text": "Опыт с RLS?", "type": "single_choice"}]
        )
        self.assertIn("ПОЛ КАНДИДАТА — ЖЕНСКИЙ", q_prompt_f)
        self.assertIn("разрабатывала", q_prompt_f)
        self.assertIn("ПРЕЗУМПЦИЯ ОТСУТСТВИЯ ОПЫТА", q_prompt_f)
        self.assertIn("Row-Level Security", q_prompt_f)

        q_prompt_m = analyzer._build_questions_prompt(
            resume_text="ФИО: Алексей Смирнов\nПол: Мужской\nSenior Python",
            vacancy={"title": "Data Engineer"},
            questions=[{"id": "q1", "text": "Опыт с RLS?", "type": "single_choice"}]
        )
        self.assertIn("ПОЛ КАНДИДАТА — МУЖСКОЙ", q_prompt_m)
        self.assertIn("разрабатывал", q_prompt_m)

        # 4. Проверка промпта сопроводительного письма и мок-анализа: подпись и согласование по полу
        cl_prompt_f = analyzer._build_cover_letter_prompt(
            resume_text="ФИО: Елена Иванова\nПол: Женский\nSenior Python",
            vacancy={"title": "AI Engineer", "company": "Tech"}
        )
        self.assertIn("С уважением, Елена", cl_prompt_f)

        cl_prompt_m = analyzer._build_cover_letter_prompt(
            resume_text="ФИО: Алексей Смирнов\nПол: Мужской\nSenior Python",
            vacancy={"title": "AI Engineer", "company": "Tech"}
        )
        self.assertIn("С уважением, Алексей", cl_prompt_m)

        # Мок-анализ для женщины
        mock_analysis_f = analyzer._mock_analysis(
            vacancy={"title": "Python Engineer", "company": "TechCorp", "skills": ["Python"]},
            match_threshold=40,
            resumes=[{"first_name": "Ольга", "gender": "Женский"}]
        )
        self.assertIn("Буду рада обсудить", mock_analysis_f.cover_letter)
        self.assertIn("С уважением,\nОльга", mock_analysis_f.cover_letter)

        # 5. Проверка автоматического снятия hard blocker по локации
        analyzer_gemini = LLMAnalyzer(gemini_api_keys=["test_gemini_key"])
        database.set_system_setting("primary_provider", "gemini")
        mock_analysis_result = VacancyAnalysis(
            reasoning="Отличный стек, но офис в Москве",
            scores=EvaluationScores(stack_score=30, experience_score=25, grade_score=20, domain_score=15, format_score=0),
            has_hard_blocker=True,
            blocker_reason="Требуется очный офис в Москве",
            match_score=90,
            is_match=False
        )
        with patch.object(analyzer_gemini, "_call_gemini", return_value=mock_analysis_result):
            res_analysis = analyzer_gemini.analyze_vacancy(
                resume_text="Senior Python Dev",
                vacancy={"title": "Python Dev", "location": "Москва, офис"},
                threshold=70
            )
            # Блокировка по офису должна быть снята, чтобы кандидат мог откликнуться и договориться
            self.assertFalse(res_analysis.has_hard_blocker)
            self.assertIsNone(res_analysis.blocker_reason)
            self.assertTrue(res_analysis.is_match)
            self.assertEqual(res_analysis.match_score, 90)

if __name__ == "__main__":
    unittest.main()



