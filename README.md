# 🤖 HeadHunter AI Job Applier

<p align="center">
  <img src="assets/icon.png" alt="HH AI Applier Logo" width="128" height="128">
</p>

<p align="center">
  <b>Умный десктопный ассистент для автоматизированного и точечного поиска работы на HeadHunter (hh.ru) с поддержкой ИИ (Google Gemini & Mistral AI).</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue.svg" alt="Python Version">
  <img src="https://img.shields.io/badge/FastAPI-0.110+-green.svg" alt="FastAPI">
  <img src="https://img.shields.io/badge/Playwright-1.42+-orange.svg" alt="Playwright">
  <img src="https://img.shields.io/badge/Platforms-macOS%20%7C%20Windows-lightgrey.svg" alt="Platforms">
  <img src="https://img.shields.io/badge/License-MIT-purple.svg" alt="License">
</p>

---

## 🌟 Ключевые возможности

- 🚀 **Браузерная автоматизация (Playwright)**: Работает напрямую через ваш профиль HeadHunter. Не требует покупки платного API работодателя — сессия сохраняется в защищенном профиле Chromium/Chrome.
- 🧠 **Умный ИИ-анализ вакансий (Match Score 0–100%)**: Оценивает совпадение стека технологий, грейда и опыта с помощью моделей **Google Gemini** (`gemini-3.6-flash`, `gemini-2.5-flash`, `gemini-2.5-pro`) и **Mistral AI** (`mistral-small-latest`, `mistral-large-latest`).
- 🔄 **Отказоустойчивость и ротация ключей**:
  - Поддержка пула нескольких API-ключей с автоматическим переключением при исчерпании лимитов (`429 Too Many Requests`).
  - Мгновенный Fallback с Gemini на Mistral AI (и наоборот) без прерывания сканирования.
- 🎯 **Мульти-резюме (Автовыбор ИИ)**: Если у вас несколько резюме на hh.ru (например, *Python Backend*, *Team Lead*, *ML Engineer*), нейросеть выберет наиболее релевантное резюме под конкретную вакансию.
- ✍️ **Персонализированные сопроводительные письма**:
  - Генерация кратких (3–5 предложений), емких писем строго под требования вакансии.
  - Никаких шаблонных фраз («Прошу рассмотреть...», «Стрессоустойчивый...») и выдуманного опыта.
- ❓ **Автоматические ответы на вопросы работодателя**:
  - ИИ анализирует вопросы скрининга (зарплата, удаленка, локация, опыт со стеком, готовность к тестовому) и отвечает на основе профиля кандидата.
  - **Режим проверки (`needs_answers`)**: если ИИ не уверен в ответе (<85%), вакансия попадает на подтверждение пользователю.
- ⚡ **Быстрый отклик по ссылке (Quick Apply)**: Вставьте ссылку на любую понравившуюся вакансию в интерфейс — ИИ мгновенно оценит её, подберет резюме, ответит на вопросы и отправит отклик.
- 🛡 **Безопасный режим (Dry Run)**: Возможность запуска в режиме симуляции для предварительного просмотра оценок и сгенерированных писем без реальной отправки откликов.
- 🖥 **Современный Dark UI**: Нативное десктопное приложение (PyWebview + FastAPI) с красивым неоновым интерфейсом, статистикой, фильтрацией и редактируемым системным промптом.

---

## 🏗 Архитектура проекта

