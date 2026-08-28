"""
HH AI Applier - Database Module (Backward Compatibility Wrapper)
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import src.db.database as _db_module
from src.db.database import (
    init_db,
    save_processed_vacancy,
    save_vacancy,
    is_vacancy_processed,
    get_all_processed,
    get_all_vacancies,
    get_processed_paginated,
    get_processed_count,
    update_vacancy_questions,
    update_vacancy_status,
    delete_vacancy,
    get_vacancy,
    get_config_value,
    set_config_value,
    get_system_setting,
    set_system_setting,
    get_all_system_settings,
    reset_system_prompt_to_default,
    get_user_profile_answers,
    set_user_profile_answer,
    delete_user_profile_answer,
    merge_from_db,
    DEFAULT_SYSTEM_PROMPT
)
import sqlite3

def __getattr__(name):
    if name == "DB_PATH":
        return _db_module.DB_PATH
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

def __setattr__(name, value):
    if name == "DB_PATH":
        _db_module.DB_PATH = value
    super().__setattr__(name, value)
