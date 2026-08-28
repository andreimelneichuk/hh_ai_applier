"""
HH AI Applier - API Server & Backward Compatibility Wrapper
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.api.app import app, create_app
from src.api.state import (
    pipeline_status,
    login_browser_active,
    SearchSettings,
    SystemSettingsPayload,
    UserProfileAnswerPayload,
    QuickApplyPayload,
    ApplyPayload,
    run_in_clean_thread
)
from src.clients.browser import HHBrowserClient
from src.clients.llm import (
    LLMAnalyzer,
    VacancyAnalysis,
    QuestionAnswer,
    QuestionsAnalysisResult,
    QuotaExceededError
)
from src.pipeline.runner import (
    run_pipeline,
    load_resume_text,
    format_hh_resume_to_text
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.app:app", host="127.0.0.1", port=8000, reload=True)
