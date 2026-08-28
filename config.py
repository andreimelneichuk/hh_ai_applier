"""
HH AI Applier - Config Module (Backward Compatibility Wrapper)
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.config import Config
