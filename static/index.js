// Глобальное состояние
let currentJobs = [];
let userSettings = {};
let isPolling = false;
let pollInterval = null;
let isStatusPolling = false;
let statusInterval = null;
let currentFilter = "all";
let currentOffset = 0;
const itemsPerPage = 50;

// Пул API ключей (Gemini и Mistral)
let currentGeminiKeys = [];
let currentMistralKeys = [];
let currentApiKeys = currentGeminiKeys; // для обратной совместимости
let activeKeyManagerTab = "gemini"; // "gemini" | "mistral"
let keyStatusesMap = {}; // key -> status object

// Инициализация при загрузке
document.addEventListener("DOMContentLoaded", () => {
    initApp();
    initLiquidGlassInteractivity();
});

let settingsLoaded = false;

async function initApp() {
    setupEventListeners();
    
    // Мгновенно восстанавливаем закэшированное состояние
    const cachedJobs = localStorage.getItem("cached_jobs");
    const cachedStats = localStorage.getItem("cached_stats");
    
    let hasCache = false;
    if (cachedJobs && cachedStats) {
        try {
            currentJobs = JSON.parse(cachedJobs);
            const stats = JSON.parse(cachedStats);
            renderJobsList(false); // рендерим без очистки
            renderStatsDom(stats);
            hasCache = true;
        } catch (e) {
            console.error("Error parsing cached state", e);
        }
    }
    
    if (!hasCache) {
        renderSkeletons();
        setStatsLoading(true);
    }
    
    await loadSettings();
    await loadJobs(!hasCache); // если кэша нет, сбрасываем, иначе тихо обновляем
    await checkStatus();
    
    // Периодическая проверка статуса и фоновое обновление счетчиков
    statusInterval = setInterval(async () => {
        await checkStatus();
        if (!isPolling) {
            await loadJobs(false, false);
        }
    }, 10000);
}

// Установка обработчиков событий
function setupEventListeners() {
    // Кнопка авторизации
    const loginBtn = document.getElementById("login-btn");
    if (loginBtn) {
        loginBtn.addEventListener("click", triggerBrowserLogin);
    }

    // Изменение ползунка порога
    const range = document.getElementById("threshold-range");
    const valBadge = document.getElementById("threshold-val");
    if (range && valBadge) {
        range.addEventListener("input", (e) => {
            valBadge.textContent = `${e.target.value}%`;
        });
    }

    // Переключатель Dry Run
    const dryRunToggle = document.getElementById("dryrun-toggle");
    if (dryRunToggle) {
        dryRunToggle.addEventListener("change", (e) => {
            updateDryRunBadge(e.target.checked);
        });
    }

    // Сохранение настроек
    const form = document.getElementById("settings-form");
    if (form) {
        form.addEventListener("submit", saveSettings);
    }

    // Кнопка запуска сканирования
    const scanBtn = document.getElementById("start-scan-btn");
    if (scanBtn) {
        scanBtn.addEventListener("click", startScanning);
    }

    // Кнопка остановки сканирования
    const stopBtn = document.getElementById("stop-scan-btn");
    if (stopBtn) {
        stopBtn.addEventListener("click", stopScanning);
    }
    const quickStopBtn = document.getElementById("quick-stop-btn");
    if (quickStopBtn) {
        quickStopBtn.addEventListener("click", stopScanning);
    }

    // Карточки статистики как интерактивные вкладки фильтрации вакансий
    const filterCards = document.querySelectorAll(".stat-filter-btn");
    filterCards.forEach(card => {
        card.addEventListener("click", async (e) => {
            const targetCard = e.currentTarget;
            const filterType = targetCard.dataset.filter;
            if (filterType === currentFilter) return;
            
            filterCards.forEach(c => c.classList.remove("active"));
            targetCard.classList.add("active");
            
            currentFilter = filterType;
            currentOffset = 0;
            currentJobs = [];
            
            // Обновляем заголовок секции
            const titleEl = document.getElementById("current-filter-title");
            if (titleEl) {
                const labelText = targetCard.querySelector(".stat-label")?.textContent?.replace(" ↺", "").replace(" ❓", "").trim() || "Все";
                titleEl.textContent = `Обработанные вакансии: ${labelText}`;
            }
            
            // Сразу скрываем кнопку групповой обработки, чтобы не моргала
            const reanalyzeBtn = document.getElementById("reanalyze-all-failed-btn");
            if (reanalyzeBtn) reanalyzeBtn.classList.add("hide");
            
            renderSkeletons();
            await loadJobs(true);
        });
    });

    // Кнопка "Переоценить все ошибки"
    const reanalyzeAllBtn = document.getElementById("reanalyze-all-failed-btn");
    if (reanalyzeAllBtn) {
        reanalyzeAllBtn.addEventListener("click", async () => {
            const confirmed = await showConfirm("Запустить переоценку всех вакансий с ошибками? Это может занять некоторое время.");
            if (!confirmed) {
                return;
            }
            
            reanalyzeAllBtn.setAttribute("disabled", "true");
            reanalyzeAllBtn.textContent = "Запуск переоценки...";
            
            try {
                const response = await fetch("/api/reanalyze-all-failed", { method: "POST" });
                const data = await response.json();
                
                if (response.ok && data.status === "started") {
                    window.hasReportedCompletion = false;
                    setScanningState(true);
                    showToast(data.message || "Переоценка запущена в фоновом режиме", "info");
                } else if (response.ok && data.status === "ok") {
                    showToast(data.message || "Нет вакансий для переоценки", "info");
                    reanalyzeAllBtn.removeAttribute("disabled");
                    reanalyzeAllBtn.textContent = "↺ Переоценить все ошибки";
                } else {
                    showToast("Ошибка: " + (data.detail || data.message || "не удалось запустить переоценку."), "error");
                    reanalyzeAllBtn.removeAttribute("disabled");
                    reanalyzeAllBtn.textContent = "↺ Переоценить все ошибки";
                }
            } catch (e) {
                console.error("Error reanalyzing all failed:", e);
                showToast("Сетевая ошибка при запуске переоценки.", "error");
                reanalyzeAllBtn.removeAttribute("disabled");
                reanalyzeAllBtn.textContent = "↺ Переоценить все ошибки";
            }
        });
    }

    // Кнопка "Показать ещё"
    const loadMoreBtn = document.getElementById("load-more-btn");
    if (loadMoreBtn) {
        loadMoreBtn.addEventListener("click", async () => {
            currentOffset += itemsPerPage;
            loadMoreBtn.setAttribute("disabled", "true");
            loadMoreBtn.textContent = "Загрузка...";
            await loadJobs(false, true); // догружаем к текущему списку
            loadMoreBtn.removeAttribute("disabled");
            loadMoreBtn.textContent = "Показать ещё";
        });
    }

    // Закрытие модального окна вакансии
    const closeBtn = document.getElementById("modal-close-btn");
    const modal = document.getElementById("vacancy-modal");
    if (closeBtn && modal) {
        closeBtn.addEventListener("click", () => {
            modal.classList.add("hide");
        });
        
        // Закрытие по клику вне модалки
        window.addEventListener("click", (e) => {
            if (e.target === modal) {
                modal.classList.add("hide");
            }
        });
    }

    // Быстрый отклик по ссылке / ID
    const quickApplyBtn = document.getElementById("quick-apply-btn");
    const quickApplyInput = document.getElementById("quick-apply-url-input");

    if (quickApplyBtn && quickApplyInput) {
        quickApplyBtn.addEventListener("click", () => {
            handleQuickApply(quickApplyInput.value.trim());
        });
        quickApplyInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                handleQuickApply(quickApplyInput.value.trim());
            }
        });
    }

    // Менеджер API Ключей
    const openKeyManagerBtn = document.getElementById("open-key-manager-btn");
    const sysOpenKeyManagerBtn = document.getElementById("sys-open-key-manager-btn");
    const keyManagerModal = document.getElementById("key-manager-modal");
    const closeKeyManagerBtn = document.getElementById("key-manager-close-btn");
    const cancelKeyManagerBtn = document.getElementById("key-manager-cancel-btn");
    const addKeyBtn = document.getElementById("add-key-btn");
    const newKeyInput = document.getElementById("new-key-input");
    const saveKeyManagerBtn = document.getElementById("key-manager-save-btn");
    const probeAllKeysBtn = document.getElementById("probe-all-keys-btn");
    const tabGeminiBtn = document.getElementById("tab-gemini-btn");
    const tabMistralBtn = document.getElementById("tab-mistral-btn");

    if (openKeyManagerBtn && keyManagerModal) {
        openKeyManagerBtn.addEventListener("click", () => {
            openKeyManager();
        });
    }

    if (sysOpenKeyManagerBtn && keyManagerModal) {
        sysOpenKeyManagerBtn.addEventListener("click", () => {
            openKeyManager();
        });
    }

    if (closeKeyManagerBtn && keyManagerModal) {
        closeKeyManagerBtn.addEventListener("click", () => {
            keyManagerModal.classList.add("hide");
        });
    }

    if (cancelKeyManagerBtn && keyManagerModal) {
        cancelKeyManagerBtn.addEventListener("click", () => {
            keyManagerModal.classList.add("hide");
        });
    }

    if (tabGeminiBtn) {
        tabGeminiBtn.addEventListener("click", () => {
            switchKeyManagerTab("gemini");
        });
    }

    if (tabMistralBtn) {
        tabMistralBtn.addEventListener("click", () => {
            switchKeyManagerTab("mistral");
        });
    }

    if (addKeyBtn && newKeyInput) {
        addKeyBtn.addEventListener("click", () => {
            addNewKeyFromInput();
        });
        newKeyInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                addNewKeyFromInput();
            }
        });
    }

    if (saveKeyManagerBtn) {
        saveKeyManagerBtn.addEventListener("click", async () => {
            await saveKeyManagerChanges();
        });
    }

    if (probeAllKeysBtn) {
        probeAllKeysBtn.addEventListener("click", async () => {
            await probeAllKeysStatus();
        });
    }

    // Кнопка обновления списка моделей
    const refreshModelsBtn = document.getElementById("refresh-models-btn");
    const sysRefreshModelsBtn = document.getElementById("sys-refresh-models-btn");
    
    async function handleRefreshModels(btn) {
        const originalText = btn ? btn.textContent : "Обновить";
        if (btn) btn.textContent = "Загрузка...";
        await loadModelsDropdown();
        if (btn) btn.textContent = originalText;
        showToast("Список доступных моделей обновлен", "info");
    }

    if (refreshModelsBtn) {
        refreshModelsBtn.addEventListener("click", () => handleRefreshModels(refreshModelsBtn));
    }
    if (sysRefreshModelsBtn) {
        sysRefreshModelsBtn.addEventListener("click", () => handleRefreshModels(sysRefreshModelsBtn));
    }

    // Системные настройки LLM
    const openSystemSettingsBtn = document.getElementById("open-system-settings-btn");
    const openSystemSettingsSidebarBtn = document.getElementById("open-system-settings-sidebar-btn");
    const systemSettingsModal = document.getElementById("system-settings-modal");
    const closeSystemSettingsBtn = document.getElementById("system-settings-close-btn");
    const cancelSystemSettingsBtn = document.getElementById("system-settings-cancel-btn");
    const saveSystemSettingsBtn = document.getElementById("system-settings-save-btn");
    const resetPromptBtn = document.getElementById("sys-reset-prompt-btn");
    const tempSlider = document.getElementById("sys-temperature");
    const tempValue = document.getElementById("sys-temp-value");
    const promptEditor = document.getElementById("sys-prompt-editor");

    if (openSystemSettingsBtn) {
        openSystemSettingsBtn.addEventListener("click", () => {
            openSystemSettings();
        });
    }

    if (openSystemSettingsSidebarBtn) {
        openSystemSettingsSidebarBtn.addEventListener("click", () => {
            openSystemSettings();
        });
    }

    if (closeSystemSettingsBtn && systemSettingsModal) {
        closeSystemSettingsBtn.addEventListener("click", () => {
            systemSettingsModal.classList.add("hide");
        });
    }

    if (cancelSystemSettingsBtn && systemSettingsModal) {
        cancelSystemSettingsBtn.addEventListener("click", () => {
            systemSettingsModal.classList.add("hide");
        });
    }

    if (tempSlider && tempValue) {
        tempSlider.addEventListener("input", (e) => {
            tempValue.textContent = parseFloat(e.target.value).toFixed(2);
        });
    }

    if (resetPromptBtn) {
        resetPromptBtn.addEventListener("click", async () => {
            await resetSystemPrompt();
        });
    }

    if (saveSystemSettingsBtn) {
        saveSystemSettingsBtn.addEventListener("click", async () => {
            await saveSystemSettings();
        });
    }

    // База ответов профиля (FAQ)
    const openProfileAnswersBtn = document.getElementById("open-profile-answers-btn");
    const profileAnswersModal = document.getElementById("profile-answers-modal");
    const closeProfileAnswersBtn = document.getElementById("profile-answers-close-btn");
    const cancelProfileAnswersBtn = document.getElementById("profile-answers-cancel-btn");
    const saveNewAnswerBtn = document.getElementById("save-new-answer-btn");

    if (openProfileAnswersBtn) {
        openProfileAnswersBtn.addEventListener("click", () => {
            openProfileAnswersModal();
        });
    }

    if (closeProfileAnswersBtn && profileAnswersModal) {
        closeProfileAnswersBtn.addEventListener("click", () => {
            profileAnswersModal.classList.add("hide");
        });
    }

    if (cancelProfileAnswersBtn && profileAnswersModal) {
        cancelProfileAnswersBtn.addEventListener("click", () => {
            profileAnswersModal.classList.add("hide");
        });
    }

    if (saveNewAnswerBtn) {
        saveNewAnswerBtn.addEventListener("click", async () => {
            await saveNewProfileAnswer();
        });
    }

    // Обработчик вставки переменных в промпт по клику на тег
    const varTags = document.querySelectorAll(".prompt-var-tag");
    varTags.forEach(tag => {
        tag.addEventListener("click", () => {
            const varText = tag.getAttribute("data-var");
            if (promptEditor && varText) {
                insertAtCursor(promptEditor, varText);
            }
        });
    });

    // Клик по статусу LLM провайдеров в сайдбаре открывает Менеджер ключей
    const aiStatusBox = document.querySelector(".ai-status-indicator");
    if (aiStatusBox) {
        aiStatusBox.style.cursor = "pointer";
        aiStatusBox.setAttribute("title", "Нажмите для управления API ключами");
        aiStatusBox.addEventListener("click", () => {
            openKeyManager();
        });
    }

    // Универсальное закрытие модальных окон по клику на фон (backdrop)
    document.querySelectorAll(".modal").forEach(m => {
        m.addEventListener("click", (e) => {
            if (e.target === m) {
                m.classList.add("hide");
            }
        });
    });

    // Закрытие активного модального окна по нажатию клавиши Escape
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            const visibleModals = document.querySelectorAll(".modal:not(.hide)");
            visibleModals.forEach(m => m.classList.add("hide"));
        }
    });
}

