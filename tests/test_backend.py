import os
import sys
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

        self.assertIn("СТРОГИЙ КОНТРОЛЬ ГРЕЙДА И РЕЛЕВАНТНОГО ОПЫТА", database.DEFAULT_SYSTEM_PROMPT)
        self.assertIn("РЕЛЕВАНТНЫЙ коммерческий опыт", database.DEFAULT_SYSTEM_PROMPT)
        self.assertIn("МЕСТО РАБОТЫ, ФОРМАТ И ЛОКАЦИЯ", database.DEFAULT_SYSTEM_PROMPT)
        self.assertIn("ЗАНЯТОСТЬ, ГРАФИК И ЧАСЫ РАБОТЫ", database.DEFAULT_SYSTEM_PROMPT)
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

if __name__ == "__main__":
    unittest.main()


