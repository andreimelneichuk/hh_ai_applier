"""
HH AI Applier - Paths Module (Backward Compatibility Wrapper)
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.paths import get_app_data_dir, get_bundle_dir, is_frozen
