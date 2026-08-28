"""
HH AI Applier - CLI / Pipeline Entrypoint
"""
import sys
import os

# Добавляем корень проекта в sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.config import Config
from src.pipeline.runner import run_pipeline, load_resume_text, format_hh_resume_to_text

if __name__ == "__main__":
    print("Запуск HH AI Applier в консольном режиме...")
    run_pipeline(
        queries=Config.SEARCH_QUERIES,
        area_id=Config.SEARCH_AREA,
        threshold=Config.MATCH_THRESHOLD,
        resume_id=Config.HH_RESUME_ID,
        dry_run=Config.DRY_RUN
    )
