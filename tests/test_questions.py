import os
import sys
import unittest
import json
import sqlite3
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import src.db.database as database
from src.api.app import app
from src.api.security import API_TOKEN, TOKEN_HEADER
from src.clients.browser import HHBrowserClient
from src.clients.llm import LLMAnalyzer, QuestionAnswer, QuestionsAnalysisResult, VacancyAnalysis

TEST_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_questions_db.db")
os.environ["HH_DB_PATH"] = TEST_DB_PATH
database.DB_PATH = TEST_DB_PATH

class TestQuestionsAndAnswersSupport(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        database.DB_PATH = TEST_DB_PATH
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except Exception:
                pass
        database.init_db()
        cls.client = TestClient(app, base_url="http://127.0.0.1", headers={TOKEN_HEADER: API_TOKEN})

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
        conn = sqlite3.connect(TEST_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM processed_vacancies")
        cursor.execute("DELETE FROM user_profile_answers")
        cursor.execute("DELETE FROM app_config")
        conn.commit()
        conn.close()

    def test_01_user_profile_answers_db_crud(self):
        """Тестирование CRUD для таблицы user_profile_answers."""
        database.set_user_profile_answer("location_city", "Город проживания", "Москва / готов к переезду в Пермь")
        database.set_user_profile_answer("salary_expectation", "Зарплатные ожидания", "от 250 000 руб.")

        answers = database.get_user_profile_answers()
        self.assertEqual(len(answers), 2)
        ans_dict = {a["key"]: a["answer"] for a in answers}
        self.assertIn("location_city", ans_dict)
        self.assertEqual(ans_dict["salary_expectation"], "от 250 000 руб.")

        # Обновляем существующий
        database.set_user_profile_answer("salary_expectation", "Зарплатные ожидания", "от 300 000 руб.")
        answers2 = database.get_user_profile_answers()
        ans_dict2 = {a["key"]: a["answer"] for a in answers2}
        self.assertEqual(ans_dict2["salary_expectation"], "от 300 000 руб.")

        # Удаляем ответ
        database.delete_user_profile_answer("location_city")
        answers3 = database.get_user_profile_answers()
        self.assertEqual(len(answers3), 1)
        self.assertEqual(answers3[0]["key"], "salary_expectation")

    def test_02_user_profile_answers_api(self):
        """Тестирование API endpoints для работы с базой ответов профиля."""
        # GET empty
        res = self.client.get("/api/user-profile-answers")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["answers"], [])

        # POST new answer
        payload = {
            "key": "it_accreditation",
            "question_hint": "IT-аккредитация",
            "answer": "Да, важна отсрочка от армии"
        }
        res = self.client.post("/api/user-profile-answers", json=payload)
        self.assertEqual(res.status_code, 200)

        # GET check
        res = self.client.get("/api/user-profile-answers")
        answers = res.json()["answers"]
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["key"], "it_accreditation")
        self.assertEqual(answers[0]["answer"], "Да, важна отсрочка от армии")

        # DELETE
        res = self.client.delete("/api/user-profile-answers/it_accreditation")
        self.assertEqual(res.status_code, 200)
        res = self.client.get("/api/user-profile-answers")
        self.assertEqual(len(res.json()["answers"]), 0)

    def test_03_database_questions_data_and_needs_answers_status(self):
        """Тестирование сохранения questions_data и фильтрации needs_answers в базе данных."""
        questions_payload = json.dumps([
            {
                "id": "task_1",
                "question_text": "Какой у вас опыт в FastAPI?",
                "answer": "Более 4 лет коммерческой разработки",
                "confidence": 95,
                "requires_user_input": False,
                "reasoning": "В резюме указан FastAPI"
            }
        ], ensure_ascii=False)

        database.save_vacancy(
            vacancy_id="vac_needs_ans_1",
            title="Senior Python Backend",
            company="Tech Corp",
            status="needs_answers",
            match_score=85,
            analysis_reason="Отличное совпадение, но есть вопросы работодателя",
            cover_letter="Добрый день! Готов присоединиться...",
            questions_data=questions_payload
        )

        database.save_vacancy(
            vacancy_id="vac_applied_2",
            title="Middle Python",
            company="Fintech Inc",
            status="applied",
            match_score=90,
            analysis_reason="Совпадение",
            cover_letter="Здравствуйте!"
        )

        rows = database.get_processed_paginated(status="needs_answers", limit=10, offset=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "vac_needs_ans_1")
        self.assertEqual(rows[0][3], "needs_answers")
        self.assertIn("FastAPI", rows[0][7])

        count_needs = database.get_processed_count("needs_answers")
        self.assertEqual(count_needs, 1)

        res = self.client.get("/api/jobs?status=all")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["stats"]["needs_answers"], 1)
        self.assertEqual(data["stats"]["total"], 2)

    def test_04_llm_analyzer_answer_questions(self):
        """Тестирование генерации ответов на вопросы с помощью LLMAnalyzer."""
        analyzer = LLMAnalyzer(gemini_api_keys=["test_gemini_key"])
        resume_text = "Python разработчик, 5 лет опыта. Стек: FastAPI, PostgreSQL, Docker, Asyncio. Город: Пермь. ЗП: от 200 000 руб."
        vacancy = {
            "title": "Backend Python Developer",
            "company": "Инновации",
            "description": "Ищем разработчика со знанием FastAPI и SQL"
        }
        questions = [
            {
                "id": "task_1",
                "text": "В каком городе вы проживаете и готовы ли к гибриду?",
                "type": "text"
            },
            {
                "id": "task_2",
                "text": "Сколько лет коммерческого опыта с Python?",
                "type": "text"
            }
        ]
        user_saved_answers = [
            {"key": "location_city", "question_hint": "Город", "answer": "Пермь, готов к гибриду"}
        ]

        # 1. Тестируем mock questions analysis
        mock_res = analyzer._mock_questions_analysis(questions, user_saved_answers)
        self.assertIsInstance(mock_res, QuestionsAnalysisResult)
        self.assertEqual(len(mock_res.answers), 2)
        self.assertIn("Пермь", mock_res.answers[0].answer)

        # 2. Тестируем с моком LLM вызова
        expected_ans = QuestionsAnalysisResult(
            answers=[
                QuestionAnswer(
                    id="task_1",
                    question_text=questions[0]["text"],
                    answer="Пермь, готов к гибриду",
                    confidence=95,
                    requires_user_input=False,
                    reasoning="Указано в профиле"
                ),
                QuestionAnswer(
                    id="task_2",
                    question_text=questions[1]["text"],
                    answer="5 лет коммерческого опыта",
                    confidence=90,
                    requires_user_input=False,
                    reasoning="Указано в резюме"
                )
            ],
            all_confident=True
        )

        with patch.object(LLMAnalyzer, "_call_gemini_questions", return_value=expected_ans):
            result = analyzer.answer_questions(
                resume_text=resume_text,
                vacancy=vacancy,
                questions=questions,
                user_saved_answers=user_saved_answers
            )

            self.assertIsInstance(result, QuestionsAnalysisResult)
            self.assertEqual(len(result.answers), 2)
            self.assertTrue(result.all_confident)
            self.assertEqual(result.answers[0].answer, "Пермь, готов к гибриду")
            self.assertEqual(result.answers[1].answer, "5 лет коммерческого опыта")

    @patch.object(HHBrowserClient, "apply_to_vacancy")
    def test_05_api_apply_with_answers(self, mock_apply):
        """Тестирование вызова POST /api/apply с ответами на вопросы."""
        mock_apply.return_value = (True, "")

        payload = {
            "vacancy_id": "136384597",
            "resume_id": "test_resume_id",
            "cover_letter": "Здравствуйте! Буду рад работать у вас.",
            "answers": {
                "task_1": "Пермь",
                "task_2": "5 лет опыта"
            }
        }

        res = self.client.post("/api/apply", json=payload)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "ok")

        mock_apply.assert_called_once_with(
            vacancy_id="136384597",
            resume_title_or_id="test_resume_id",
            cover_letter="Здравствуйте! Буду рад работать у вас.",
            answers={"task_1": "Пермь", "task_2": "5 лет опыта"},
            dry_run=False
        )

    def test_06_quick_apply_auto_send(self):
        """Тестирование Быстрого отклика по ссылке с автоматической отправкой (уверенные ответы)."""
        mock_details = {
            "title": "Python Lead Developer",
            "company": "SuperTech",
            "description": "FastAPI, PostgreSQL, Redis"
        }
        mock_questions = [
            {"id": "task_1", "text": "Город проживания?", "type": "text"}
        ]
        mock_q_res = QuestionsAnalysisResult(
            answers=[
                QuestionAnswer(
                    id="task_1",
                    question_text="Город проживания?",
                    answer="Пермь",
                    confidence=95,
                    requires_user_input=False,
                    reasoning="Из профиля"
                )
            ],
            all_confident=True
        )

        mock_hh = MagicMock()
        mock_hh.get_vacancy_details.return_value = mock_details
        mock_hh.get_resume.return_value = None
        mock_hh.get_vacancy_questions.return_value = mock_questions
        mock_hh.apply_to_vacancy.return_value = (True, "")
        database.set_config_value("dry_run", "false")

        mock_analysis = VacancyAnalysis(
            match_score=95,
            is_match=True,
            reasoning="Отличное совпадение",
            cover_letter="Добрый день! Готов присоединиться к команде."
        )

        with patch("src.api.routes.vacancies.HHBrowserClient", return_value=mock_hh), \
             patch("src.api.routes.vacancies.load_resume_text", return_value="Senior Python Developer, 6 лет"), \
             patch.object(LLMAnalyzer, "analyze_vacancy", return_value=mock_analysis), \
             patch.object(LLMAnalyzer, "answer_questions", return_value=mock_q_res):
            
            # Отправляем URL вакансии
            target_url = "https://perm.hh.ru/applicant/vacancy_response?vacancyId=136384597&startedWithQuestion=false"
            res = self.client.post("/api/quick-apply", json={"url_or_id": target_url})
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["status"], "applied")
            self.assertEqual(data["vacancy_id"], "136384597")

            # Проверяем, что в БД статус applied
            saved = database.get_vacancy("136384597")
            self.assertIsNotNone(saved)
            self.assertEqual(saved[3], "applied")

    def test_07_quick_apply_needs_review(self):
        """Тестирование Быстрого отклика, когда ИИ не уверен и переводит в needs_answers для модалки."""
        mock_details = {
            "title": "C++ / Python Developer",
            "company": "GameCorp",
            "description": "3D engine development"
        }
        mock_questions = [
            {"id": "q_test", "text": "Готовы ли вы выйти в офис в Сербии со следующей недели?", "type": "text"}
        ]
        mock_q_res = QuestionsAnalysisResult(
            answers=[
                QuestionAnswer(
                    id="q_test",
                    question_text="Готовы ли вы выйти в офис в Сербии со следующей недели?",
                    answer="Требуется обсудить условия",
                    confidence=50,
                    requires_user_input=True,
                    reasoning="Релокация в другую страну"
                )
            ],
            all_confident=False
        )
        mock_analysis = VacancyAnalysis(
            match_score=80,
            is_match=True,
            reasoning="Хорошее совпадение",
            cover_letter="Здравствуйте!"
        )

        mock_hh = MagicMock()
        mock_hh.get_vacancy_details.return_value = mock_details
        mock_hh.get_resume.return_value = None
        mock_hh.get_vacancy_questions.return_value = mock_questions

        with patch("src.api.routes.vacancies.HHBrowserClient", return_value=mock_hh), \
             patch("src.api.routes.vacancies.load_resume_text", return_value="Python Developer"), \
             patch.object(LLMAnalyzer, "analyze_vacancy", return_value=mock_analysis), \
             patch.object(LLMAnalyzer, "answer_questions", return_value=mock_q_res):
            
            res = self.client.post("/api/quick-apply", json={"url_or_id": "77788899"})
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["status"], "needs_answers")
            self.assertEqual(data["vacancy_id"], "77788899")

            # Проверяем, что в БД статус needs_answers
            saved = database.get_vacancy("77788899")
            self.assertIsNotNone(saved)
            self.assertEqual(saved[3], "needs_answers")

    def _quick_apply_mocks(self, score, cover_letter="Письмо от ИИ"):
        mock_hh = MagicMock()
        mock_hh.get_vacancy_details.return_value = {"title": "Go Developer", "company": "Corp", "description": "Go, k8s"}
        mock_hh.get_resume.return_value = None
        mock_hh.get_vacancy_questions.return_value = []
        mock_hh.apply_to_vacancy.return_value = (True, "")
        analysis = VacancyAnalysis(match_score=score, is_match=score >= 70, reasoning="r", cover_letter=cover_letter)
        return mock_hh, analysis

    def test_08_quick_apply_below_threshold_not_sent_without_force(self):
        """Вакансия ниже порога не отправляется без force и помечается ignored."""
        database.set_config_value("dry_run", "false")
        database.set_config_value("match_threshold", "70")
        mock_hh, analysis = self._quick_apply_mocks(40)
        with patch("src.api.routes.vacancies.HHBrowserClient", return_value=mock_hh), \
             patch("src.api.routes.vacancies.load_resume_text", return_value="Resume"), \
             patch.object(LLMAnalyzer, "analyze_vacancy", return_value=analysis):
            res = self.client.post("/api/quick-apply", json={"url_or_id": "55500011"})
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["status"], "not_eligible")
            self.assertEqual(data["threshold"], 70)
            mock_hh.apply_to_vacancy.assert_not_called()
            mock_hh.get_vacancy_questions.assert_not_called()
            self.assertEqual(database.get_vacancy("55500011")[3], "ignored")

            # С подтверждением пользователя (force) — отклик уходит с переданным письмом
            res = self.client.post("/api/quick-apply", json={"url_or_id": "55500011", "force": True, "cover_letter": "Моё письмо"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["status"], "applied")
            sent_letter = mock_hh.apply_to_vacancy.call_args.kwargs["cover_letter"]
            self.assertTrue(sent_letter.startswith("Моё письмо"))

    def test_09_quick_apply_reports_already_applied(self):
        """Если hh.ru сообщает о существующем отклике, API возвращает already_applied."""
        database.set_config_value("dry_run", "false")
        database.set_config_value("match_threshold", "70")
        mock_hh, analysis = self._quick_apply_mocks(90)
        mock_hh.apply_to_vacancy.return_value = (True, "ALREADY_APPLIED")
        with patch("src.api.routes.vacancies.HHBrowserClient", return_value=mock_hh), \
             patch("src.api.routes.vacancies.load_resume_text", return_value="Resume"), \
             patch.object(LLMAnalyzer, "analyze_vacancy", return_value=analysis):
            res = self.client.post("/api/quick-apply", json={"url_or_id": "55500022"})
            self.assertEqual(res.json()["status"], "already_applied")

    def test_10_browser_endpoints_rejected_while_pipeline_running(self):
        """Пока идёт сканирование, эндпоинты с браузером отвечают 409 и не запускают второй Chrome."""
        import src.api.state as state
        state.pipeline_status["is_running"] = True
        try:
            with patch("src.api.routes.vacancies.HHBrowserClient") as browser_cls:
                res = self.client.post("/api/quick-apply", json={"url_or_id": "55500033"})
                self.assertEqual(res.status_code, 409)
                browser_cls.assert_not_called()
        finally:
            state.pipeline_status["is_running"] = False

    def test_11_user_errors_are_4xx(self):
        """Ошибки из-за данных пользователя и hh.ru не маскируются под 500."""
        database.set_config_value("dry_run", "false")
        database.set_config_value("match_threshold", "70")

        # Нет ни одного резюме — 400
        mock_hh, analysis = self._quick_apply_mocks(90)
        mock_hh.get_my_resumes.return_value = []
        with patch("src.api.routes.vacancies.HHBrowserClient", return_value=mock_hh), \
             patch("src.api.routes.vacancies.load_resume_text", return_value=""):
            res = self.client.post("/api/quick-apply", json={"url_or_id": "55500044"})
            self.assertEqual(res.status_code, 400)
            self.assertIn("Резюме не найдено", res.json()["detail"])

        # Вакансия недоступна на hh.ru — 404
        mock_hh.get_vacancy_details.return_value = None
        with patch("src.api.routes.vacancies.HHBrowserClient", return_value=mock_hh), \
             patch("src.api.routes.vacancies.load_resume_text", return_value="Resume"):
            res = self.client.post("/api/quick-apply", json={"url_or_id": "55500044"})
            self.assertEqual(res.status_code, 404)

        # hh.ru отклонил отклик — 502, статус failed сохранён
        mock_hh, analysis = self._quick_apply_mocks(90)
        mock_hh.apply_to_vacancy.return_value = (False, "Кнопка отклика не найдена")
        with patch("src.api.routes.vacancies.HHBrowserClient", return_value=mock_hh), \
             patch("src.api.routes.vacancies.load_resume_text", return_value="Resume"), \
             patch.object(LLMAnalyzer, "analyze_vacancy", return_value=analysis):
            res = self.client.post("/api/quick-apply", json={"url_or_id": "55500055"})
            self.assertEqual(res.status_code, 502)
            self.assertIn("Кнопка отклика не найдена", res.json()["detail"])
            self.assertEqual(database.get_vacancy("55500055")["status"], "failed")

    def test_12_reanalyze_unavailable_vacancy_is_404(self):
        """Переоценка вакансии, которая исчезла с hh.ru, — 404, пайплайн освобождается."""
        import src.api.state as state
        database.save_vacancy(vacancy_id="55500066", title="Old", company="C", status="failed",
                              match_score=0, analysis_reason="err", cover_letter="")
        mock_hh, _ = self._quick_apply_mocks(90)
        mock_hh.get_vacancy_details.return_value = None
        with patch("src.api.routes.vacancies.HHBrowserClient", return_value=mock_hh), \
             patch("src.api.routes.vacancies.load_resume_text", return_value="Resume"):
            res = self.client.post("/api/reanalyze/55500066")
            self.assertEqual(res.status_code, 404)
        self.assertFalse(state.pipeline_status["is_running"])

    def test_13_pipeline_does_not_apply_with_local_resume(self):
        """Автоматический сценарий не откликается без резюме из профиля HH."""
        from src.pipeline.processing import decide_and_apply
        mock_hh, analysis = self._quick_apply_mocks(90)
        decision = decide_and_apply(mock_hh, MagicMock(), "1", {}, analysis,
                                    [{"id": "local", "title": "Локальное", "text": "t"}],
                                    threshold=70, dry_run=False)
        self.assertEqual(decision.status, "failed")
        mock_hh.apply_to_vacancy.assert_not_called()

if __name__ == "__main__":
    unittest.main()
