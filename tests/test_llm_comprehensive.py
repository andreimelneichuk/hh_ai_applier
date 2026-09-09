import os
import sys
import unittest
import json
import sqlite3
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.db.database as database
from src.core.config import Config
from src.clients.llm import (
    LLMAnalyzer, VacancyAnalysis, EvaluationScores, QuestionAnswer,
    QuestionsAnalysisResult, QuotaExceededError, OPENAI_PROVIDER_PRESETS
)

TEST_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_llm_comprehensive_db.db")

class TestLLMComprehensive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["HH_DB_PATH"] = TEST_DB_PATH
        database.DB_PATH = TEST_DB_PATH
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except Exception:
                pass
        database.init_db()

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

    def test_01_candidate_meta_extraction_all_sources(self):
        """Проверка динамического извлечения имени и пола из всех источников."""
        # 1. Из структурированного резюме (женщина)
        resumes_female = [{
            "id": "res_f",
            "first_name": "Анна",
            "last_name": "Смирнова",
            "gender": "Женский",
            "title": "Backend Python Developer"
        }]
        meta_f = LLMAnalyzer._extract_candidate_meta(resume_text="", resumes=resumes_female)
        self.assertEqual(meta_f["first_name"], "Анна")
        self.assertEqual(meta_f["full_name"], "Анна Смирнова")
        self.assertEqual(meta_f["gender"], "Женский")

        # 2. Из структурированного резюме (мужчина)
        resumes_male = [{
            "id": "res_m",
            "first_name": "Дмитрий",
            "last_name": "Ковалев",
            "gender": "Мужской",
            "title": "Lead AI Engineer"
        }]
        meta_m = LLMAnalyzer._extract_candidate_meta(resume_text="", resumes=resumes_male)
        self.assertEqual(meta_m["first_name"], "Дмитрий")
        self.assertEqual(meta_m["full_name"], "Дмитрий Ковалев")
        self.assertEqual(meta_m["gender"], "Мужской")

        # 3. Из сырого текста резюме
        raw_text = "ФИО: Светлана Соколова\nПол: Женский\nОпыт работы: 5 лет..."
        meta_raw = LLMAnalyzer._extract_candidate_meta(resume_text=raw_text)
        self.assertEqual(meta_raw["first_name"], "Светлана")
        self.assertEqual(meta_raw["gender"], "Женский")

        # 4. Из профиля пользователя (fallback)
        database.set_user_profile_answer("candidate_name", "Имя соискателя", "Максим Романов")
        database.set_user_profile_answer("candidate_gender", "Пол соискателя", "Мужской")
        meta_fallback = LLMAnalyzer._extract_candidate_meta(resume_text="Только опыт без имени")
        self.assertEqual(meta_fallback["first_name"], "Максим")
        self.assertEqual(meta_fallback["gender"], "Мужской")

    def test_02_build_prompt_signature_and_name_injection(self):
        """Проверка динамической подстановки имени соискателя в системный промпт."""
        analyzer = LLMAnalyzer()
        vac = {"title": "Python Developer", "company": "Alpha Corp", "description": "Django, Postgres"}
        
        # Для кандидата Екатерины
        prompt_katya = analyzer._build_prompt(
            resume_text="Python dev",
            vacancy=vac,
            match_threshold=70,
            resumes=[{"first_name": "Екатерина", "gender": "Женский"}]
        )
        self.assertIn("С уважением, Екатерина", prompt_katya)
        self.assertNotIn("{candidate_name}", prompt_katya)
        self.assertNotIn("С уважением, Кандидат", prompt_katya)

        # Для кандидата Игоря
        prompt_igor = analyzer._build_prompt(
            resume_text="Python dev",
            vacancy=vac,
            match_threshold=70,
            resumes=[{"first_name": "Игорь", "gender": "Мужской"}]
        )
        self.assertIn("С уважением, Игорь", prompt_igor)
        self.assertNotIn("{candidate_name}", prompt_igor)

    def test_03_questions_prompt_gender_and_honesty_rules(self):
        """Проверка динамических правил рода и честности в промпте опросников работодателя."""
        analyzer = LLMAnalyzer()
        questions = [
            {"id": "q1", "text": "Работали ли вы с Row-Level Security (RLS)?", "type": "single_choice"},
            {"id": "q2", "text": "Опишите ваш опыт оптимизации сложных SQL запросов", "type": "text"}
        ]
        vac = {"title": "Senior Data Engineer", "company": "DataWorks"}

        # 1. Женский род
        q_prompt_female = analyzer._build_questions_prompt(
            resume_text="ФИО: Мария Петрова\nПол: Женский\nPython, FastAPI",
            vacancy=vac,
            questions=questions
        )
        self.assertIn("ПОЛ КАНДИДАТА — ЖЕНСКИЙ", q_prompt_female)
        self.assertIn("разрабатывала", q_prompt_female)
        self.assertIn("применяла", q_prompt_female)
        self.assertIn("готова", q_prompt_female)
        self.assertIn("ПРЕЗУМПЦИЯ ОТСУТСТВИЯ ОПЫТА", q_prompt_female)
        self.assertIn("Row-Level Security", q_prompt_female)

        # 2. Мужской род
        q_prompt_male = analyzer._build_questions_prompt(
            resume_text="ФИО: Роман Сидоров\nПол: Мужской\nPython, FastAPI",
            vacancy=vac,
            questions=questions
        )
        self.assertIn("ПОЛ КАНДИДАТА — МУЖСКОЙ", q_prompt_male)
        self.assertIn("разрабатывал", q_prompt_male)
        self.assertIn("применял", q_prompt_male)
        self.assertIn("готов", q_prompt_male)
        self.assertIn("ПРЕЗУМПЦИЯ ОТСУТСТВИЯ ОПЫТА", q_prompt_male)

    def test_04_cover_letter_signature_and_custom_postfix(self):
        """Проверка генерации сопроводительного письма с подписью и постфиксом."""
        analyzer = LLMAnalyzer()
        database.set_system_setting("cover_letter_postfix", "Telegram: @candidate_dev\nGitHub: github.com/test")

        cl_prompt = analyzer._build_cover_letter_prompt(
            resume_text="ФИО: Анастасия Морозова\nSenior Python Engineer",
            vacancy={"title": "Team Lead Python", "company": "FintechTech"}
        )
        self.assertIn("С уважением, Анастасия", cl_prompt)

        # Проверка мок-анализа с учетом пола и постфикса
        res_mock = analyzer._mock_analysis(
            vacancy={"title": "Team Lead Python", "company": "FintechTech", "skills": ["Python"]},
            match_threshold=40,
            resumes=[{"first_name": "Анастасия", "gender": "Женский"}]
        )
        self.assertTrue(res_mock.is_match)
        self.assertIn("Буду рада обсудить", res_mock.cover_letter)
        self.assertIn("С уважением,\nАнастасия", res_mock.cover_letter)
        self.assertIn("Telegram: @candidate_dev", res_mock.cover_letter)
        self.assertIn("GitHub: github.com/test", res_mock.cover_letter)

    def test_05_location_non_blocking_policy(self):
        """
        Проверка: офис или чужой город не должен блокировать отклик (has_hard_blocker сбрасывается),
        а реальные блокеры (гражданство/гостайна) остаются активными.
        """
        analyzer = LLMAnalyzer(gemini_api_keys=["mock_key"])
        database.set_system_setting("primary_provider", "gemini")

        # 1. Вакансия с офисом в другом городе, ошибочно помеченная моделью как hard blocker
        location_blocked_analysis = VacancyAnalysis(
            reasoning="Отличный Python разработчик, но работа в офисе в Новосибирске",
            scores=EvaluationScores(stack_score=30, experience_score=25, grade_score=20, domain_score=15, format_score=0),
            has_hard_blocker=True,
            blocker_reason="Требуется присутствие в офисе в Новосибирске",
            match_score=90,
            is_match=False
        )

        with patch.object(analyzer, "_call_gemini", return_value=location_blocked_analysis):
            result = analyzer.analyze_vacancy(
                resume_text="Python Senior Developer",
                vacancy={"title": "Python Dev", "location": "Новосибирск, офис"},
                threshold=70
            )
            # Блокер должен быть программно снят, чтобы кандидат мог откликнуться
            self.assertFalse(result.has_hard_blocker)
            self.assertIsNone(result.blocker_reason)
            self.assertTrue(result.is_match)
            self.assertEqual(result.match_score, 90)

        # 2. Реальный юридический блокер (например, строгое гражданство ЕС или гостайна)
        legal_blocked_analysis = VacancyAnalysis(
            reasoning="Кандидат не имеет гражданства ЕС и формы допуска к гостайне",
            scores=EvaluationScores(stack_score=30, experience_score=20, grade_score=20, domain_score=15, format_score=10),
            has_hard_blocker=True,
            blocker_reason="Только граждане ЕС с формой допуска секретности",
            match_score=95,
            is_match=False
        )

        with patch.object(analyzer, "_call_gemini", return_value=legal_blocked_analysis):
            result_legal = analyzer.analyze_vacancy(
                resume_text="Python Senior Developer",
                vacancy={"title": "Security Engineer", "description": "Только граждане ЕС"},
                threshold=70
            )
            # Юридический блокер должен остаться в силе
            self.assertTrue(result_legal.has_hard_blocker)
            self.assertEqual(result_legal.blocker_reason, "Только граждане ЕС с формой допуска секретности")
            self.assertFalse(result_legal.is_match)

    def test_06_provider_model_tracking_and_saving(self):
        """Проверка трекинга провайдера и модели через весь стек (LLM -> DB -> API)."""
        analyzer = LLMAnalyzer(gemini_api_keys=["mock_key"])
        database.set_system_setting("primary_provider", "gemini")

        mock_gemini_resp = VacancyAnalysis(
            reasoning="Отличный стек Python и FastAPI",
            scores=EvaluationScores(stack_score=28, experience_score=22, grade_score=18, domain_score=14, format_score=10),
            has_hard_blocker=False,
            match_score=92,
            is_match=True,
            cover_letter="Здравствуйте! Рад познакомиться. С уважением, Андрей"
        )

        with patch.object(analyzer, "_call_gemini", return_value=mock_gemini_resp):
            res = analyzer.analyze_vacancy(
                resume_text="Senior Python Dev",
                vacancy={"title": "Python Dev", "company": "CloudCorp"},
                threshold=70,
                model="gemini-2.5-flash"
            )
            self.assertEqual(res.analyzed_by_provider, "Google Gemini")
            self.assertEqual(res.analyzed_by_model, "gemini-2.5-flash")

            # Сохраняем в БД
            database.save_vacancy(
                vacancy_id="vac_track_777",
                title="Python Dev",
                company="CloudCorp",
                status="applied",
                match_score=res.match_score,
                analysis_reason=res.reasoning,
                cover_letter=res.cover_letter,
                analyzed_by_provider=res.analyzed_by_provider,
                analyzed_by_model=res.analyzed_by_model
            )

            job = database.get_vacancy("vac_track_777")
            self.assertIsNotNone(job)
            self.assertEqual(job["analyzed_by_provider"], "Google Gemini")
            self.assertEqual(job["analyzed_by_model"], "gemini-2.5-flash")

    def test_07_provider_failover_gemini_to_mistral(self):
        """Проверка автоматического перехода на следующего провайдера при исчерпании квоты (429)."""
        analyzer = LLMAnalyzer()

        mock_provs = [
            {"id": "gemini", "protocol": "gemini", "name": "Google Gemini", "active_model": "gemini-2.5-flash"},
            {"id": "mistral", "protocol": "mistral", "name": "Mistral AI", "active_model": "open-mistral-nemo"}
        ]

        mistral_resp = VacancyAnalysis(
            reasoning="Разбор выполнен через Mistral резервный провайдер",
            scores=EvaluationScores(stack_score=25, experience_score=20, grade_score=18, domain_score=12, format_score=10),
            has_hard_blocker=False,
            match_score=85,
            is_match=True
        )

        with patch.object(analyzer, "_get_execution_providers", return_value=mock_provs), \
             patch.object(analyzer, "_call_gemini", side_effect=QuotaExceededError("Gemini quota 429")), \
             patch.object(analyzer, "_call_mistral", return_value=mistral_resp):

            res = analyzer.analyze_vacancy(
                resume_text="Senior Python",
                vacancy={"title": "Backend Dev", "company": "FailoverCo"},
                threshold=70
            )

            self.assertEqual(res.match_score, 85)
            self.assertEqual(res.analyzed_by_provider, "Mistral AI")
            self.assertEqual(res.analyzed_by_model, "open-mistral-nemo")

    def test_08_pydantic_question_answering_schema(self):
        """Проверка валидации ответов на вопросы через Pydantic схему QuestionsAnalysisResult."""
        raw_json = {
            "all_confident": True,
            "answers": [
                {
                    "id": "q_salary",
                    "question_text": "Зарплатные ожидания?",
                    "answer": "250000",
                    "confidence": 95,
                    "requires_user_input": False,
                    "reasoning": "Ожидания кандидата от 250 000 руб."
                },
                {
                    "id": "q_relocation",
                    "question_text": "Готовы к переезду?",
                    "answer": "Да, готов",
                    "confidence": 90,
                    "requires_user_input": False,
                    "reasoning": "Кандидат готов к релокации"
                },
                {
                    "id": "q_experience",
                    "question_text": "Ваш опыт?",
                    "answer": "Разрабатывала высоконагруженные микросервисы на FastAPI и Celery",
                    "confidence": 90,
                    "requires_user_input": False,
                    "reasoning": "Релевантный опыт в женском роде"
                }
            ]
        }
        parsed = QuestionsAnalysisResult.model_validate(raw_json)
        self.assertTrue(parsed.all_confident)
        self.assertEqual(len(parsed.answers), 3)
        self.assertEqual(parsed.answers[0].id, "q_salary")
        self.assertEqual(parsed.answers[0].answer, "250000")
        self.assertEqual(parsed.answers[2].answer, "Разрабатывала высоконагруженные микросервисы на FastAPI и Celery")

    def test_09_multi_resume_matching_selection(self):
        """Проверка выбора наиболее релевантного резюме при анализе с несколькими резюме."""
        analyzer = LLMAnalyzer()
        resumes = [
            {"id": "r_frontend", "title": "React Frontend Developer", "first_name": "Иван"},
            {"id": "r_ai", "title": "Senior AI / Python Engineer", "first_name": "Иван"},
            {"id": "r_qa", "title": "QA Automation Engineer", "first_name": "Иван"}
        ]
        vac = {
            "title": "Senior LLM / Python Engineer",
            "company": "NeuralHub",
            "skills": ["Python", "FastAPI", "LLM", "LangChain"]
        }

        # Мок-анализ должен выбрать r_ai благодаря максимальному совпадению стека Python и AI
        res = analyzer._mock_analysis(vac, match_threshold=50, resumes=resumes)
        self.assertEqual(res.selected_resume_id, "r_ai")
        self.assertEqual(res.selected_resume_title, "Senior AI / Python Engineer")
        self.assertTrue(res.is_match)

    def test_10_stack_score_safeguard_and_rejection(self):
        """Проверка программного предохранителя: если stack_score < 15, отклик блокируется и письмо очищается."""
        analysis = VacancyAnalysis(
            reasoning="Кандидат сильный бэкендер, но вакансия требует Oracle PL/SQL, которого нет в резюме.",
            scores=EvaluationScores(
                stack_score=5,       # Стек не подходит (< 15)
                experience_score=25,
                grade_score=20,
                domain_score=15,
                format_score=10
            ),
            has_hard_blocker=False,
            match_score=75,
            is_match=True,
            cover_letter="Готов писать на Oracle PL/SQL"
        )
        # При вызове calculate_total_score() или анализе
        analysis.calculate_total_score()
        self.assertFalse(analysis.is_match, "is_match должен быть сброшен в False при stack_score < 15")
        self.assertEqual(analysis.cover_letter, "", "cover_letter должен быть очищен при неподходящем стеке")

if __name__ == "__main__":
    unittest.main()