// Запуск браузера авторизации
async function triggerBrowserLogin() {
    const loginBtn = document.getElementById("login-btn");
    loginBtn.setAttribute("disabled", "true");
    loginBtn.textContent = "Запуск браузера...";
    
    try {
        const response = await fetch("/api/browser/login", { method: "POST" });
        const data = await response.json();
        
        if (data.status === "opened" || data.status === "already_open") {
            document.getElementById("auth-status-text").textContent = "Пройдите вход в открывшемся окне браузера и закройте его.";
            loginBtn.textContent = "Браузер входа открыт...";
        } else {
            showToast("Не удалось запустить браузер.", "error");
            loginBtn.removeAttribute("disabled");
            loginBtn.textContent = "Открыть браузер для входа";
        }
    } catch (e) {
        console.error("Error triggering browser login:", e);
        loginBtn.removeAttribute("disabled");
        loginBtn.textContent = "Открыть браузер для входа";
    }
}

// Проверка статуса подключения к hh.ru
async function checkStatus() {
    try {
        const [response, modelResponse] = await Promise.all([
            fetch("/api/status"),
            fetch("/api/model-status").catch(() => ({ ok: false }))
        ]);
        const data = await response.json();
        
        let modelData = { status: "error" };
        if (modelResponse && modelResponse.ok) {
            modelData = await modelResponse.json();
        }
        
        if (!settingsLoaded) {
            await loadSettings();
        }
        
        const loginBtn = document.getElementById("login-btn");
        const userInfo = document.getElementById("user-info");
        const botStatusDot = document.getElementById("bot-status-dot");
        const startBtn = document.getElementById("start-scan-btn");
        const statusText = document.getElementById("auth-status-text");
        
        // Обработка статуса AI
        const aiDot = document.getElementById("ai-status-dot");
        const aiLabel = document.getElementById("ai-status-label");
        let aiCanStart = true;
        
        if (modelData.status === "ok") {
            aiDot.className = "pulse-dot active";
            aiDot.style.backgroundColor = ""; // reset to css
            
            const gAvail = modelData.gemini ? modelData.gemini.available : 0;
            const gTotal = modelData.gemini ? modelData.gemini.total : 0;
            const mAvail = modelData.mistral ? modelData.mistral.available : 0;
            const mTotal = modelData.mistral ? modelData.mistral.total : 0;

            if (gTotal > 0 && mTotal > 0) {
                if (gAvail > 0 && mAvail > 0) {
                    aiLabel.textContent = `Gemini (${gAvail}/${gTotal}) + Mistral (${mAvail}/${mTotal})`;
                } else if (gAvail > 0) {
                    aiLabel.textContent = `Gemini (${gAvail}/${gTotal})`;
                } else {
                    aiLabel.textContent = `Резерв Mistral (${mAvail}/${mTotal})`;
                }
            } else if (gTotal > 0) {
                aiLabel.textContent = `Gemini (${gAvail}/${gTotal})`;
            } else if (mTotal > 0) {
                aiLabel.textContent = `Mistral (${mAvail}/${mTotal})`;
            } else {
                aiLabel.textContent = "Доступно (OK)";
            }
            aiLabel.style.color = "var(--accent-green)";
        } else if (modelData.status === "mock") {
            aiDot.className = "pulse-dot";
            aiDot.style.backgroundColor = "var(--accent-blue)";
            aiDot.style.boxShadow = "0 0 10px var(--accent-blue)";
            aiLabel.textContent = "Mock-режим (без API)";
            aiLabel.style.color = "var(--accent-blue)";
        } else {
            aiDot.className = "pulse-dot";
            aiDot.style.backgroundColor = "var(--accent-red)";
            aiDot.style.boxShadow = "0 0 10px var(--accent-red)";
            const total = modelData.total !== undefined ? modelData.total : 0;
            aiLabel.textContent = total > 0 ? `Лимиты исчерпаны (0/${total})` : "Ключи не настроены";
            aiLabel.style.color = "var(--accent-red)";
            aiCanStart = false;
        }

        if (data.authorized) {
            // Пользователь вошел
            loginBtn.classList.add("hide");
            userInfo.classList.remove("hide");
            botStatusDot.classList.add("active");
            statusText.textContent = "Подключение активно";
            
            if (aiCanStart) {
                startBtn.removeAttribute("disabled");
                startBtn.removeAttribute("title");
            } else {
                startBtn.setAttribute("disabled", "true");
                startBtn.title = "Невозможно запустить сканирование: API-ключ невалиден или исчерпаны лимиты.";
                if (window.wasAiAvailable !== false) {
                    showToast("Gemini API недоступен. Проверьте лимиты или ключ.", "error");
                    window.wasAiAvailable = false;
                }
            }
            if (aiCanStart) window.wasAiAvailable = true;
            
            // Наполняем ФИО
            const u = data.user;
            const fullName = u.first_name || "Пользователь HH";
            document.getElementById("user-fullname").textContent = fullName;
            document.getElementById("user-email-addr").textContent = u.email || "";
            
            // Инициалы
            const initials = fullName.split(" ").slice(0, 2).map(w => w[0] || "").join("").toUpperCase();
            document.getElementById("user-initials").textContent = initials || "HH";
            
            // Подгружаем настройки только при первой загрузке
            if (!settingsLoaded) {
                await loadSettings();
            }
            
            // Синхронизируем состояние сканирования UI
            if (data.pipeline && data.pipeline.is_running) {
                setScanningState(true);
                if (!isPolling) {
                    startRealtimePolling();
                }
            } else if (isPolling) {
                setScanningState(false);
                await loadJobs(true);
            }
            
            // Обновляем плашку текущей обработки
            updateProcessingStatus(data.pipeline);
        } else {
            // Требуется вход
            loginBtn.classList.remove("hide");
            userInfo.classList.add("hide");
            botStatusDot.classList.remove("active");
            startBtn.setAttribute("disabled", "true");
            startBtn.title = "Для запуска необходимо войти в аккаунт HH.ru";
            
            if (data.login_active) {
                loginBtn.setAttribute("disabled", "true");
                loginBtn.textContent = "Браузер входа открыт...";
                statusText.textContent = "Пройдите вход в открывшемся окне браузера и закройте его.";
            } else {
                loginBtn.removeAttribute("disabled");
                loginBtn.textContent = "Открыть браузер для входа";
                statusText.textContent = "Авторизация отсутствует.";
            }
        }
    } catch (e) {
        console.error("Error checking status:", e);
    }
}

