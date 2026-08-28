import os
import sys
import sqlite3
import unittest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
import database
import config

TEST_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_frontend_api_db.db")

class TestFrontendAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Очищаем старую БД если есть
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
            
        # Подменяем путь к БД в модуле database
        database.DB_PATH = TEST_DB_PATH
        database.init_db()
        
        # Наполняем БД тестовыми данными
        conn = sqlite3.connect(TEST_DB_PATH)
        cursor = conn.cursor()
        for i in range(5):
            cursor.execute(
                "INSERT INTO processed_vacancies (id, title, company, status, match_score, processed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (f"e2e_vac_{i}", f"Python Dev {i}", f"Company {i}", "new" if i < 3 else "failed", 70 + i * 5, f"2026-08-12 10:00:0{i}")
            )
        conn.commit()
        conn.close()

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        # Удаляем тестовую БД
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)

    def test_root_html_renders(self):
        """Проверка, что главная страница отдается и содержит нужные ID элементов."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        
        html = response.text
        # Проверяем наличие ключевых элементов интерфейса
        self.assertIn('id="vacancy-modal"', html, "Модальное окно должно присутствовать в HTML")
        self.assertIn('id="modal-close-btn"', html, "Кнопка закрытия модалки должна присутствовать")
        self.assertIn('id="dryrun-toggle"', html, "Тумблер dry-run должен присутствовать")
        self.assertIn('id="dryrun-badge"', html, "Бейдж dry-run должен присутствовать")
        self.assertIn('id="start-scan-btn"', html, "Кнопка запуска должна присутствовать")
        self.assertIn('class="vacancy-card"', html, "Должен быть placeholder для карточки вакансии")

    def test_api_jobs_endpoint(self):
        """Проверка API эндпоинта для получения вакансий."""
        response = self.client.get("/api/jobs?status=all&limit=20&offset=0")
        self.assertEqual(response.status_code, 200)
        
        data = response.json()
        self.assertIn("jobs", data)
        self.assertIn("stats", data)
        
        jobs = data["jobs"]
        self.assertEqual(len(jobs), 5, "API должно вернуть 5 тестовых вакансий")
        self.assertEqual(jobs[0]["id"], "e2e_vac_4")
        
        stats = data["stats"]
        self.assertEqual(stats["total"], 5)
        self.assertEqual(stats["failed"], 2)

    def test_api_settings_endpoint(self):
        """Проверка API эндпоинта настроек."""
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        
        data = response.json()
        self.assertIn("queries", data)
        self.assertIn("area_id", data)
        self.assertIn("dry_run", data)

if __name__ == "__main__":
    unittest.main()