```
hh_job_applier/
├── src/                               # Исходный код приложения
│   ├── core/                          # Системное ядро (конфигурация, пути данных)
│   │   ├── config.py
│   │   └── paths.py
│   ├── db/                            # База данных SQLite (хранение вакансий, настроек, профиля)
│   │   └── database.py
│   ├── clients/                       # Клиентский слой
│   │   ├── browser.py                 # Playwright-автоматизация hh.ru
│   │   └── llm.py                     # Анализатор Gemini / Mistral AI
│   ├── pipeline/                      # Логика конвейера откликов
│   │   └── runner.py
│   ├── api/                           # FastAPI сервер и маршруты
│   │   ├── app.py
│   │   ├── state.py
│   │   └── routes/                    # Модульные роуты (auth, vacancies, settings, pipeline)
│   └── desktop/                       # Десктопный лаунчер (PyWebview + Uvicorn)
│       └── launcher.py
├── static/                            # Фронтенд интерфейса (HTML, CSS, JS)
├── assets/                            # Иконки и графические ресурсы (.icns, .ico, .png)
├── scripts/                           # Вспомогательные утилиты разработки
├── tests/                             # Автоматические unit и интеграционные тесты
├── desktop.py                         # Главная точка входа (GUI)
├── main.py                            # Главная точка входа (CLI)
├── hh_applier.spec                    # Спецификация сборщика PyInstaller
├── build_mac.sh                       # Скрипт сборки macOS .app
├── build_win.bat                      # Скрипт сборки Windows .exe
└── .github/workflows/build.yml        # CI/CD автосборка в GitHub Actions
```

---

## 📦 Установка и запуск

### Требования
- **Python 3.11+**
- Установленный браузер **Google Chrome** или **Microsoft Edge**

### 1. Клонирование репозитория
```bash
git clone https://github.com/andreimelneichuk/hh_ai_applier.git
cd hh_ai_applier
```

### 2. Создание виртуального окружения и установка зависимостей
```bash
# Создание venv
python3 -m venv venv

# Активация (macOS / Linux):
source venv/bin/activate

# Активация (Windows PowerShell / CMD):
# venv\Scripts\activate

# Установка зависимостей
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Настройка API-ключей нейросетей
Создайте файл `.env` на основе примера:
```bash
cp .env.example .env
```
Заполните ключи в `.env` (или укажите их позже прямо в интерфейсе в разделе «Настройки»):
```env
# Gemini API Key (бесплатно в Google AI Studio: https://aistudio.google.com/)
GEMINI_API_KEYS=AIzaSy...,AIzaSy...
GEMINI_MODEL=gemini-3.6-flash

# Mistral API Key (опционально, https://console.mistral.ai/)
MISTRAL_API_KEYS=your_mistral_api_key
MISTRAL_MODEL=mistral-small-latest
```

---

## 🚀 Запуск приложения

### Десктопный режим (GUI)
```bash
python desktop.py
```
Откроется нативное окно приложения.

### Консольный режим (CLI)
```bash
python main.py
```

### Запуск только веб-сервера (для работы через обычный браузер)
```bash
python -m uvicorn src.api.app:app --host 127.0.0.1 --port 8000 --reload
```
Интерфейс будет доступен по адресу: `http://localhost:8000`.

---

## 🔑 Авторизация на hh.ru

1. При первом запуске нажмите кнопку **«Войти в HH.ru»** в верхнем меню.
2. В открывшемся окне браузера авторизуйтесь в свой аккаунт HeadHunter.
3. Закройте окно браузера — сессия сохранится навсегда в системную папку приложения:
   - **macOS**: `~/Library/Application Support/HH_AI_Applier/playwright_session`
   - **Windows**: `%APPDATA%/HH_AI_Applier/playwright_session`

---

## 🛠 Сборка бинарных файлов (.app / .exe)

### Локальная сборка для macOS:
```bash
bash build_mac.sh
```
Готовое приложение появится в папке `dist/HH_AI_Applier.app`.

### Локальная сборка для Windows:
Запустите командный файл:
```bat
build_win.bat
```
Готовый исполняемый файл появится в `dist\HH_AI_Applier\HH_AI_Applier.exe`.

### Автоматическая сборка в облаке (GitHub Actions):
При пуше в репозиторий автоматически запускается сборка под обе платформы. Скачать готовые `.zip` архивы можно во вкладке **Actions** → выберите последний запуск → блок **Artifacts**.

---

## 🧪 Запуск тестов

Проект покрыт автоматическими тестами API, логики скоринга, ротации ключей и ответов на вопросы:
```bash
python -m unittest discover -s tests
```

---

## 📄 Лицензия

Проект распространяется под лицензией **MIT**. Подробности в файле `LICENSE`.