// Загрузка настроек поиска
async function loadSettings() {
    try {
        const response = await fetch("/api/settings");
        userSettings = await response.json();
        
        document.getElementById("queries-input").value = userSettings.queries.join(", ");
        document.getElementById("area-select").value = userSettings.area_id;
        document.getElementById("threshold-range").value = userSettings.threshold;
        document.getElementById("threshold-val").textContent = `${userSettings.threshold}%`;
        
        const dryRunToggle = document.getElementById("dryrun-toggle");
        dryRunToggle.checked = userSettings.dry_run;
        updateDryRunBadge(userSettings.dry_run);
        
        if (userSettings.gemini_api_keys !== undefined || userSettings.mistral_api_keys !== undefined) {
            updateKeysPoolFromSettings(userSettings.gemini_api_keys, userSettings.mistral_api_keys);
        }
        
        // Подгружаем список доступных моделей
        await loadModelsDropdown(userSettings.gemini_model, userSettings.mistral_model);
        
        // Подгружаем список резюме
        await loadResumesDropdown(userSettings.resume_id);
        settingsLoaded = true;
    } catch (e) {
        console.error("Error loading settings:", e);
    }
}

// Загрузка доступных моделей Gemini и Mistral в выпадающие списки
async function loadModelsDropdown(selectedGeminiModel, selectedMistralModel) {
    const geminiSelects = [
        document.getElementById("sys-gemini-model-select"),
        document.getElementById("model-select")
    ].filter(Boolean);
    
    const mistralSelects = [
        document.getElementById("sys-mistral-model-select"),
        document.getElementById("mistral-model-select")
    ].filter(Boolean);
    
    try {
        const response = await fetch("/api/models");
        if (response.ok) {
            const data = await response.json();
            
            // Заполнение Gemini моделей
            const geminiModels = data.gemini || (Array.isArray(data.models) ? data.models : ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-flash-latest", "gemini-3.1-pro-preview"]);
            geminiSelects.forEach(sel => {
                const currentVal = selectedGeminiModel || sel.value || "gemini-3.6-flash";
                sel.innerHTML = "";
                geminiModels.forEach(m => {
                    const opt = document.createElement("option");
                    opt.value = m;
                    let desc = m;
                    if (m === "gemini-3.6-flash") desc += " (Рекомендуемая, быстрая)";
                    else if (m === "gemini-3.5-flash") desc += " (Flash 3.5)";
                    else if (m === "gemini-flash-latest") desc += " (Flash Latest)";
                    else if (m.includes("pro")) desc += " (Pro - макс. интеллект)";
                    opt.textContent = desc;
                    sel.appendChild(opt);
                });
                
                if (geminiModels.includes(currentVal)) {
                    sel.value = currentVal;
                } else if (sel.options.length > 0) {
                    sel.value = sel.options[0].value;
                }
            });

            // Заполнение Mistral моделей
            const mistralModels = data.mistral || ["mistral-small-latest", "mistral-large-latest", "mistral-medium-latest", "open-mistral-nemo", "codestral-latest"];
            mistralSelects.forEach(sel => {
                const currentMistralVal = selectedMistralModel || sel.value || "mistral-small-latest";
                sel.innerHTML = "";
                mistralModels.forEach(m => {
                    const opt = document.createElement("option");
                    opt.value = m;
                    let desc = m;
                    if (m === "mistral-small-latest") desc += " (Быстрая, рекомендуемая)";
                    else if (m === "mistral-large-latest") desc += " (Максимальное качество)";
                    else if (m === "codestral-latest") desc += " (Для кода и IT)";
                    opt.textContent = desc;
                    sel.appendChild(opt);
                });

                if (mistralModels.includes(currentMistralVal)) {
                    sel.value = currentMistralVal;
                } else if (sel.options.length > 0) {
                    sel.value = sel.options[0].value;
                }
            });
        }
    } catch (e) {
        console.error("Error loading models:", e);
    }
}

function updateDryRunBadge(isDryRun) {
    const badge = document.getElementById("dryrun-badge");
    if (badge) {
        if (isDryRun) {
            badge.classList.remove("hide");
        } else {
            badge.classList.add("hide");
        }
    }
}

// Подгрузка резюме в выпадающий список
async function loadResumesDropdown(selectedResumeId) {
    const select = document.getElementById("resume-select");
    const resumeGroup = document.getElementById("resume-group");
    
    try {
        const response = await fetch("/api/resumes");
        const data = await response.json();
        
        select.innerHTML = "";
        
        // Добавляем пункт "Все резюме (Автовыбор ИИ)"
        const allOpt = document.createElement("option");
        allOpt.value = "all";
        allOpt.textContent = "✨ Все резюме (Автовыбор ИИ)";
        select.appendChild(allOpt);
        
        if (data.resumes && data.resumes.length > 0) {
            data.resumes.forEach(r => {
                const opt = document.createElement("option");
                opt.value = r.id;
                opt.dataset.title = r.title;
                opt.textContent = `📄 ${r.title}`;
                select.appendChild(opt);
            });
            
            if (selectedResumeId && selectedResumeId !== "all") {
                let matched = false;
                for (let opt of select.options) {
                    if (opt.value === selectedResumeId || opt.dataset.title === selectedResumeId) {
                        select.value = opt.value;
                        matched = true;
                        break;
                    }
                }
                if (!matched) {
                    select.value = "all";
                }
            } else {
                select.value = "all";
            }
            resumeGroup.classList.remove("hide");
        } else {
            select.value = "all";
            resumeGroup.classList.remove("hide");
        }
    } catch (e) {
        console.error("Error loading resumes:", e);
        select.innerHTML = '<option value="all">✨ Все резюме (Автовыбор ИИ)</option>';
    }
}

// Сохранение настроек поиска
async function saveSettings(e) {
    e.preventDefault();
    
    const queries = document.getElementById("queries-input").value
        .split(",")
        .map(q => q.trim())
        .filter(Boolean);
        
    const geminiKeys = currentGeminiKeys.join(",");
    const mistralKeys = currentMistralKeys.join(",");
    const modelSelect = document.getElementById("sys-gemini-model-select") || document.getElementById("model-select");
    const selectedModel = modelSelect ? modelSelect.value : (userSettings.gemini_model || "gemini-3.6-flash");
    const mistralModelSelect = document.getElementById("sys-mistral-model-select") || document.getElementById("mistral-model-select");
    const selectedMistralModel = mistralModelSelect ? mistralModelSelect.value : (userSettings.mistral_model || "mistral-small-latest");

    const payload = {
        queries: queries,
        area_id: document.getElementById("area-select").value,
        threshold: parseInt(document.getElementById("threshold-range").value, 10),
        resume_id: document.getElementById("resume-select").value,
        dry_run: document.getElementById("dryrun-toggle").checked,
        gemini_api_keys: geminiKeys,
        gemini_model: selectedModel,
        mistral_api_keys: mistralKeys,
        mistral_model: selectedMistralModel
    };
    
    try {
        const response = await fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        
        if (response.ok) {
            const btn = document.getElementById("save-settings-btn");
            const originalText = btn.textContent;
            btn.textContent = "Сохранено ✓";
            btn.style.backgroundColor = "var(--accent-green)";
            
            setTimeout(() => {
                btn.textContent = originalText;
                btn.style.backgroundColor = "";
            }, 2000);
            
            userSettings = payload;
        }
    } catch (e) {
        console.error("Error saving settings:", e);
    }
}

// Запуск сканирования
async function startScanning() {
    const btn = document.getElementById("start-scan-btn");
    if (btn.hasAttribute("disabled") || isPolling) return;
    
    try {
        const response = await fetch("/api/search", { method: "POST" });
        const data = await response.json();
        
        if (data.status === "started") {
            window.hasReportedCompletion = false;
            setScanningState(true);
            showToast("Сканирование и анализ вакансий запущены", "info");
        } else {
            showToast("Ошибка при запуске: " + (data.message || "попробуйте еще раз."), "error");
        }
    } catch (e) {
        console.error("Error starting search:", e);
        showToast("Сетевая ошибка при запуске сканирования", "error");
    }
}

// Остановка сканирования
async function stopScanning() {
    const stopBtn = document.getElementById("stop-scan-btn");
    const quickStopBtn = document.getElementById("quick-stop-btn");
    
    if (stopBtn) {
        stopBtn.setAttribute("disabled", "true");
        const loader = stopBtn.querySelector(".loader");
        const text = stopBtn.querySelector(".btn-text");
        if (loader) loader.classList.remove("hide");
        if (text) text.textContent = "Останавливаем...";
    }
    if (quickStopBtn) {
        quickStopBtn.setAttribute("disabled", "true");
        quickStopBtn.textContent = "Остановка...";
    }
    
    showToast("Запрос на остановку отправлен. Завершаем текущий шаг...", "info");
    
    try {
        const response = await fetch("/api/stop", { method: "POST" });
        const data = await response.json();
        if (data.status === "stopping") {
            // Ожидаем в polling
        }
    } catch (e) {
        console.error("Error stopping search:", e);
        showToast("Ошибка связи при попытке остановить сканирование", "error");
    }
}

// Визуальное состояние сканирования / переоценки
function setScanningState(running) {
    const startBtn = document.getElementById("start-scan-btn");
    const stopBtn = document.getElementById("stop-scan-btn");
    const quickStopBtn = document.getElementById("quick-stop-btn");
    const reanalyzeAllBtn = document.getElementById("reanalyze-all-failed-btn");
    const startLoader = startBtn ? startBtn.querySelector(".loader") : null;
    const startText = startBtn ? startBtn.querySelector(".btn-text") : null;
    const stopLoader = stopBtn ? stopBtn.querySelector(".loader") : null;
    const stopText = stopBtn ? stopBtn.querySelector(".btn-text") : null;
    
    if (running) {
        if (startBtn) startBtn.classList.add("hide");
        if (stopBtn) {
            stopBtn.classList.remove("hide");
            stopBtn.removeAttribute("disabled");
            if (stopLoader) stopLoader.classList.add("hide");
            if (stopText) stopText.textContent = "⏹ Остановить анализ";
        }
        if (quickStopBtn) {
            quickStopBtn.removeAttribute("disabled");
            quickStopBtn.textContent = "⏹ Остановить";
        }
        if (reanalyzeAllBtn) {
            reanalyzeAllBtn.setAttribute("disabled", "true");
            reanalyzeAllBtn.textContent = "Идет анализ...";
        }
        
        startRealtimePolling();
    } else {
        if (stopBtn) {
            stopBtn.classList.add("hide");
            if (stopLoader) stopLoader.classList.add("hide");
        }
        if (startBtn) {
            startBtn.classList.remove("hide");
            startBtn.removeAttribute("disabled");
            if (startLoader) startLoader.classList.add("hide");
            if (startText) startText.textContent = "Запустить сканирование";
        }
        if (quickStopBtn) {
            quickStopBtn.removeAttribute("disabled");
            quickStopBtn.textContent = "⏹ Остановить";
        }
        if (reanalyzeAllBtn) {
            reanalyzeAllBtn.removeAttribute("disabled");
            reanalyzeAllBtn.textContent = "↺ Переоценить все ошибки";
        }
        
        stopRealtimePolling();
    }
}

// Запуск частого опроса (раз в 1.5 секунды)
function startRealtimePolling() {
    if (!isPolling) {
        isPolling = true;
        // Опрашиваем часто для плавной реалтайм статистики
        if (pollInterval) clearInterval(pollInterval);
        pollInterval = setInterval(pollScanStatus, 1500);
    }
}

// Остановка частого опроса
function stopRealtimePolling() {
    if (isPolling) {
        clearInterval(pollInterval);
        pollInterval = null;
        isPolling = false;
    }
}

// Опрос статуса фоновой задачи
async function pollScanStatus() {
    try {
        const response = await fetch("/api/status");
        const statusData = await response.json();
        
        // Во время сканирования регулярно подгружаем актуальные вакансии и обновляем счетчики
        const currentId = statusData.pipeline?.currently_processing?.id || null;
        if (window.lastProcessingId !== currentId || !window.lastJobsPolledTime || (Date.now() - window.lastJobsPolledTime) > 3000) {
            window.lastProcessingId = currentId;
            window.lastJobsPolledTime = Date.now();
            await loadJobs(false, false);
        }
        
        // Обновляем плашку текущей обработки
        updateProcessingStatus(statusData.pipeline);
        
        if (statusData.pipeline && !statusData.pipeline.is_running && !statusData.pipeline.currently_processing) {
            setScanningState(false);
            await loadJobs(true);
            
            // Финальный сброс уведомления (показываем ровно один раз за запуск)
            if (!window.hasReportedCompletion) {
                window.hasReportedCompletion = true;
                if (statusData.pipeline.last_error) {
                    showToast(`Анализ завершен с ошибкой:\n${statusData.pipeline.last_error}`, "error");
                } else if (statusData.pipeline.last_status === "stopped") {
                    showToast("⏹ Анализ остановлен пользователем", "info");
                } else if (statusData.pipeline.last_run_stats) {
                    const s = statusData.pipeline.last_run_stats;
                    const msg = [
                        `✅ Анализ завершен!`,
                        `Обработано: ${s.processed}`,
                        `Подошли: ${s.matched}`,
                        `Откликов: ${s.applied || 0}`,
                        `Ошибок: ${s.failed || 0}`
                    ].join("\n");
                    showToast(msg, "success");
                }
            }
        }
    } catch (e) {
        console.error("Error polling status:", e);
    }
}

let currentFetchId = 0;

// Загрузка обработанных вакансий из БД (с пагинацией и защитой от race conditions)
async function loadJobs(reset = false, append = false) {
    if (reset) {
        currentOffset = 0;
        currentJobs = [];
    }
    
    const fetchId = ++currentFetchId;
    const requestedFilter = currentFilter;
    const requestedOffset = currentOffset;
    
    try {
        setStatsLoading(true);
        const response = await fetch(`/api/jobs?status=${requestedFilter}&limit=${itemsPerPage}&offset=${requestedOffset}`);
        if (!response.ok) {
            if (fetchId === currentFetchId) {
                setStatsLoading(false);
            }
            return;
        }
        const data = await response.json();
        
        // Отбрасываем устаревший ответ, если пользователь уже переключился на другую вкладку
        if (fetchId !== currentFetchId || requestedFilter !== currentFilter) {
            return;
        }
        
        const newJobs = data.jobs || [];
        
        if (append) {
            currentJobs = [...currentJobs, ...newJobs];
        } else {
            currentJobs = newJobs;
        }
        
        // Кэшируем результаты только для вкладки "Все" без догрузки
        if (requestedFilter === "all" && !append) {
            localStorage.setItem("cached_jobs", JSON.stringify(currentJobs));
            localStorage.setItem("cached_stats", JSON.stringify(data.stats));
        }
        
        setStatsLoading(false);
        renderStatsDom(data.stats);
        renderJobsList(append);
        
        // Управляем кнопкой "Переоценить все ошибки"
        const reanalyzeAllBtn = document.getElementById("reanalyze-all-failed-btn");
        if (reanalyzeAllBtn) {
            if (currentFilter === "failed" && data.stats && data.stats.failed > 0) {
                reanalyzeAllBtn.classList.remove("hide");
            } else {
                reanalyzeAllBtn.classList.add("hide");
            }
        }
        
        // Управляем видимостью кнопки "Показать ещё"
        const loadMoreWrapper = document.getElementById("load-more-wrapper");
        if (loadMoreWrapper) {
            if (newJobs.length < itemsPerPage) {
                loadMoreWrapper.classList.add("hide");
            } else {
                loadMoreWrapper.classList.remove("hide");
            }
        }
    } catch (e) {
        console.error("Error loading jobs:", e);
        if (fetchId === currentFetchId) {
            setStatsLoading(false);
        }
    }
}

// Показ пульсирующих скелетонов вместо списка
function renderSkeletons() {
    const container = document.getElementById("vacancies-container");
    container.innerHTML = "";
    
    for (let i = 0; i < 3; i++) {
        const skeleton = document.createElement("div");
        skeleton.className = "skeleton-card skeleton-pulse";
        skeleton.style.marginBottom = "12px";
        skeleton.innerHTML = `
            <div>
                <div class="skeleton-text-1"></div>
                <div class="skeleton-text-2"></div>
            </div>
            <div class="skeleton-badge"></div>
        `;
        container.appendChild(skeleton);
    }
}

// Добавление/удаление класса загрузки счетчикам
function setStatsLoading(isLoading) {
    const statValues = document.querySelectorAll(".stat-value");
    statValues.forEach(val => {
        if (isLoading) {
            val.classList.add("loading");
        } else {
            val.classList.remove("loading");
        }
    });
}

// Обновление карточек статистики
// matched = ИИ сказал "подходит" (new + applied + already_applied)
// applied = реально откликнулись
// ignored = ИИ сказал "не подходит"
// failed  = техническая ошибка анализа или отклика
// Рендеринг статистики на DOM
function renderStatsDom(stats) {
    if (!stats) return;
    document.getElementById("stat-total").textContent   = stats.total || 0;
    document.getElementById("stat-matched").textContent  = stats.matched || 0;
    const needsAnsEl = document.getElementById("stat-needs-answers");
    if (needsAnsEl) needsAnsEl.textContent = stats.needs_answers || 0;
    document.getElementById("stat-applied").textContent  = stats.applied || 0;
    document.getElementById("stat-ignored").textContent  = stats.ignored || 0;
    document.getElementById("stat-failed").textContent   = stats.failed || 0;
}

// Функция генерации заглушки логотипа на CSS-градиенте
function getCompanyLogoHtml(companyName) {
    if (!companyName) return '<div class="company-logo-placeholder">🏢</div>';
    const firstLetter = companyName.trim().charAt(0).toUpperCase();
    
    // Генерируем уникальный градиент на основе буквы
    const colors = [
        ['#3b82f6', '#1d4ed8'], // Blue
        ['#10b981', '#047857'], // Green
        ['#a855f7', '#6b21a8'], // Purple
        ['#f59e0b', '#b45309'], // Orange
        ['#ec4899', '#be185d'], // Pink
        ['#06b6d4', '#0891b2']  // Cyan
    ];
    const idx = firstLetter.charCodeAt(0) % colors.length;
    const grad = colors[idx];
    
    return `<div class="company-logo-placeholder" style="background: linear-gradient(135deg, ${grad[0]}, ${grad[1]});">${escapeHtml(firstLetter)}</div>`;
}

// Определение временной группы вакансии
function getJobTimeGroup(processedAtStr) {
    if (!processedAtStr) return "older";
    
    // SQLite пишется в локальном времени: 'YYYY-MM-DD HH:MM:SS'
    let dateObj;
    if (processedAtStr.includes("T")) {
        dateObj = new Date(processedAtStr);
    } else {
        const parts = processedAtStr.split(/[- :]/);
        if (parts.length >= 6) {
            dateObj = new Date(
                parseInt(parts[0], 10),
                parseInt(parts[1], 10) - 1,
                parseInt(parts[2], 10),
                parseInt(parts[3], 10),
                parseInt(parts[4], 10),
                parseInt(parts[5], 10)
            );
        } else {
            dateObj = new Date(processedAtStr);
        }
    }
    
    if (isNaN(dateObj.getTime())) return "older";
    
    const now = new Date();
    const diffMs = now.getTime() - dateObj.getTime();
    const diffHours = diffMs / (1000 * 60 * 60);
    
    // Последний час (до 60 минут)
    if (diffHours <= 1.0 && diffHours >= -0.1) {
        return "last_hour";
    }
    
    // Сегодня (по календарной дате)
    const isToday = now.getFullYear() === dateObj.getFullYear() &&
                    now.getMonth() === dateObj.getMonth() &&
                    now.getDate() === dateObj.getDate();
    if (isToday) {
        return "today";
    }
    
    // До 2 дней
    const diffDays = diffMs / (1000 * 60 * 60 * 24);
    if (diffDays <= 2) {
        return "two_days";
    }
    
    // До недели (до 7 дней)
    if (diffDays <= 7) {
        return "week";
    }
    
    // Все остальное
    return "older";
}

// Заголовки временных групп
const TIME_GROUPS = [
    { id: "last_hour", title: "⚡ Последний час", icon: "⚡" },
    { id: "today",     title: "📅 Сегодня",         icon: "📅" },
    { id: "two_days",  title: "⏳ Последние 2 дня",  icon: "⏳" },
    { id: "week",      title: "🗓️ За эту неделю",   icon: "🗓️" },
    { id: "older",     title: "📦 Ранее",            icon: "📦" }
];

// Парсинг даты вакансии в миллисекунды
function parseJobDate(processedAtStr) {
    if (!processedAtStr) return 0;
    if (processedAtStr.includes("T")) return new Date(processedAtStr).getTime();
    const parts = processedAtStr.split(/[- :]/);
    if (parts.length >= 6) {
        return new Date(
            parseInt(parts[0], 10),
            parseInt(parts[1], 10) - 1,
            parseInt(parts[2], 10),
            parseInt(parts[3], 10),
            parseInt(parts[4], 10),
            parseInt(parts[5], 10)
        ).getTime();
    }
    return new Date(processedAtStr).getTime();
}

// Рендеринг списка вакансий на основе currentJobs с группировкой по времени
function renderJobsList(append = false) {
    const container = document.getElementById("vacancies-container");
    
    if (currentJobs.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <p>Нет вакансий в категории "${getFilterLabel(currentFilter)}"</p>
            </div>
        `;
        return;
    }
    
    // Всегда сортируем вакансии по дате (самые свежие вверху)
    currentJobs.sort((a, b) => {
        return parseJobDate(b.processed_at) - parseJobDate(a.processed_at);
    });

    container.innerHTML = "";
    
    // Группируем вакансии по интервалам
    const grouped = {
        last_hour: [],
        today: [],
        two_days: [],
        week: [],
        older: []
    };
    
    currentJobs.forEach(job => {
        const grp = getJobTimeGroup(job.processed_at);
        if (grouped[grp]) {
            grouped[grp].push(job);
        } else {
            grouped.older.push(job);
        }
    });
    
    // Рендерим только те группы, где есть хотя бы одна вакансия
    TIME_GROUPS.forEach(g => {
        const jobsInGroup = grouped[g.id];
        if (!jobsInGroup || jobsInGroup.length === 0) return;
        
        // Создаем заголовок секции
        const header = document.createElement("div");
        header.className = "timeline-section-header";
        header.innerHTML = `
            <span class="timeline-title">${g.title}</span>
            <span class="timeline-badge">${jobsInGroup.length}</span>
            <div class="timeline-divider"></div>
        `;
        container.appendChild(header);
        
        // Рендерим карточки группы
        jobsInGroup.forEach(job => {
            const card = document.createElement("div");
            card.className = "vacancy-card";
            card.dataset.id = job.id;
            card.dataset.status = job.status;
            
            let scoreClass = "score-low";
            if (job.match_score >= 80) scoreClass = "score-high";
            else if (job.match_score >= 60) scoreClass = "score-mid";
            
            let statusLabel = getStatusLabel(job.status);
            const logoHtml = getCompanyLogoHtml(job.company);
            
            let quickBtnHtml = "";
            if (job.status !== "applied" && job.status !== "already_applied") {
                quickBtnHtml = `<button class="btn btn-secondary" style="font-size: 11px; padding: 4px 10px; border-radius: var(--radius-pill); border-color: rgba(99, 102, 241, 0.4); background: rgba(99, 102, 241, 0.15); color: #c7d2fe; white-space: nowrap;" onclick="event.stopPropagation(); window.handleQuickApply('${job.id}')" title="Сгенерировать письмо, ответить на вопросы и отправить">⚡ ИИ-отклик</button>`;
            }
            
            const resumeBadgeHtml = job.applied_resume_title 
                ? `<span class="resume-badge" style="font-size: 11px; background: rgba(56, 189, 248, 0.12); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.25); border-radius: var(--radius-pill); padding: 2px 7px; font-weight: 500; display: inline-flex; align-items: center; gap: 4px; white-space: nowrap;" title="Резюме, выбранное для этого отклика">📄 ${escapeHtml(job.applied_resume_title)}</span>` 
                : '';

            card.innerHTML = `
                <div style="display: flex; align-items: center; gap: 14px; flex: 1; min-width: 0;">
                    ${logoHtml}
                    <div class="v-info" style="flex: 1; min-width: 0;">
                        <h4 class="v-title" style="margin-bottom: 3px; font-weight: 600; line-height: 1.3; color: white;">${escapeHtml(job.title)}</h4>
                        <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                            <span class="v-company-salary" style="font-size: 13px; color: var(--text-secondary);">${escapeHtml(job.company)}</span>
                            ${resumeBadgeHtml}
                            <a href="https://hh.ru/vacancy/${job.id}" target="_blank" class="v-link" onclick="event.stopPropagation()">Открыть на hh.ru ↗</a>
                        </div>
                    </div>
                </div>
                <div class="v-actions" style="display: flex; align-items: center; gap: 8px; flex-shrink: 0;">
                    ${quickBtnHtml}
                    <span class="score-badge ${scoreClass}">${job.match_score}% Match</span>
                    <span class="status-badge status-${job.status}">${statusLabel}</span>
                </div>
            `;
            
            card.addEventListener("click", () => openModal(job));
            container.appendChild(card);
        });
    });
}

function getFilterLabel(filter) {
    switch(filter) {
        case "matched": return "Релевантные";
        case "needs_answers": return "Требуют ответа ❓";
        case "applied": return "Отправленные";
        case "ignored": return "Не подошли";
        case "failed": return "Ошибки";
        default: return "Все";
    }
}

function getStatusLabel(status) {
    switch(status) {
        case "new":            return "Ожидает отклика";
        case "needs_answers":  return "Вопросы ❓";
        case "applied":        return "Откликнут";
        case "already_applied":return "Уже откликнут";
        case "ignored":        return "Не подошёл";
        case "failed":         return "Ошибка";
        default:               return status || "Новый";
    }
}

// Открытие модального окна просмотра / редактирования вакансии
function openModal(job) {
    const modal = document.getElementById("vacancy-modal");
    
    document.getElementById("modal-vacancy-title").textContent = job.title;
    const metaText = job.applied_resume_title ? `${job.company} • 📄 Резюме: ${job.applied_resume_title}` : job.company;
    document.getElementById("modal-vacancy-meta").textContent = metaText;
    document.getElementById("modal-vacancy-link").setAttribute("href", `https://hh.ru/vacancy/${job.id}`);
    document.getElementById("modal-reasoning-text").textContent = job.reasoning || "Обоснование отсутствует.";
    
    const textarea = document.getElementById("modal-cover-letter-input");
    textarea.value = job.cover_letter || "";
    
    document.getElementById("modal-score-val").textContent = `${job.match_score}%`;
    const circle = document.getElementById("modal-progress-bar");
    const strokeOffset = 220 - (220 * job.match_score) / 100;
    circle.style.strokeDashoffset = strokeOffset;
    
    const applyBtn = document.getElementById("modal-apply-btn");
    const ignoreBtn = document.getElementById("modal-ignore-btn");
    const reanalyzeBtn = document.getElementById("modal-reanalyze-btn");
    
    // Обработка вопросов работодателя
    const qContainer = document.getElementById("modal-questions-container");
    const qList = document.getElementById("modal-questions-list");
    const qBadge = document.getElementById("modal-questions-count-badge");
    
    let questions = [];
    if (job.questions_data) {
        try {
            questions = typeof job.questions_data === "string" ? JSON.parse(job.questions_data) : job.questions_data;
        } catch (e) {
            console.error("Error parsing questions_data:", e);
        }
    }
    
    if (questions && Array.isArray(questions) && questions.length > 0) {
        if (qContainer) qContainer.classList.remove("hide");
        if (qBadge) qBadge.textContent = `${questions.length} вопр.`;
        if (qList) {
            qList.innerHTML = "";
            questions.forEach((q, idx) => {
                const item = document.createElement("div");
                item.className = "question-item-card";
                item.style.cssText = "background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 10px 12px;";
                
                const isUserReq = q.requires_user_input;
                const badgeHtml = isUserReq 
                    ? `<span class="badge" style="background: rgba(239,68,68,0.2); color: #f87171; font-size: 10px; padding: 2px 6px;">⚠️ Требуется ваш ответ</span>`
                    : `<span class="badge" style="background: rgba(16,185,129,0.2); color: #34d399; font-size: 10px; padding: 2px 6px;">✨ ИИ уверен (${q.confidence || 90}%)</span>`;
                
                item.innerHTML = `
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; margin-bottom: 6px;">
                        <label style="font-size: 12px; font-weight: 600; color: white; line-height: 1.3;">
                            ${idx + 1}. ${escapeHtml(q.question_text || q.text || "Вопрос")}
                        </label>
                        ${badgeHtml}
                    </div>
                    <textarea class="question-answer-input" data-qid="${escapeHtml(q.id || `q_${idx}`)}" data-qtext="${escapeHtml(q.question_text || q.text || '')}" rows="2" style="width: 100%; box-sizing: border-box; padding: 6px 10px; font-size: 12px; background: rgba(0,0,0,0.4); border: 1px solid ${isUserReq ? 'rgba(239,68,68,0.5)' : 'rgba(255,255,255,0.15)'}; border-radius: 6px; color: white; resize: vertical;">${escapeHtml(q.answer || '')}</textarea>
                `;
                qList.appendChild(item);
            });
        }
    } else {
        if (qContainer) qContainer.classList.add("hide");
        if (qList) qList.innerHTML = "";
    }
    
    // Сбрасываем состояние кнопок
    reanalyzeBtn.classList.add("hide");
    applyBtn.classList.remove("hide");
    ignoreBtn.classList.remove("hide");
    
    const defaultApplyText = userSettings.dry_run ? "Симуляция отклика (Dry Run)" : "Откликнуться";
    
    if (job.status === "applied" || job.status === "already_applied") {
        applyBtn.setAttribute("disabled", "true");
        applyBtn.textContent = job.status === "already_applied" ? "Откликнут ранее" : "Уже отправлено";
        textarea.setAttribute("readonly", "true");
        ignoreBtn.classList.add("hide");
    } else if (job.status === "failed") {
        // Для ошибочных вакансий показываем кнопку переоценки
        reanalyzeBtn.classList.remove("hide");
        applyBtn.removeAttribute("disabled");
        applyBtn.textContent = defaultApplyText;
        textarea.removeAttribute("readonly");
    } else {
        applyBtn.removeAttribute("disabled");
        applyBtn.textContent = defaultApplyText;
        textarea.removeAttribute("readonly");
        ignoreBtn.classList.remove("hide");
    }
    
    applyBtn.onclick = async () => {
        const coverLetter = textarea.value.trim();
        if (!coverLetter) {
            showToast("Пожалуйста, напишите сопроводительное письмо.", "error");
            return;
        }
        
        // Сбор ответов на вопросы
        const answersDict = {};
        const qInputs = document.querySelectorAll("#modal-questions-list textarea, #modal-questions-list input");
        qInputs.forEach(inp => {
            const qid = inp.getAttribute("data-qid") || inp.getAttribute("data-qtext");
            if (qid) {
                answersDict[qid] = inp.value.trim();
            }
        });

        applyBtn.setAttribute("disabled", "true");
        applyBtn.textContent = "Отправка...";
        
        const applyResumeId = job.applied_resume_id || userSettings.resume_id;

        try {
            const response = await fetch("/api/apply", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    vacancy_id: job.id,
                    resume_id: applyResumeId,
                    cover_letter: coverLetter,
                    answers: Object.keys(answersDict).length > 0 ? answersDict : null
                })
            });
            
            if (response.ok) {
                modal.classList.add("hide");
                showToast("Отклик успешно отправлен!", "success");
                await loadJobs();
            } else {
                const err = await response.json();
                showToast("Ошибка при отклике: " + (err.detail || "неизвестная ошибка."), "error");
                applyBtn.removeAttribute("disabled");
                applyBtn.textContent = "Откликнуться";
            }
        } catch (e) {
            console.error("Error applying:", e);
            showToast("Сетевая ошибка при отклике.", "error");
            applyBtn.removeAttribute("disabled");
            applyBtn.textContent = "Откликнуться";
        }
    };
    
    reanalyzeBtn.onclick = async () => {
        reanalyzeBtn.setAttribute("disabled", "true");
        reanalyzeBtn.textContent = "Анализ...";
        
        startRealtimePolling();
        
        try {
            const response = await fetch(`/api/reanalyze/${job.id}`, { method: "POST" });
            const data = await response.json();
            if (response.ok && data.status !== "error") {
                modal.classList.add("hide");
                await loadJobs(true);
            } else {
                showToast("Ошибка при переоценке: " + (data.message || "неизвестная ошибка."), "error");
                reanalyzeBtn.removeAttribute("disabled");
                reanalyzeBtn.textContent = "↺ Переоценить";
            }
        } catch (e) {
            console.error("Error reanalyzing:", e);
            showToast("Сетевая ошибка при переоценке.", "error");
            reanalyzeBtn.removeAttribute("disabled");
            reanalyzeBtn.textContent = "↺ Переоценить";
        }
    };
    
    ignoreBtn.onclick = () => {
        modal.classList.add("hide");
    };
    
    modal.classList.remove("hide");
}

