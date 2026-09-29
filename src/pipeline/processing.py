"""
Общий шаг обработки вакансии после LLM-анализа: проверка порога → вопросы работодателя → отклик.

Используется пайплайном, быстрым откликом и переоценкой, чтобы правила принятия решения
(порог, блокеры, уверенность ответов, dry run) были одинаковыми во всех сценариях.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.db import database

logger = logging.getLogger("VacancyProcessing")

# Ответ ИИ ниже этой уверенности требует подтверждения пользователем
QUESTION_CONFIDENCE_THRESHOLD = 85


@dataclass
class VacancyDecision:
    status: str  # new | needs_answers | applied | already_applied | ignored | failed
    analysis: Any
    is_eligible: bool
    resume_id: Optional[str]
    resume_title: Optional[str]
    cover_letter: str
    question_answers: List[Dict[str, Any]] = field(default_factory=list)
    # Причина отказа, которая сохраняется в БД вместо обоснования ИИ
    error: Optional[str] = None
    # Ответ hh.ru при неудачном отклике (в БД остаётся обоснование анализа)
    apply_error: Optional[str] = None
    # Пользователь нажал «Стоп» до отклика — решение не принято и не должно сохраняться
    stopped: bool = False

    @property
    def questions_data(self) -> Optional[str]:
        return json.dumps(self.question_answers, ensure_ascii=False) if self.question_answers else None

    @property
    def scores(self) -> Optional[Dict[str, Any]]:
        if not self.analysis.scores:
            return None
        scores = self.analysis.scores.model_dump()
        scores["has_hard_blocker"] = self.analysis.has_hard_blocker
        scores["blocker_reason"] = self.analysis.blocker_reason
        return scores

    def save(self, vacancy_id: str, title: str, company: str):
        scores = self.scores
        database.save_vacancy(
            vacancy_id=vacancy_id,
            title=title,
            company=company,
            status=self.status,
            match_score=self.analysis.match_score,
            analysis_reason=self.error or self.analysis.reasoning,
            cover_letter=self.cover_letter,
            questions_data=self.questions_data,
            applied_resume_id=self.resume_id,
            applied_resume_title=self.resume_title,
            scores_data=json.dumps(scores, ensure_ascii=False) if scores else None,
            analyzed_by_provider=getattr(self.analysis, "analyzed_by_provider", None),
            analyzed_by_model=getattr(self.analysis, "analyzed_by_model", None)
        )


def is_eligible(analysis, threshold: int) -> bool:
    """Вакансия проходит: нет блокирующего фактора и совпадение не ниже порога."""
    return (not analysis.has_hard_blocker) and (analysis.match_score >= threshold)


def choose_resume(analysis, candidate_resumes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Резюме, выбранное ИИ, или первое из кандидатов."""
    fallback = candidate_resumes[0] if candidate_resumes else {"id": None, "title": "Резюме", "text": ""}
    resume_id = analysis.selected_resume_id or fallback.get("id")
    chosen = next((r for r in candidate_resumes if r.get("id") == resume_id), fallback)
    return {
        "id": resume_id,
        "title": analysis.selected_resume_title or chosen.get("title") or "Резюме",
        "text": chosen.get("text", "")
    }


def decide_and_apply(
    hh_client,
    analyzer,
    vacancy_id: str,
    details: Dict[str, Any],
    analysis,
    candidate_resumes: List[Dict[str, Any]],
    *,
    threshold: int,
    dry_run: bool,
    user_saved_answers: Optional[List[Dict[str, Any]]] = None,
    cover_letter: Optional[str] = None,
    force: bool = False,
    allow_local_resume: bool = False,
    should_stop: Optional[Callable[[], bool]] = None,
) -> VacancyDecision:
    """Принимает решение по проанализированной вакансии и при необходимости отправляет отклик.

    cover_letter — письмо, заданное пользователем (иначе берётся письмо из анализа).
    force — обработать вакансию как подходящую, даже если она не прошла порог.
    allow_local_resume — разрешить отклик без резюме из профиля HH (hh.ru выберет резюме по умолчанию);
    автоматические сценарии так не делают, чтобы не откликаться неподходящим резюме.
    """
    resume = choose_resume(analysis, candidate_resumes)
    decision = VacancyDecision(
        status="ignored",
        analysis=analysis,
        is_eligible=is_eligible(analysis, threshold),
        resume_id=resume["id"],
        resume_title=resume["title"],
        cover_letter=(cover_letter if cover_letter is not None else analysis.cover_letter) or "",
    )

    if not decision.is_eligible and not force:
        return decision

    if should_stop and should_stop():
        decision.stopped = True
        return decision

    answers_dict = None
    needs_user_answers = False
    questions = hh_client.get_vacancy_questions(vacancy_id)
    if questions and isinstance(questions, list):
        logger.info(f"Обнаружено {len(questions)} вопросов от работодателя. Генерация ответов через ИИ...")
        if user_saved_answers is None:
            user_saved_answers = database.get_user_profile_answers()
        q_res = analyzer.answer_questions(resume["text"], details, questions, user_saved_answers)
        decision.question_answers = [a.model_dump() for a in q_res.answers]
        answers_dict = {a.id: a.answer for a in q_res.answers}
        needs_user_answers = (not q_res.all_confident) or any(
            a.requires_user_input or a.confidence < QUESTION_CONFIDENCE_THRESHOLD for a in q_res.answers
        )

    if needs_user_answers:
        decision.status = "needs_answers"
    elif dry_run:
        decision.status = "new"
    elif not allow_local_resume and (not decision.resume_id or decision.resume_id == "local"):
        decision.status = "failed"
        decision.error = "Резюме не найдено в профиле HH для отклика"
    else:
        success, err_msg = hh_client.apply_to_vacancy(
            vacancy_id=vacancy_id,
            resume_title_or_id=decision.resume_id,
            cover_letter=decision.cover_letter,
            answers=answers_dict,
            dry_run=False
        )
        if success:
            decision.status = "already_applied" if err_msg == "ALREADY_APPLIED" else "applied"
        else:
            decision.status = "failed"
            decision.apply_error = err_msg or "неизвестная ошибка"
    return decision
