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
        analyzer = LLMAnalyzer()
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

if __name__ == "__main__":
    unittest.main()