function updateProcessingStatus(pipeline) {
    const panel = document.getElementById("processing-status-panel");
    const titleEl = document.getElementById("processing-job-title");
    const companyEl = document.getElementById("processing-job-company");
    
    if (pipeline && pipeline.currently_processing) {
        if (panel && titleEl && companyEl) {
            panel.classList.remove("hide");
            titleEl.textContent = pipeline.currently_processing.title || "Обработка...";
            const comp = pipeline.currently_processing.company;
            if (comp) {
                companyEl.textContent = `🏢 ${comp}`;
                companyEl.style.display = "block";
            } else {
                companyEl.textContent = "";
                companyEl.style.display = "none";
            }
        }
    } else {
        if (panel) panel.classList.add("hide");
    }
}

// Утилита для защиты от XSS
function escapeHtml(unsafe) {
    if (!unsafe) return "";
    return String(unsafe)
         .replace(/&/g, "&amp;")
         .replace(/</g, "&lt;")
         .replace(/>/g, "&gt;")
         .replace(/"/g, "&quot;")
         .replace(/'/g, "&#039;");
}

// Система Toast уведомлений
function showToast(message, type = "info") {
    const container = document.getElementById("toast-container");
    if (!container) return;
    
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.style.whiteSpace = "pre-line";
    toast.style.cursor = "pointer";
    toast.setAttribute("title", "Нажмите, чтобы скрыть");
    
    let icon = "ℹ️";
    if (type === "success") icon = "✅";
    if (type === "error") icon = "❌";
    
    const cleanMsg = escapeHtml(message).replace(/&lt;br\s*\/?&gt;/gi, "\n");
    toast.innerHTML = `<span style="flex-shrink:0;">${icon}</span> <span style="line-height: 1.4; flex: 1;">${cleanMsg}</span>`;
    
    const dismiss = () => {
        toast.classList.add("toast-fade-out");
        setTimeout(() => toast.remove(), 300);
    };

    toast.addEventListener("click", dismiss);
    container.appendChild(toast);
    
    setTimeout(dismiss, 5000);
}

// Кастомный confirm с безопасной очисткой обработчиков
function showConfirm(message) {
    return new Promise((resolve) => {
        const modal = document.getElementById("confirm-modal");
        const msgEl = document.getElementById("confirm-message");
        const okBtn = document.getElementById("confirm-ok-btn");
        const cancelBtn = document.getElementById("confirm-cancel-btn");
        
        if (!modal || !msgEl || !okBtn || !cancelBtn) {
            resolve(confirm(message));
            return;
        }

        msgEl.textContent = message;
        modal.classList.remove("hide");
        
        let resolved = false;
        const cleanup = (result) => {
            if (resolved) return;
            resolved = true;
            modal.classList.add("hide");
            okBtn.removeEventListener("click", onOk);
            cancelBtn.removeEventListener("click", onCancel);
            document.removeEventListener("keydown", onKeyDown);
            modal.removeEventListener("click", onBackdrop);
            resolve(result);
        };
        
        const onOk = () => cleanup(true);
        const onCancel = () => cleanup(false);
        const onKeyDown = (e) => {
            if (e.key === "Escape") cleanup(false);
            else if (e.key === "Enter") cleanup(true);
        };
        const onBackdrop = (e) => {
            if (e.target === modal) cleanup(false);
        };
        
        okBtn.addEventListener("click", onOk, { once: true });
        cancelBtn.addEventListener("click", onCancel, { once: true });
        document.addEventListener("keydown", onKeyDown);
        modal.addEventListener("click", onBackdrop);
    });
}
// Переключение табов в Менеджере Ключей
function switchKeyManagerTab(tab) {
    activeKeyManagerTab = tab;
    const tabGeminiBtn = document.getElementById("tab-gemini-btn");
    const tabMistralBtn = document.getElementById("tab-mistral-btn");
    const addKeyLabel = document.getElementById("add-key-label");
    const newKeyInput = document.getElementById("new-key-input");
    const keysPoolTitle = document.getElementById("keys-pool-title");

    if (tab === "gemini") {
        if (tabGeminiBtn) {
            tabGeminiBtn.style.background = "rgba(59, 130, 246, 0.2)";
            tabGeminiBtn.style.color = "#60a5fa";
            tabGeminiBtn.style.borderColor = "rgba(59, 130, 246, 0.4)";
        }
        if (tabMistralBtn) {
            tabMistralBtn.style.background = "rgba(255, 255, 255, 0.05)";
            tabMistralBtn.style.color = "var(--text-secondary)";
            tabMistralBtn.style.borderColor = "rgba(255, 255, 255, 0.1)";
        }
        if (addKeyLabel) addKeyLabel.textContent = "Добавить ключ Gemini API";
        if (newKeyInput) newKeyInput.placeholder = "Вставьте ключ Gemini (например: AIzaSy...)";
        if (keysPoolTitle) keysPoolTitle.textContent = "Пул ключей Gemini";
    } else {
        if (tabMistralBtn) {
            tabMistralBtn.style.background = "rgba(249, 115, 22, 0.2)";
            tabMistralBtn.style.color = "#fdba74";
            tabMistralBtn.style.borderColor = "rgba(249, 115, 22, 0.4)";
        }
        if (tabGeminiBtn) {
            tabGeminiBtn.style.background = "rgba(255, 255, 255, 0.05)";
            tabGeminiBtn.style.color = "var(--text-secondary)";
            tabGeminiBtn.style.borderColor = "rgba(255, 255, 255, 0.1)";
        }
        if (addKeyLabel) addKeyLabel.textContent = "Добавить ключ Mistral API";
        if (newKeyInput) newKeyInput.placeholder = "Вставьте ключ Mistral (например: mistral_...)";
        if (keysPoolTitle) keysPoolTitle.textContent = "Пул ключей Mistral AI (Резерв)";
    }

    renderKeysList();
}

// Обновление пула ключей из загруженных настроек
function updateKeysPoolFromSettings(geminiKeysStr, mistralKeysStr) {
    if (!geminiKeysStr) {
        currentGeminiKeys = [];
    } else {
        currentGeminiKeys = geminiKeysStr.split(/[\n,;]+/)
            .map(k => k.trim())
            .filter(k => k && !k.toLowerCase().includes("your_gemini_api_key"));
    }

    if (!mistralKeysStr) {
        currentMistralKeys = [];
    } else {
        currentMistralKeys = mistralKeysStr.split(/[\n,;]+/)
            .map(k => k.trim())
            .filter(k => k && !k.toLowerCase().includes("your_mistral_api_key"));
    }

    currentApiKeys = currentGeminiKeys;
    updateKeyManagerBadge();
}

function updateKeyManagerBadge() {
    const badge = document.getElementById("key-manager-badge");
    const geminiBadge = document.getElementById("gemini-keys-badge");
    const mistralBadge = document.getElementById("mistral-keys-badge");

    const totalKeys = currentGeminiKeys.length + currentMistralKeys.length;

    if (geminiBadge) geminiBadge.textContent = currentGeminiKeys.length;
    if (mistralBadge) mistralBadge.textContent = currentMistralKeys.length;

    if (badge) {
        badge.textContent = `${currentGeminiKeys.length} G | ${currentMistralKeys.length} M`;
        if (totalKeys > 0) {
            badge.style.background = "rgba(59, 130, 246, 0.25)";
            badge.style.color = "#60a5fa";
        } else {
            badge.style.background = "rgba(239, 68, 68, 0.2)";
            badge.style.color = "#f87171";
        }
    }
}

// Открытие Менеджера Ключей
async function openKeyManager() {
    const modal = document.getElementById("key-manager-modal");
    if (!modal) return;
    
    switchKeyManagerTab(activeKeyManagerTab || "gemini");
    modal.classList.remove("hide");
    
    // Запрашиваем актуальный статус ключей
    try {
        const response = await fetch("/api/model-status");
        if (response.ok) {
            const data = await response.json();
            if (data.keys && Array.isArray(data.keys)) {
                keyStatusesMap = {};
                data.keys.forEach(k => {
                    keyStatusesMap[k.key] = k;
                });
                renderKeysList();
            }
        }
    } catch (e) {
        console.error("Error fetching model keys status:", e);
    }
}

// Отрисовка списка ключей в модалке для активного таба
function renderKeysList() {
    const container = document.getElementById("keys-list-container");
    const countLabel = document.getElementById("keys-count-label");
    if (!container) return;
    
    const activeList = activeKeyManagerTab === "mistral" ? currentMistralKeys : currentGeminiKeys;
    const providerName = activeKeyManagerTab === "mistral" ? "Mistral AI" : "Gemini API";

    if (countLabel) {
        countLabel.textContent = `${activeList.length} ${activeList.length === 1 ? 'ключ' : 'ключей'}`;
    }
    
    if (activeList.length === 0) {
        container.innerHTML = `
            <div style="text-align: center; padding: 24px; color: var(--text-secondary); font-size: 13px; border: 1px dashed rgba(255,255,255,0.1); border-radius: 8px;">
                Ключи ${providerName} еще не добавлены. Введите ключ выше и нажмите «+ Добавить».
            </div>
        `;
        return;
    }
    
    container.innerHTML = "";
    activeList.forEach((key, index) => {
        const card = document.createElement("div");
        card.className = "key-item-card";
        
        // Маскируем ключ (показываем первые 6 и последние 4 символа)
        const maskedKey = key.length > 14 
            ? `${key.substring(0, 6)}••••••••${key.substring(key.length - 4)}` 
            : key;
            
        const keyInfo = keyStatusesMap[key] || { status: "ok" };
        let statusBadge = `<span style="font-size: 11px; padding: 2px 6px; border-radius: 4px; background: rgba(52, 211, 153, 0.15); color: #34d399;">Активен</span>`;
        if (keyInfo.status === "error") {
            if (keyInfo.reason === "rate_limit_or_quota") {
                statusBadge = `<span style="font-size: 11px; padding: 2px 6px; border-radius: 4px; background: rgba(239, 68, 68, 0.15); color: #f87171;" title="${escapeHtml(keyInfo.detail || '')}">Лимит исчерпан</span>`;
            } else {
                statusBadge = `<span style="font-size: 11px; padding: 2px 6px; border-radius: 4px; background: rgba(239, 68, 68, 0.15); color: #f87171;" title="${escapeHtml(keyInfo.detail || '')}">Ошибка</span>`;
            }
        }
        
        card.innerHTML = `
            <div class="key-item-info">
                <span style="font-size: 12px; color: var(--text-secondary); font-weight: 600; width: 20px;">#${index + 1}</span>
                <span class="key-item-text" id="key-text-${index}">${escapeHtml(maskedKey)}</span>
                ${statusBadge}
            </div>
            <div class="key-item-actions">
                <button type="button" class="key-btn-icon" data-action="toggle-visibility" data-index="${index}" title="Показать/скрыть ключ">👁️</button>
                <button type="button" class="key-btn-icon" data-action="edit" data-index="${index}" title="Редактировать ключ">✏️</button>
                <button type="button" class="key-btn-icon key-btn-delete" data-action="delete" data-index="${index}" title="Удалить ключ">🗑️</button>
            </div>
        `;
        
        // Обработчики кнопок карточки
        const toggleBtn = card.querySelector('[data-action="toggle-visibility"]');
        const editBtn = card.querySelector('[data-action="edit"]');
        const deleteBtn = card.querySelector('[data-action="delete"]');
        const textSpan = card.querySelector(`#key-text-${index}`);
        
        let isRevealed = false;
        toggleBtn.addEventListener("click", () => {
            isRevealed = !isRevealed;
            if (isRevealed) {
                textSpan.textContent = key;
                toggleBtn.textContent = "🔒";
            } else {
                textSpan.textContent = maskedKey;
                toggleBtn.textContent = "👁️";
            }
        });
        
        editBtn.addEventListener("click", () => {
            const newKey = prompt(`Изменить API-ключ #${index + 1}:`, key);
            if (newKey !== null) {
                const trimmed = newKey.trim();
                if (trimmed) {
                    activeList[index] = trimmed;
                    renderKeysList();
                    updateKeyManagerBadge();
                }
            }
        });
        
        deleteBtn.addEventListener("click", async () => {
            const confirmed = await showConfirm(`Удалить ключ #${index + 1} (${maskedKey}) из пула ${providerName}?`);
            if (confirmed) {
                activeList.splice(index, 1);
                renderKeysList();
                updateKeyManagerBadge();
            }
        });
        
        container.appendChild(card);
    });
}

// Добавление ключа из поля ввода
function addNewKeyFromInput() {
    const input = document.getElementById("new-key-input");
    if (!input) return;
    
    const raw = input.value.trim();
    if (!raw) {
        showToast("Пожалуйста, введите или вставьте API ключ.", "error");
        return;
    }
    
    const activeList = activeKeyManagerTab === "mistral" ? currentMistralKeys : currentGeminiKeys;
    const placeholderFilter = activeKeyManagerTab === "mistral" ? "your_mistral_api_key" : "your_gemini_api_key";
    const parts = raw.split(/[\n,;]+/).map(p => p.trim()).filter(Boolean);
    let addedCount = 0;
    
    parts.forEach(p => {
        if (!activeList.includes(p) && !p.toLowerCase().includes(placeholderFilter)) {
            activeList.push(p);
            addedCount++;
        }
    });
    
    input.value = "";
    renderKeysList();
    updateKeyManagerBadge();
    
    if (addedCount > 0) {
        showToast(`Добавлено ключей в пул ${activeKeyManagerTab === "mistral" ? "Mistral" : "Gemini"}: ${addedCount}`, "success");
    } else {
        showToast("Такой ключ уже присутствует в списке.", "info");
    }
}

// Сохранение изменений менеджера ключей на сервер
async function saveKeyManagerChanges() {
    const saveBtn = document.getElementById("key-manager-save-btn");
    const modal = document.getElementById("key-manager-modal");
    
    if (saveBtn) {
        saveBtn.setAttribute("disabled", "true");
        saveBtn.textContent = "Сохранение...";
    }
    
    try {
        const payload = {
            queries: document.getElementById("queries-input").value.split(",").map(q => q.trim()).filter(Boolean),
            area_id: document.getElementById("area-select").value,
            threshold: parseInt(document.getElementById("threshold-range").value, 10),
            resume_id: document.getElementById("resume-select").value,
            dry_run: document.getElementById("dryrun-toggle").checked,
            gemini_api_keys: currentGeminiKeys.join(","),
            gemini_model: userSettings.gemini_model || "gemini-3.6-flash",
            mistral_api_keys: currentMistralKeys.join(","),
            mistral_model: userSettings.mistral_model || "mistral-small-latest"
        };
        
        const response = await fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        
        if (response.ok) {
            userSettings.gemini_api_keys = payload.gemini_api_keys;
            userSettings.mistral_api_keys = payload.mistral_api_keys;
            updateKeyManagerBadge();
            if (modal) modal.classList.add("hide");
            showToast("Список API ключей успешно сохранен!", "success");
            // Перепроверяем статус
            await checkStatus();
        } else {
            showToast("Ошибка при сохранении ключей.", "error");
        }
    } catch (e) {
        console.error("Error saving keys:", e);
        showToast("Сетевая ошибка при сохранении ключей.", "error");
    } finally {
        if (saveBtn) {
            saveBtn.removeAttribute("disabled");
            saveBtn.textContent = "Сохранить изменения";
        }
    }
}

// Принудительная проверка доступности всех ключей
async function probeAllKeysStatus() {
    const probeBtn = document.getElementById("probe-all-keys-btn");
    if (probeBtn) {
        probeBtn.setAttribute("disabled", "true");
        probeBtn.textContent = "Проверяем...";
    }
    
    try {
        showToast("Выполняется проверка всех ключей в пуле...", "info");
        const response = await fetch("/api/model-status?probe=true");
        if (response.ok) {
            const data = await response.json();
            if (data.keys && Array.isArray(data.keys)) {
                keyStatusesMap = {};
                data.keys.forEach(k => {
                    keyStatusesMap[k.key] = k;
                });
                renderKeysList();
            }
            let toastMsg = `Проверка завершена: доступно ${data.available || 0} из ${data.total || 0}`;
            if (data.gemini && data.mistral && (data.gemini.total > 0 || data.mistral.total > 0)) {
                toastMsg = `Проверка: Gemini (${data.gemini.available}/${data.gemini.total}), Mistral (${data.mistral.available}/${data.mistral.total})`;
            }
            showToast(toastMsg, "success");
            await checkStatus();
        } else {
            showToast("Не удалось выполнить проверку ключей.", "error");
        }
    } catch (e) {
        console.error("Error probing keys:", e);
        showToast("Ошибка соединения при проверке ключей.", "error");
    } finally {
        if (probeBtn) {
            probeBtn.removeAttribute("disabled");
            probeBtn.textContent = "🔄 Проверить доступность всех";
        }
    }
}

// Вспомогательная функция вставки текста в позицию курсора
function insertAtCursor(textarea, text) {
    if (!textarea) return;
    const start = textarea.selectionStart || 0;
    const end = textarea.selectionEnd || 0;
    const val = textarea.value;
    textarea.value = val.substring(0, start) + text + val.substring(end);
    textarea.selectionStart = textarea.selectionEnd = start + text.length;
    textarea.focus();
}

// Открытие модального окна Системных настроек
async function openSystemSettings() {
    const modal = document.getElementById("system-settings-modal");
    if (!modal) return;

    modal.classList.remove("hide");

    try {
        const response = await fetch("/api/system-settings");
        if (response.ok) {
            const data = await response.json();
            
            const promptEditor = document.getElementById("sys-prompt-editor");
            const primaryProviderSelect = document.getElementById("sys-primary-provider");
            const fallbackToggle = document.getElementById("sys-fallback-toggle");
            const tempSlider = document.getElementById("sys-temperature");
            const tempValue = document.getElementById("sys-temp-value");
            const sysGeminiSelect = document.getElementById("sys-gemini-model-select");
            const sysMistralSelect = document.getElementById("sys-mistral-model-select");

            if (promptEditor) {
                promptEditor.value = data.system_prompt || data.default_system_prompt || "";
            }
            if (primaryProviderSelect) {
                primaryProviderSelect.value = data.primary_provider || "gemini";
            }
            if (fallbackToggle) {
                fallbackToggle.checked = data.fallback_enabled !== false;
            }
            if (tempSlider) {
                tempSlider.value = data.temperature !== undefined ? data.temperature : 0.2;
                if (tempValue) tempValue.textContent = parseFloat(tempSlider.value).toFixed(2);
            }
            if (sysGeminiSelect && data.gemini_model) {
                sysGeminiSelect.value = data.gemini_model;
            }
            if (sysMistralSelect && data.mistral_model) {
                sysMistralSelect.value = data.mistral_model;
            }
            
            await loadModelsDropdown(data.gemini_model || userSettings.gemini_model, data.mistral_model || userSettings.mistral_model);
        }
    } catch (e) {
        console.error("Error loading system settings:", e);
        showToast("Не удалось загрузить системные настройки.", "error");
    }
}

// Сохранение системных настроек
async function saveSystemSettings() {
    const saveBtn = document.getElementById("system-settings-save-btn");
    const modal = document.getElementById("system-settings-modal");
    
    if (saveBtn) {
        saveBtn.setAttribute("disabled", "true");
        saveBtn.textContent = "Сохранение...";
    }

    try {
        const promptEditor = document.getElementById("sys-prompt-editor");
        const primaryProviderSelect = document.getElementById("sys-primary-provider");
        const fallbackToggle = document.getElementById("sys-fallback-toggle");
        const tempSlider = document.getElementById("sys-temperature");
        const sysGeminiSelect = document.getElementById("sys-gemini-model-select");
        const sysMistralSelect = document.getElementById("sys-mistral-model-select");

        const selectedGeminiModel = sysGeminiSelect ? sysGeminiSelect.value : (userSettings.gemini_model || "gemini-3.6-flash");
        const selectedMistralModel = sysMistralSelect ? sysMistralSelect.value : (userSettings.mistral_model || "mistral-small-latest");

        const payload = {
            system_prompt: promptEditor ? promptEditor.value : "",
            primary_provider: primaryProviderSelect ? primaryProviderSelect.value : "gemini",
            fallback_enabled: fallbackToggle ? fallbackToggle.checked : true,
            temperature: tempSlider ? parseFloat(tempSlider.value) : 0.2,
            gemini_model: selectedGeminiModel,
            mistral_model: selectedMistralModel
        };

        const response = await fetch("/api/system-settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        if (response.ok) {
            userSettings.gemini_model = selectedGeminiModel;
            userSettings.mistral_model = selectedMistralModel;
            showToast("Системные настройки успешно сохранены!", "success");
            if (modal) modal.classList.add("hide");
            await checkStatus();
        } else {
            showToast("Ошибка при сохранении системных настроек.", "error");
        }
    } catch (e) {
        console.error("Error saving system settings:", e);
        showToast("Сетевая ошибка при сохранении системных настроек.", "error");
    } finally {
        if (saveBtn) {
            saveBtn.removeAttribute("disabled");
            saveBtn.textContent = "Сохранить настройки";
        }
    }
}

// Сброс промпта к дефолтному
async function resetSystemPrompt() {
    const confirmed = await showConfirm("Сбросить системный промпт к заводскому шаблону по умолчанию?");
    if (!confirmed) return;

    try {
        const response = await fetch("/api/system-settings/reset-prompt", {
            method: "POST"
        });
        if (response.ok) {
            const data = await response.json();
            const promptEditor = document.getElementById("sys-prompt-editor");
            if (promptEditor && data.system_prompt) {
                promptEditor.value = data.system_prompt;
            }
            showToast("Системный промпт сброшен к заводскому.", "info");
        }
    } catch (e) {
        console.error("Error resetting system prompt:", e);
        showToast("Ошибка при сбросе промпта.", "error");
    }
}

// ----------------------------------------------------
// База ответов профиля кандидата (FAQ)
// ----------------------------------------------------

async function openProfileAnswersModal() {
    const modal = document.getElementById("profile-answers-modal");
    if (!modal) return;
    modal.classList.remove("hide");
    await loadProfileAnswers();
}

async function loadProfileAnswers() {
    const container = document.getElementById("profile-answers-list");
    if (!container) return;

    try {
        container.innerHTML = '<div style="padding: 12px; color: var(--text-secondary); text-align: center;">Загрузка ответов...</div>';
        const response = await fetch("/api/user-profile-answers");
        if (response.ok) {
            const data = await response.json();
            renderProfileAnswers(data.answers || []);
        } else {
            container.innerHTML = '<div style="padding: 12px; color: var(--accent-red); text-align: center;">Не удалось загрузить ответы.</div>';
        }
    } catch (e) {
        console.error("Error loading profile answers:", e);
        container.innerHTML = '<div style="padding: 12px; color: var(--accent-red); text-align: center;">Сетевая ошибка при загрузке ответов.</div>';
    }
}

function renderProfileAnswers(answers) {
    const container = document.getElementById("profile-answers-list");
    if (!container) return;

    if (!answers || answers.length === 0) {
        container.innerHTML = '<div style="padding: 12px; color: var(--text-secondary); text-align: center;">Нет сохраненных ответов. Добавьте первый ответ выше.</div>';
        return;
    }

    container.innerHTML = "";
    answers.forEach(item => {
        const card = document.createElement("div");
        card.style.cssText = "background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 12px 14px; display: flex; flex-direction: column; gap: 8px;";

        card.innerHTML = `
            <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 8px;">
                <div>
                    <span style="font-size: 13px; font-weight: 600; color: white;">${escapeHtml(item.question_hint || item.key)}</span>
                    <span style="font-size: 11px; color: var(--text-secondary); margin-left: 6px;">[${escapeHtml(item.key)}]</span>
                </div>
                <div style="display: flex; gap: 6px;">
                    <button class="btn btn-secondary btn-edit-answer" style="font-size: 11px; padding: 3px 8px;">✏️ Изменить</button>
                    <button class="btn btn-secondary btn-delete-answer" style="font-size: 11px; padding: 3px 8px; color: #f87171; border-color: rgba(239,68,68,0.3);">🗑️</button>
                </div>
            </div>
            <div class="answer-view-text" style="font-size: 12px; color: #e5e7eb; line-height: 1.4; background: rgba(0,0,0,0.25); padding: 6px 10px; border-radius: 6px;">
                ${escapeHtml(item.answer)}
            </div>
            <div class="answer-edit-container hide" style="display: flex; flex-direction: column; gap: 6px;">
                <textarea class="edit-answer-input" rows="2" style="width: 100%; box-sizing: border-box; padding: 6px 10px; font-size: 12px; background: rgba(0,0,0,0.4); border: 1px solid rgba(255,255,255,0.2); border-radius: 6px; color: white;">${escapeHtml(item.answer)}</textarea>
                <div style="display: flex; justify-content: flex-end; gap: 6px;">
                    <button class="btn btn-secondary btn-cancel-edit" style="font-size: 11px; padding: 3px 10px;">Отмена</button>
                    <button class="btn btn-primary btn-save-edit" style="font-size: 11px; padding: 3px 12px;">Сохранить</button>
                </div>
            </div>
        `;

        const editBtn = card.querySelector(".btn-edit-answer");
        const deleteBtn = card.querySelector(".btn-delete-answer");
        const viewEl = card.querySelector(".answer-view-text");
        const editContainer = card.querySelector(".answer-edit-container");
        const cancelEditBtn = card.querySelector(".btn-cancel-edit");
        const saveEditBtn = card.querySelector(".btn-save-edit");
        const editInput = card.querySelector(".edit-answer-input");

        editBtn.addEventListener("click", () => {
            viewEl.classList.add("hide");
            editContainer.classList.remove("hide");
            editBtn.classList.add("hide");
        });

        cancelEditBtn.addEventListener("click", () => {
            viewEl.classList.remove("hide");
            editContainer.classList.add("hide");
            editBtn.classList.remove("hide");
            editInput.value = item.answer;
        });

        saveEditBtn.addEventListener("click", async () => {
            const newText = editInput.value.trim();
            if (!newText) {
                showToast("Ответ не может быть пустым", "error");
                return;
            }
            saveEditBtn.setAttribute("disabled", "true");
            saveEditBtn.textContent = "Сохранение...";
            try {
                const res = await fetch("/api/user-profile-answers", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        key: item.key,
                        question_hint: item.question_hint || item.key,
                        answer: newText
                    })
                });
                if (res.ok) {
                    showToast("Ответ обновлен", "success");
                    await loadProfileAnswers();
                } else {
                    showToast("Ошибка сохранения", "error");
                    saveEditBtn.removeAttribute("disabled");
                    saveEditBtn.textContent = "Сохранить";
                }
            } catch (e) {
                console.error("Error updating profile answer:", e);
                showToast("Сетевая ошибка", "error");
                saveEditBtn.removeAttribute("disabled");
                saveEditBtn.textContent = "Сохранить";
            }
        });

        deleteBtn.addEventListener("click", async () => {
            const confirmed = await showConfirm(`Удалить ответ на "${item.question_hint || item.key}"?`);
            if (!confirmed) return;

            try {
                const res = await fetch(`/api/user-profile-answers/${encodeURIComponent(item.key)}`, {
                    method: "DELETE"
                });
                if (res.ok) {
                    showToast("Ответ удален", "info");
                    await loadProfileAnswers();
                } else {
                    showToast("Не удалось удалить ответ", "error");
                }
            } catch (e) {
                console.error("Error deleting answer:", e);
                showToast("Сетевая ошибка при удалении", "error");
            }
        });

        container.appendChild(card);
    });
}

