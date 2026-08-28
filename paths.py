import os
import sys
import platform

def is_frozen() -> bool:
    """Возвращает True, если приложение запущено в виде собранного бинарника PyInstaller."""
    return getattr(sys, 'frozen', False)

def get_bundle_dir() -> str:
    """Возвращает путь к директории ресурсов (где лежат static, шаблоны и упакованные файлы)."""
    if is_frozen():
        return getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.dirname(os.path.abspath(__file__))

def get_app_data_dir() -> str:
    """
    Возвращает путь к постоянной системной директории для пользовательских данных (БД, куки, сессии).
    - macOS: ~/Library/Application Support/HH_AI_Applier
    - Windows: %APPDATA%/HH_AI_Applier
    - Linux: ~/.config/hh_ai_applier
    - В dev-режиме: локальная папка проекта.
    """
    custom_dir = os.getenv("HH_DATA_DIR")
    if custom_dir:
        os.makedirs(custom_dir, exist_ok=True)
        return custom_dir

    if not is_frozen():
        # В режиме разработки храним файлы рядом в корне проекта
        return os.path.dirname(os.path.abspath(__file__))

    app_name = "HH_AI_Applier"
    system = platform.system()

    if system == "Darwin":
        path = os.path.expanduser(f"~/Library/Application Support/{app_name}")
    elif system == "Windows":
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(appdata, app_name)
    else:
        path = os.path.expanduser(f"~/.config/{app_name.lower()}")

    os.makedirs(path, exist_ok=True)
    return path
