import os
import sys
import json
import time
import socket
import sqlite3
import threading
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn
from playwright.sync_api import sync_playwright

import src.db.database as database
from src.core.config import Config
from src.api.app import app
import src.pipeline.runner as runner
from src.clients.browser import HHBrowserClient
from src.clients.llm import LLMAnalyzer, VacancyAnalysis, EvaluationScores

TEST_E2E_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_e2e_browser.db")

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

class TestE2EComprehensive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 1. Мокаем внешние вызовы (HH.ru и LLM status) для быстрого запуска и независимости от сети
        import src.api.state as app_state
        app_state.cached_login_status = True
        app_state.cached_user_info = {"first_name": "Тест", "last_name": "Пользователь"}
        app_state.last_login_check_time = time.time() + 999999

        cls.patcher_resumes = patch.object(
            HHBrowserClient, "get_my_resumes", 
            return_value=[{"id": "test_r1", "title": "Senior Python Engineer", "url": "https://hh.ru/resume/1"}]
        )
        cls.patcher_resumes.start()

        cls.patcher_login = patch.object(HHBrowserClient, "is_logged_in", return_value=True)
        cls.patcher_login.start()

        cls.patcher_info = patch.object(
            HHBrowserClient, "get_my_info",
            return_value={"first_name": "Тест", "last_name": "Пользователь"}
        )
        cls.patcher_info.start()

        cls.patcher_models = patch.object(
            LLMAnalyzer, "check_availability", 
            return_value={
                "status": "ok",
                "available": 1,
                "total": 1,
                "gemini": {"available": 1, "total": 1, "keys": [{"key": "mock_key", "status": "active"}]},
                "mistral": {"available": 0, "total": 0, "keys": []},
                "openai": {"available": 0, "total": 0, "keys": []},
            }
        )
        cls.patcher_models.start()

        cls.patcher_available_models = patch.object(
            LLMAnalyzer, "get_available_models",
            return_value=["gemini-3.6-flash", "gemini-2.5-flash"]
        )
        cls.patcher_available_models.start()

        # 2. Настраиваем тестовую БД
        if os.path.exists(TEST_E2E_DB):
            try:
                os.remove(TEST_E2E_DB)
            except Exception:
                pass
        
        database.DB_PATH = TEST_E2E_DB
        os.environ["HH_DB_PATH"] = TEST_E2E_DB
        database.init_db()

        # Заполняем тестовыми вакансиями с 5 шкалами
        conn = sqlite3.connect(TEST_E2E_DB)
        cursor = conn.cursor()
        
        # 1. Вакансия с 65% (не проходит порог 70%)
        scores_65 = json.dumps({
            "stack_score": 20,
            "experience_score": 15,
            "grade_score": 15,
            "domain_score": 10,
            "format_score": 5,
            "total": 65,
            "has_hard_blocker": False,
            "blocker_reason": ""
        })
        cursor.execute(
            """INSERT INTO processed_vacancies 
               (id, title, company, status, match_score, analysis_reason, scores_data, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("vac_65", "Python Backend Dev", "Alpha Corp", "failed", 65, 
             "Недостаточно опыта в распределенных системах", scores_65, "2026-09-05 10:00:00")
        )

        # 2. Вакансия с 85% (проходит порог 70%)
        scores_85 = json.dumps({
            "stack_score": 26,
            "experience_score": 22,
            "grade_score": 18,
            "domain_score": 11,
            "format_score": 8,
            "total": 85,
            "has_hard_blocker": False,
            "blocker_reason": ""
        })
        cursor.execute(
            """INSERT INTO processed_vacancies 
               (id, title, company, status, match_score, analysis_reason, scores_data, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("vac_85", "Senior Python / FastApi Engineer", "Beta Tech", "new", 85, 
             "Отличный стек FastApi, релевантный опыт", scores_85, "2026-09-05 10:05:00")
        )

        # 3. Вакансия с блокером (90% стек, но онсайт в Новосибирске)
        scores_blocker = json.dumps({
            "stack_score": 28,
            "experience_score": 23,
            "grade_score": 19,
            "domain_score": 12,
            "format_score": 8,
            "total": 90,
            "has_hard_blocker": True,
            "blocker_reason": "Строго онсайт в Новосибирске без релокационного пакета"
        })
        cursor.execute(
            """INSERT INTO processed_vacancies 
               (id, title, company, status, match_score, analysis_reason, scores_data, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("vac_blocker", "Lead Python Architect", "Gamma Cloud", "failed", 90, 
             "Стек идеальный, но есть непреодолимый блокер по локации", scores_blocker, "2026-09-05 10:10:00")
        )
        
        conn.commit()
        conn.close()

        # 3. Запускаем локальный веб-сервер в фоне для Playwright
        cls.port = get_free_port()
        cls.server_url = f"http://127.0.0.1:{cls.port}"
        
        config = uvicorn.Config(app=app, host="127.0.0.1", port=cls.port, log_level="error")
        cls.server = uvicorn.Server(config)
        cls.server_thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.server_thread.start()

        # Ждем старта сервера
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.1):
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.patcher_resumes.stop()
        cls.patcher_login.stop()
        cls.patcher_info.stop()
        cls.patcher_models.stop()
        cls.patcher_available_models.stop()
        cls.server.should_exit = True
        if os.path.exists(TEST_E2E_DB):
            try:
                os.remove(TEST_E2E_DB)
            except Exception:
                pass

    def test_e2e_pipeline_strict_threshold_and_blockers(self):
        """
        E2E Тест конвейера обработки:
        1. 65% при пороге 70% ДОЛЖНА БЫТЬ ОТСЕЯНА (несмотря на is_match=True от LLM).
        2. 90% с жестким блокером ДОЛЖНА БЫТЬ ОТСЕЯНА.
        3. 85% без блокеров ДОЛЖНА БЫТЬ ОДОБРЕНА.
        """
        mock_vac_65 = {
            "id": "mock_65", "title": "Dev 65", "company": "Co 65", 
            "url": "https://hh.ru/vacancy/65", "description": "Django, Postgres"
        }
        mock_vac_blocker = {
            "id": "mock_blocker", "title": "Lead 90 Onsite", "company": "Co Blocker", 
            "url": "https://hh.ru/vacancy/blocker", "description": "Onsite only Novosibirsk"
        }
        mock_vac_85 = {
            "id": "mock_85", "title": "Senior 85 Remote", "company": "Co 85", 
            "url": "https://hh.ru/vacancy/85", "description": "Python, FastApi, Remote"
        }

        # 1. Вакансия 65%
        analysis_65 = VacancyAnalysis(
            reasoning="Стек частично подходит, но не хватает опыта в микросервисах",
            scores=EvaluationScores(
                stack_score=20, experience_score=15, grade_score=15, domain_score=10, format_score=5
            ),
            has_hard_blocker=False,
            blocker_reason="",
            is_match=True,  # Модель галлюцинирует True!
            match_score=65,
            cover_letter="Тестовое письмо"
        )

        # 2. Вакансия с блокером
        analysis_blocker = VacancyAnalysis(
            reasoning="Отличный стек, но требует переезд в Новосибирск",
            scores=EvaluationScores(
                stack_score=28, experience_score=23, grade_score=19, domain_score=12, format_score=8
            ),
            has_hard_blocker=True,
            blocker_reason="Требуется онсайт в Новосибирске",
            is_match=True,
            match_score=90,
            cover_letter="Тестовое письмо"
        )

        # 3. Вакансия 85%
        analysis_85 = VacancyAnalysis(
            reasoning="Идеальное совпадение стека и удалённого формата",
            scores=EvaluationScores(
                stack_score=26, experience_score=22, grade_score=18, domain_score=11, format_score=8
            ),
            has_hard_blocker=False,
            blocker_reason="",
            is_match=True,
            match_score=85,
            cover_letter="Тестовое письмо"
        )

        # Запускаем конвейер в режиме dry-run с порогом 70%
        with patch("src.pipeline.runner.HHBrowserClient") as mock_browser_cls, \
             patch("src.pipeline.runner.LLMAnalyzer") as mock_llm_cls:

            mock_browser = MagicMock()
            mock_browser_cls.return_value = mock_browser
            mock_browser.is_logged_in.return_value = True
            mock_browser.get_my_resumes.return_value = [{"id": "res_1", "title": "Python Dev"}]
            mock_browser.get_resume.return_value = {"title": "Python Dev", "skills": ["Python", "FastAPI"], "experience": []}
            mock_browser.get_recommended_vacancies.return_value = [mock_vac_65, mock_vac_blocker, mock_vac_85]
            mock_browser.get_vacancy_details.side_effect = lambda vid: {"title": f"Dev {vid}", "company": "Co", "description": "Job details", "already_applied": False}
            mock_browser.has_employer_questions.return_value = False
            mock_browser.get_vacancy_questions.return_value = []

            mock_llm = MagicMock()
            mock_llm_cls.return_value = mock_llm
            # Возвращаем анализы по очереди
            mock_llm.analyze_vacancy.side_effect = [analysis_65, analysis_blocker, analysis_85]

            # Конфиг: порог 70%
            Config.MATCH_THRESHOLD = 70
            database.set_config_value("match_threshold", "70")

            # Запуск конвейера
            res = runner.run_pipeline(max_process=10, dry_run=True)
            stats = res["stats"]

            # Проверяем статистику конвейера
            self.assertEqual(stats["processed"], 3)
            # Из 3 вакансий подошла только 1 (85%)! 65% и блокер отсеяны!
            self.assertEqual(stats["matched"], 1)
            self.assertEqual(stats["ignored"], 2)

            # Проверяем состояние в базе данных
            vac_65_row = database.get_vacancy("mock_65")
            self.assertIsNotNone(vac_65_row)
            self.assertEqual(vac_65_row[3], "ignored", "Вакансия 65% должна иметь статус ignored")
            self.assertEqual(vac_65_row[4], 65)

            vac_blocker_row = database.get_vacancy("mock_blocker")
            self.assertIsNotNone(vac_blocker_row)
            self.assertEqual(vac_blocker_row[3], "ignored", "Вакансия с блокером должна иметь статус ignored")
            self.assertEqual(vac_blocker_row[4], 90)

            vac_85_row = database.get_vacancy("mock_85")
            self.assertIsNotNone(vac_85_row)
            self.assertEqual(vac_85_row[3], "new", "Вакансия 85% в dry-run должна иметь статус new")
            self.assertEqual(vac_85_row[4], 85)

    def test_e2e_playwright_browser_ui(self):
        """
        E2E Браузерный тест через Playwright:
        1. Открытие страницы приложения в Google Chrome.
        2. Проверка отображения карточек и тултипов 5 шкал.
        3. Клик по карточке вакансии -> проверка отображения блока 5 шкал в модалке.
        4. Проверка корректности шкал и предупреждения о жестком блокере.
        5. Открытие системных настроек и модального окна лимитов -> интерактивная проверка скрытия/показа полей.
        """
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch(headless=True)
                except Exception:
                    browser = p.chromium.launch(channel="chrome", headless=True)
                page = browser.new_page()
                
                # 1. Открываем веб-интерфейс
                page.goto(self.server_url)
            page.wait_for_selector("#vacancies-container")
            
            # Ждем загрузки карточек вакансий
            page.wait_for_selector(".vacancy-card", timeout=10000)
            cards = page.query_selector_all(".vacancy-card")
            self.assertGreaterEqual(len(cards), 3, "Должно отображаться минимум 3 тестовых вакансии")

            # 2. Проверяем тултип на бейдже процента (шкалы 5 категорий)
            score_badges = page.query_selector_all(".score-badge")
            self.assertGreater(len(score_badges), 0)
            tooltip_found = False
            for b in score_badges:
                title = b.get_attribute("title")
                if title and "Стек:" in title and "Опыт:" in title:
                    tooltip_found = True
                    break
            self.assertTrue(tooltip_found, "Тултип с 5 шкалами должен присутствовать на бейдже оценки")

            # 3. Кликаем по карточке с 85% (Beta Tech)
            beta_card = page.locator("text=Beta Tech").first
            self.assertTrue(beta_card.is_visible())
            beta_card.click()

            # Ждем открытия модалки вакансии
            page.locator("#vacancy-modal").wait_for(state="visible", timeout=5000)
            
            # Проверяем блок детализации по 5 шкалам
            breakdown_card = page.locator("#modal-scores-breakdown-card")
            self.assertTrue(breakdown_card.is_visible(), "Карточка 5 шкал должна быть видима в модалке")
            
            # Проверяем значения шкал
            stack_val = page.locator("#scale-stack-val").inner_text()
            self.assertEqual("26/30", stack_val)
            exp_val = page.locator("#scale-exp-val").inner_text()
            self.assertEqual("22/25", exp_val)
            grade_val = page.locator("#scale-grade-val").inner_text()
            self.assertEqual("18/20", grade_val)
            domain_val = page.locator("#scale-domain-val").inner_text()
            self.assertEqual("11/15", domain_val)
            format_val = page.locator("#scale-format-val").inner_text()
            self.assertEqual("8/10", format_val)
            
            # Проверяем ширину прогресс-баров (должна быть выставлена стилем width)
            stack_bar_style = page.locator("#scale-stack-fill").get_attribute("style")
            self.assertIn("width:", stack_bar_style)

            # Закрываем модалку вакансии
            page.click("#modal-close-btn")
            page.locator("#vacancy-modal").wait_for(state="hidden", timeout=5000)

            # 4. Кликаем по вакансии с блокером (Gamma Cloud)
            gamma_card = page.locator("text=Gamma Cloud").first
            gamma_card.click()
            page.locator("#vacancy-modal").wait_for(state="visible", timeout=5000)

            # Проверяем появление алерта о жестком блокере
            blocker_alert = page.locator("#modal-blocker-alert")
            self.assertTrue(blocker_alert.is_visible(), "Алерт о жестком блокере должен быть виден")
            blocker_text = page.locator("#modal-blocker-text").inner_text()
            self.assertIn("Новосибирске", blocker_text)

            # Закрываем модалку
            page.click("#modal-close-btn")
            page.locator("#vacancy-modal").wait_for(state="hidden", timeout=5000)

            # 5. Тестируем модалку условий автоостановки (кнопка в системных настройках)
            page.click("#open-system-settings-btn")
            page.locator("#system-settings-modal").wait_for(state="visible", timeout=5000)

            open_limits_btn = page.locator("#sys-open-limits-modal-btn")
            self.assertTrue(open_limits_btn.is_visible())
            open_limits_btn.click()

            page.locator("#limits-settings-modal").wait_for(state="visible", timeout=5000)

            apps_wrapper = page.locator("#limit-applications-wrapper")
            proc_wrapper = page.locator("#limit-processed-wrapper")

            # Проверяем клик по карточке "Только число откликов"
            apps_only_card = page.locator('.limit-mode-card[data-mode="applications"]')
            apps_only_card.click()
            page.wait_for_timeout(400) # ждем завершения анимации
            self.assertIn("collapsed", proc_wrapper.get_attribute("class"))
            self.assertNotIn("collapsed", apps_wrapper.get_attribute("class"))

            # Проверяем клик по карточке "Только число оценок"
            evals_only_card = page.locator('.limit-mode-card[data-mode="processed"]')
            evals_only_card.click()
            page.wait_for_timeout(400)
            self.assertNotIn("collapsed", proc_wrapper.get_attribute("class"))
            self.assertIn("collapsed", apps_wrapper.get_attribute("class"))

            # Проверяем клик по карточке "Оба условия"
            both_card = page.locator('.limit-mode-card[data-mode="both"]')
            both_card.click()
            page.wait_for_timeout(400)
            self.assertNotIn("collapsed", proc_wrapper.get_attribute("class"))
            self.assertNotIn("collapsed", apps_wrapper.get_attribute("class"))

            # Сохраняем модалку лимитов
            page.click("#limits-settings-save-btn")
            page.locator("#limits-settings-modal").wait_for(state="hidden", timeout=5000)

            # Проверяем обновление бейджа на кнопке лимитов
            badge_text = page.locator("#sys-limits-badge").inner_text()
            self.assertTrue(len(badge_text) > 0)

            browser.close()
        except Exception as e:
            err_str = str(e)
            if "TargetClosedError" in err_str or "Permission denied" in err_str or "Executable doesn't exist" in err_str or "MachPort" in err_str:
                self.skipTest(f"Браузер недоступен в текущей изолированной среде: {e}")
            raise

if __name__ == "__main__":
    unittest.main()