async function saveNewProfileAnswer() {
    const keyInput = document.getElementById("new-answer-key");
    const hintInput = document.getElementById("new-answer-hint");
    const textInput = document.getElementById("new-answer-text");
    const saveBtn = document.getElementById("save-new-answer-btn");

    if (!keyInput || !textInput) return;

    const key = keyInput.value.trim();
    const hint = hintInput ? hintInput.value.trim() : key;
    const answer = textInput.value.trim();

    if (!key || !answer) {
        showToast("Заполните ключ темы и текст ответа.", "error");
        return;
    }

    if (saveBtn) {
        saveBtn.setAttribute("disabled", "true");
        saveBtn.textContent = "Сохранение...";
    }

    try {
        const response = await fetch("/api/user-profile-answers", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                key: key,
                question_hint: hint || key,
                answer: answer
            })
        });

        if (response.ok) {
            showToast("Новый ответ сохранен в базу!", "success");
            keyInput.value = "";
            if (hintInput) hintInput.value = "";
            textInput.value = "";
            await loadProfileAnswers();
        } else {
            showToast("Ошибка при сохранении ответа.", "error");
        }
    } catch (e) {
        console.error("Error saving new answer:", e);
        showToast("Сетевая ошибка при сохранении.", "error");
    } finally {
        if (saveBtn) {
            saveBtn.removeAttribute("disabled");
            saveBtn.textContent = "Сохранить ответ";
        }
    }
}

