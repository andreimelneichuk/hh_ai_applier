import os
import sys
import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.db.database as database
from src.api.app import app
from src.api.security import API_TOKEN, TOKEN_HEADER
from src.clients.llm import LLMAnalyzer

TEST_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_vacancy_persistence_db.db")

QUESTIONS = [
    {"id": "q_city", "question_text": "Город?", "answer": "Москва", "confidence": 95, "requires_user_input": False},
    {"id": "q_salary", "question_text": "Зарплата?", "answer": "", "confidence": 30, "requires_user_input": True},
]


class TestVacancyPersistence(unittest.TestCase):
    """Правки пользователя (письмо, ответы, статус) сохраняются в БД и не теряются."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app, base_url="http://127.0.0.1", headers={TOKEN_HEADER: API_TOKEN})

    def setUp(self):
        database.DB_PATH = TEST_DB_PATH
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        database.init_db()
        database.save_vacancy(
            vacancy_id="700", title="Python Dev", company="Acme", status="needs_answers",
            match_score=85, cover_letter="Старое письмо", questions_data=json.dumps(QUESTIONS, ensure_ascii=False),
            applied_resume_id="res_42", applied_resume_title="Backend"
        )

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)

    def _questions(self, vacancy_id="700"):
        return json.loads(database.get_vacancy(vacancy_id)["questions_data"])

    def test_save_draft_persists_answers(self):
        res = self.client.post("/api/vacancies/700/save-draft", json={
            "cover_letter": "Новое письмо",
            "answers": {"q_salary": "200 000 ₽", "q_city": "Москва"}
        })
        self.assertEqual(res.status_code, 200)
        row = database.get_vacancy("700")
        self.assertEqual(row["cover_letter"], "Новое письмо")
        q = {item["id"]: item for item in self._questions()}
        self.assertEqual(q["q_salary"]["answer"], "200 000 ₽")
        self.assertTrue(q["q_salary"]["answered_by_user"])
        self.assertFalse(q["q_salary"]["requires_user_input"])
        # Неизменённый ответ ИИ не помечается как пользовательский
        self.assertNotIn("answered_by_user", q["q_city"])

    def test_save_draft_unknown_vacancy_404(self):
        res = self.client.post("/api/vacancies/does_not_exist/save-draft", json={"cover_letter": "x"})
        self.assertEqual(res.status_code, 404)

    def test_apply_persists_answers_and_already_applied(self):
        mock_hh = MagicMock()
        mock_hh.apply_to_vacancy.return_value = (True, "ALREADY_APPLIED")
        with patch("src.api.routes.vacancies.HHBrowserClient", return_value=mock_hh):
            res = self.client.post("/api/apply", json={
                "vacancy_id": "700", "resume_id": "res_42",
                "cover_letter": "Итоговое письмо", "answers": {"q_salary": "180 000 ₽"}
            })
        self.assertEqual(res.status_code, 200)
        row = database.get_vacancy("700")
        self.assertEqual(row["status"], "already_applied")
        self.assertEqual(row["cover_letter"], "Итоговое письмо")
        self.assertEqual({q["id"]: q["answer"] for q in self._questions()}["q_salary"], "180 000 ₽")


class TestLegacyColumnOrder(unittest.TestCase):
    """В БД старых версий processed_at стоит 8-й колонкой — чтение по позиции брало не те данные."""

    def setUp(self):
        database.DB_PATH = TEST_DB_PATH
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        conn = sqlite3.connect(TEST_DB_PATH)
        conn.execute("""
            CREATE TABLE processed_vacancies (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL, status TEXT NOT NULL,
                match_score INTEGER, analysis_reason TEXT, cover_letter TEXT,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()
        database.init_db()  # миграции добавят остальные колонки в конец
        database.save_vacancy(
            vacancy_id="800", title="Go Dev", company="Corp", status="new",
            questions_data=json.dumps(QUESTIONS), applied_resume_id="res_legacy"
        )

    def tearDown(self):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)

    def test_generate_cover_letter_uses_applied_resume(self):
        client = TestClient(app, base_url="http://127.0.0.1", headers={TOKEN_HEADER: API_TOKEN})
        mock_hh = MagicMock()
        mock_hh.get_vacancy_details.return_value = {"title": "Go Dev", "company": "Corp", "description": "Go"}
        with patch("src.api.routes.vacancies.HHBrowserClient", return_value=mock_hh), \
             patch("src.api.routes.vacancies.load_candidate_resumes",
                   return_value=[{"id": "res_legacy", "title": "Go", "text": "Go developer"}]) as load_resumes, \
             patch.object(LLMAnalyzer, "generate_cover_letter", return_value="Письмо"):
            res = client.post("/api/generate-cover-letter/800")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(load_resumes.call_args.args[1], "res_legacy")
        self.assertEqual(database.get_vacancy("800")["cover_letter"], "Письмо")


if __name__ == "__main__":
    unittest.main()
