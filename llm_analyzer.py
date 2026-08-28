"""
HH AI Applier - LLM Analyzer Module (Backward Compatibility Wrapper)
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.clients.llm import (
    LLMAnalyzer,
    VacancyAnalysis,
    QuestionAnswer,
    QuestionsAnalysisResult,
    QuotaExceededError
)