// ----------------------------------------------------
// Быстрый ИИ-отклик по ссылке или ID
// ----------------------------------------------------

async function handleQuickApply(urlOrId) {
    if (!urlOrId) {
        showToast("Пожалуйста, вставьте ссылку на вакансию или её ID.", "error");
        return;
    }

    const btn = document.getElementById("quick-apply-btn");
    const btnText = document.getElementById("quick-apply-btn-text");
    const loader = document.getElementById("quick-apply-loader");
    const input = document.getElementById("quick-apply-url-input");

    if (btn) {
        btn.setAttribute("disabled", "true");
        if (btnText) btnText.textContent = "Анализ и отклик...";
        if (loader) loader.classList.remove("hide");
    }

    showToast("⚡ Запущен анализ вакансии, подготовка письма и ответов на вопросы...", "info");

    try {
        const response = await fetch("/api/quick-apply", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                url_or_id: urlOrId,
                resume_id: userSettings.resume_id
            })
        });

        const data = await response.json();

        if (!response.ok || data.status === "error") {
            showToast("Ошибка быстрого отклика: " + (data.message || data.detail || "не удалось обработать вакансию"), "error");
            return;
        }

        if (input) input.value = "";
        await loadJobs(true);

        if (data.status === "applied") {
            showToast(`✅ Отклик успешно отправлен на hh.ru!\n${data.title} (${data.company})`, "success");
        } else if (data.status === "already_applied") {
            showToast(`ℹ️ Вы уже откликались на эту вакансию ранее:\n${data.title} (${data.company})`, "info");
        } else if (data.status === "needs_answers") {
            showToast(`⚠️ ИИ подготовил сопроводительное и ответы, но требуется ваша проверка перед отправкой.`, "info");
            const jobObj = {
                id: data.vacancy_id,
                title: data.title,
                company: data.company,
                status: "needs_answers",
                match_score: data.match_score,
                reasoning: data.reasoning,
                cover_letter: data.cover_letter,
                questions_data: data.questions_data
            };
            openModal(jobObj);
        } else if (data.status === "dry_run") {
            showToast(`🧪 [Dry Run] Вакансия сохранена: ${data.title}. Отклик не отправлялся.`, "info");
            const jobObj = {
                id: data.vacancy_id,
                title: data.title,
                company: data.company,
                status: "new",
                match_score: data.match_score,
                reasoning: data.reasoning,
                cover_letter: data.cover_letter,
                questions_data: data.questions_data
            };
            openModal(jobObj);
        }
    } catch (e) {
        console.error("Error in quick apply:", e);
        showToast("Сетевая ошибка при быстром отклике.", "error");
    } finally {
        if (btn) {
            btn.removeAttribute("disabled");
            if (btnText) btnText.textContent = "Откликнуться с ИИ";
            if (loader) loader.classList.add("hide");
        }
    }
}
window.handleQuickApply = handleQuickApply;

/**
 * Liquid Glass Dynamic Specular Highlights & Pointer Morphology
 */
function initLiquidGlassInteractivity() {
    let ticking = false;
    document.addEventListener("mousemove", (e) => {
        if (!ticking) {
            window.requestAnimationFrame(() => {
                const target = (e.target && typeof e.target.closest === "function") 
                    ? e.target.closest(".stat-card, .vacancy-card, .btn-primary, .modal-content, .auth-box") 
                    : null;
                if (target) {
                    const rect = target.getBoundingClientRect();
                    const x = e.clientX - rect.left;
                    const y = e.clientY - rect.top;
                    target.style.setProperty("--mouse-x", `${x}px`);
                    target.style.setProperty("--mouse-y", `${y}px`);
                }
                ticking = false;
            });
            ticking = true;
        }
    }, { passive: true });
}
