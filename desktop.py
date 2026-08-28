"""
HH AI Applier - Desktop GUI Entrypoint
"""
import sys
import os

# Добавляем корень проекта в sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.desktop.launcher import main

if __name__ == "__main__":
    main()
