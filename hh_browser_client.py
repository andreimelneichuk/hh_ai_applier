"""
HH AI Applier - Browser Client Module (Backward Compatibility Wrapper)
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.clients.browser import HHBrowserClient
