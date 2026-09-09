import os
import sys
import json
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.db.database as database
from src.clients.llm import LLMAnalyzer

def run_quality_evaluation():
    print("=" * 70)
    print("🔬 ЖИВАЯ ОЦЕНКА КАЧЕСТВА ВЫДАЧИ LLM")
    print("=" * 70)

    # 1. Инициализация БД
    database.init_db()
    
    # 2. Убеждаемся, что модель Gemini установлена в gemini-2.5-flash
    g_model = database.get_config_value("gemini_model")
    if not g_model or "3.6" in g_model:
        database.set_config_value("gemini_model", "gemini-2.5-flash")

    analyzer = LLMAnalyzer()

    # 3. Резюме кандидата (пример)
    candidate_resume = """
ФИО: Иван Иванов
Пол: Мужской
Желаемая должность: Senior AI / LLM / Python Engineer
Опыт работы: 5+ лет в коммерческой разработке.

Ключевой стек:
- Python 3.11+, FastAPI, AsyncIO, Celery, Redis, PostgreSQL (SQLAlchemy, Alembic)
- LLM / AI: RAG архитектуры, LangChain, LlamaIndex, OpenAI API, Anthropic, Gemini, OpenSearch (векторный и гибридный поиск), MCP (Model Context Protocol), динамический tool-use, LLM-as-a-Judge, Langfuse трейсинг
- Инфраструктура: Docker, Kubernetes, GitLab CI/CD, Linux, Kafka
- Опыт руководства: менторинг разработчиков, проведение code review, проектирование архитектуры микросервисов и AI-агентов
"""

    resumes_struct = [{
        "id": "sample_resume_id",
        "title": "LLM Engineer",
        "first_name": "Иван",
        "gender": "Мужской"
    }]

    # --- ТЕСТ КЕЙС 1: Профильная вакансия ---
    print("\n" + "-" * 70)
    print("📌 ТЕСТ 1: Профильная вакансия (Senior AI / Python Engineer)")
    vac_target = {
        "title": "Senior AI / LLM Engineer",
        "company": "Neural Innovations",
        "salary": "от 300 000 руб.",
        "experience": "От 3 до 6 лет",
        "schedule": "Удаленная работа",
        "employment": "Полная занятость",
        "location": "Москва (Удаленно)",
        "skills": ["Python", "FastAPI", "PostgreSQL", "LLM", "RAG", "Docker", "Asyncio"],
        "description": """
Мы разрабатываем корпоративную AI-платформу с мультиагентным взаимодействием.
Задачи:
- Проектирование и разработка RAG-пайплайнов с гибридным поиском и переранжированием.
- Интеграция LLM-агентов через MCP (Tool Calling) для взаимодействия с внутренними сервисами.
- Оптимизация latency и затрат на токены, внедрение трейсинга и автоматических тестов качества генерации.
- Разработка асинхронных микросервисов на FastAPI и PostgreSQL.
"""
    }

    try:
        res1 = analyzer.analyze_vacancy(resume_text=candidate_resume, vacancy=vac_target, threshold=70, resumes=resumes_struct)
        print(f"📊 Провайдер: {res1.analyzed_by_provider} | Модель: {res1.analyzed_by_model}")
        print(f"🎯 Итоговый балл: {res1.match_score}/100 | Подходит: {res1.is_match}")
        if res1.scores:
            print(f"   Детализация шкал:")
            print(f"   - Стек: {res1.scores.stack_score}/30")
            print(f"   - Опыт: {res1.scores.experience_score}/25")
            print(f"   - Грейд: {res1.scores.grade_score}/20")
            print(f"   - Домен: {res1.scores.domain_score}/15")
            print(f"   - Формат: {res1.scores.format_score}/10")
        print(f"💡 Обоснование:\n{res1.reasoning}\n")
        print(f"✉️ Сопроводительное письмо:\n{res1.cover_letter}\n")
        
        # Проверки качества письма
        has_stamp = any(s in res1.cover_letter.lower() for s in ["откликаюсь на позицию", "мой опыт решает ваши задачи", "прошу рассмотреть"])
        has_correct_signature = "с уважением,\nиван" in res1.cover_letter.lower() or "с уважением, иван" in res1.cover_letter.lower()
        print("🔍 Проверка критериев качества:")
        print(f"   - Отсутствие шаблонных штампов: {'✅ ДА' if not has_stamp else '❌ НЕТ (найдены штампы)'}")
        print(f"   - Персонализированная подпись (Иван): {'✅ ДА' if has_correct_signature else '❌ НЕТ'}")
        print(f"   - Соответствие порогу (>75%): {'✅ ДА' if res1.match_score >= 75 else '❌ НЕТ'}")
    except Exception as e:
        print(f"❌ Ошибка вызова LLM в тесте 1: {e}")

    # --- ТЕСТ КЕЙС 2: Непрофильная вакансия ---
    print("\n" + "-" * 70)
    print("📌 ТЕСТ 2: Непрофильная вакансия (Преподаватель программирования для школьников)")
    vac_tutor = {
        "title": "Преподаватель Python для школьников 5-11 классов",
        "company": "Детская IT-Академия",
        "salary": "от 40 000 руб.",
        "experience": "От 1 года",
        "schedule": "Гибкий график",
        "employment": "Частичная занятость",
        "location": "Пермь",
        "skills": ["Python", "Обучение", "Педагогика", "Работа с детьми", "Scratch"],
        "description": """
Обучение школьников основам алгоритмизации, синтаксиса Python, создания простых 2D-игр на Pygame.
Проведение открытых уроков для родителей, проверка домашних заданий, поддержание дисциплины.
Требования: любовь к детям, педагогическое образование или опыт репетиторства.
"""
    }

    try:
        res2 = analyzer.analyze_vacancy(resume_text=candidate_resume, vacancy=vac_tutor, threshold=70, resumes=resumes_struct)
        print(f"📊 Провайдер: {res2.analyzed_by_provider} | Модель: {res2.analyzed_by_model}")
        print(f"🎯 Итоговый балл: {res2.match_score}/100 | Подходит: {res2.is_match}")
        if res2.scores:
            print(f"   Детализация шкал:")
            print(f"   - Стек: {res2.scores.stack_score}/30 (Python есть, но Scratch/Pygame нет)")
            print(f"   - Опыт: {res2.scores.experience_score}/25 (Педагогического опыта нет)")
            print(f"   - Грейд: {res2.scores.grade_score}/20 (Overqualified)")
            print(f"   - Домен: {res2.scores.domain_score}/15 (Школьное образование)")
            print(f"   - Формат: {res2.scores.format_score}/10")
        print(f"💡 Обоснование:\n{res2.reasoning}\n")
        print("🔍 Проверка критериев качества:")
        print(f"   - Адекватная отсечка непрофильной вакансии (<70%): {'✅ ДА' if not res2.is_match else '❌ ОШИБКА: модель одобрила нерелевантную вакансию!'}")
    except Exception as e:
        print(f"❌ Ошибка вызова LLM в тесте 2: {e}")

    # --- ТЕСТ КЕЙС 3: Офис в Москве (Политика неблокирующей локации) ---
    print("\n" + "-" * 70)
    print("📌 ТЕСТ 3: Вакансия со строгим офисом в Москве (не должна блокироваться hard blocker)")
    vac_office = {
        "title": "Senior Python Backend Developer",
        "company": "Moscow Onsite FinTech",
        "salary": "от 320 000 руб.",
        "experience": "От 3 до 6 лет",
        "schedule": "Полный день",
        "employment": "Полная занятость",
        "location": "Москва, офис (м. Деловой центр, без удаленки)",
        "skills": ["Python", "FastAPI", "PostgreSQL", "Docker", "Kafka"],
        "description": """
Разработка бэкенда высоконагруженного финтех-процессинга.
ВНИМАНИЕ: Работа строго в офисе в Москва-Сити, удаленный формат не предусмотрен из соображений безопасности.
"""
    }

    try:
        res3 = analyzer.analyze_vacancy(resume_text=candidate_resume, vacancy=vac_office, threshold=70, resumes=resumes_struct)
        print(f"📊 Провайдер: {res3.analyzed_by_provider} | Модель: {res3.analyzed_by_model}")
        print(f"🎯 Итоговый балл: {res3.match_score}/100 | Подходит: {res3.is_match}")
        print(f"🚫 Hard Blocker: {res3.has_hard_blocker} (Причина: {res3.blocker_reason})")
        if res3.scores:
            print(f"   - Шкала формата (локации): {res3.scores.format_score}/10 (ожидается 0-2)")
        print(f"💡 Обоснование:\n{res3.reasoning}\n")
        print("🔍 Проверка критериев качества:")
        print(f"   - Hard Blocker снят (локация не блокирует отклик): {'✅ ДА' if not res3.has_hard_blocker else '❌ ОШИБКА: офис заблокировал отклик!'}")
    except Exception as e:
        print(f"❌ Ошибка вызова LLM в тесте 3: {e}")

    # --- ТЕСТ КЕЙС 4: Тестовые вопросы работодателя ---
    print("\n" + "-" * 70)
    print("📌 ТЕСТ 4: Ответы на опросник работодателя (честность и мужской род)")
    questions = [
        {
            "id": "q_rls",
            "text": "Есть ли у вас практический опыт настройки Row-Level Security (RLS) в PostgreSQL?",
            "type": "single_choice",
            "options": [
                "Да, регулярно настраивал политики RLS на продакшене",
                "Знаю теоретически, но на проде не внедрял",
                "Нет, с RLS не работал"
            ]
        },
        {
            "id": "q_test_task",
            "text": "Готовы ли вы выполнить тестовое задание перед техническим интервью?",
            "type": "single_choice",
            "options": [
                "Да, готов выполнить адекватное тестовое (до 2-4 часов)",
                "Нет, тестовые задания принципиально не выполняю",
                "Только за отдельную оплату"
            ]
        },
        {
            "id": "q_free_text",
            "text": "Кратко опишите самую сложную техническую задачу за последний год и как вы ее решили.",
            "type": "text"
        }
    ]

    try:
        res4 = analyzer.answer_questions(
            resume_text=candidate_resume,
            vacancy=vac_target,
            questions=questions
        )
        print(f"📊 Уверенность во всех ответах: {res4.all_confident}")
        for ans in res4.answers:
            print(f"\n❓ Вопрос [{ans.id}]: {ans.question_text}")
            print(f"💬 Ответ: {ans.answer}")
            print(f"🧠 Обоснование: {ans.reasoning}")

        # Проверка пола в тексте ответа
        q3_ans = next((a for a in res4.answers if a.id == "q_free_text"), None)
        if q3_ans:
            is_female_grammar = any(w in q3_ans.answer.lower() for w in ["разрабатывала", "проектировала", "решала", "готова"])
            has_brackets = "(а)" in q3_ans.answer or "/а" in q3_ans.answer
            print("\n🔍 Проверка критериев качества ответов на вопросы:")
            print(f"   - Формулировка строго в мужском роде: {'✅ ДА' if not is_female_grammar else '❌ ОШИБКА: обнаружен женский род'}")
            print(f"   - Отсутствие скобок (а) и слэшей: {'✅ ДА' if not has_brackets else '❌ ОШИБКА: найдены скобки (а)'}")
            
        # Проверка честности RLS (не заявлять прод-опыт)
        q1_ans = next((a for a in res4.answers if a.id == "q_rls"), None)
        if q1_ans:
            falsely_claimed_prod = "регулярно настраивал" in q1_ans.answer.lower()
            print(f"   - Честность по RLS (не заявлять несуществующий прод-опыт): {'✅ ДА' if not falsely_claimed_prod else '❌ ОШИБКА: модель соврала о прод-опыте с RLS!'}")
            
    except Exception as e:
        print(f"❌ Ошибка вызова LLM в тесте 4: {e}")

    print("\n" + "=" * 70)
    print("🏁 ОЦЕНКА ЗАВЕРШЕНА")
    print("=" * 70)

if __name__ == "__main__":
    run_quality_evaluation()
