@echo off
chcp 65001 > nul
echo ===============================================
echo   Сборка HH.ru AI Job Applier для Windows (.exe)
echo ===============================================

if exist venv (
    echo Использование venv...
    call venv\Scripts\activate.bat
)

echo Проверка и установка зависимостей...
pip install -r requirements.txt

echo Очистка каталогов сборки...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo Компиляция через PyInstaller...
pyinstaller hh_applier.spec --noconfirm

echo.
echo ===============================================
echo   Сборка завершена успешно!
echo   Исполняемый файл: dist\HH_AI_Applier\HH_AI_Applier.exe
echo ===============================================
pause
