#!/bin/bash
set -e

echo "==============================================="
echo "  Сборка HH.ru AI Job Applier для macOS (.app) "
echo "==============================================="

# Активация виртуального окружения
if [ -d "venv" ]; then
    echo "Использование venv..."
    source venv/bin/activate
fi

# Очистка предыдущих сборок
echo "Очистка каталогов сборки (build/dist)..."
rm -rf build dist

# Сборка приложения через PyInstaller
echo "Компиляция через PyInstaller..."
PYINSTALLER_CONFIG_DIR="$(pwd)/build/pyi_cache" pyinstaller hh_applier.spec --noconfirm

echo ""
echo "==============================================="
echo "  Сборка завершена успешно!"
echo "  Приложение: dist/HH_AI_Applier.app"
echo "  Запуск: open dist/HH_AI_Applier.app"
echo "==============================================="
