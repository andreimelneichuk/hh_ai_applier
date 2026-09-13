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

// Пул API ключей (OpenAI/Groq, Gemini и Mistral)
let currentGeminiKeys = [];
let currentMistralKeys = [];
let currentOpenaiKeys = [];
let currentApiKeys = currentGeminiKeys; // для обратной совместимости
let activeKeyManagerTab = "openai"; // "openai" | "gemini" | "mistral"
let keyStatusesMap = {}; // key -> status object
let openaiPresetsMap = {}; // preset_id -> preset object

// Единые провайдеры и модели
let unifiedProvidersMap = {}; // provider_id -> provider object
let activeUnifiedProvider = "groq"; // "groq" | "openrouter" | "gemini" | "mistral" | "github" | "cerebras" | "custom"
let currentProviderModels = []; // models for active provider
let cachedCatalogModels = []; // all models for catalog
let activeSelectedModelId = ""; // active model id
let currentFreeOnlyFilter = false;
let currentCatalogProviderFilter = "all";
let currentDetailProviderId = null;
let currentDetailKeys = [];
let currentDetailKeysData = []; // [{ key, masked, status, reason, detail, latency_ms }]
let currentProviderStatusFilter = "all"; // "all" | "active" | "rate_limited" | "unconfigured"

const DEAD_OPENROUTER_MODELS = new Set([
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "google/gemini-2.0-pro-exp-02-05:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "deepseek/deepseek-r1:free",
    "mistralai/mistral-7b-instruct:free",
    "z-ai/glm-5.2:free",
    "meta-llama/llama-3.2-1b-instruct:free",
    "meta-llama/llama-3.2-3b-instruct:free"
]);

// Универсальный парсер API ключей (очищает от кавычек, скобок, поддерживает JSON-массивы и списки через запятую)
function parseKeysList(raw) {
    if (!raw) return [];
    if (Array.isArray(raw)) {
        const res = [];
        raw.forEach(item => {
            if (Array.isArray(item)) {
                res.push(...parseKeysList(item));
            } else if (typeof item === 'string') {
                const s = item.trim();
                if (s.startsWith("[") && s.endsWith("]")) {
                    try {
                        const parsed = JSON.parse(s);
                        if (Array.isArray(parsed)) {
                            res.push(...parseKeysList(parsed));
                            return;
                        }
                    } catch (e) {}
                }
                const cleaned = s.replace(/^["'\[]+|["'\]]+$/g, '').trim();
                if (cleaned && !cleaned.toLowerCase().includes("your_")) {
                    res.push(cleaned);
                }
            }
        });
        return Array.from(new Set(res));
    }
    const str = String(raw).trim();
    if (str.startsWith("[") && str.endsWith("]")) {
        try {
            const parsed = JSON.parse(str);
            if (Array.isArray(parsed)) {
                return parseKeysList(parsed);
            }
        } catch (e) {}
    }
    const parts = str.split(/[\n,;]+/);
    const res = [];
    parts.forEach(p => {
        const cleaned = p.trim().replace(/^["'\[]+|["'\]]+$/g, '').trim();
        if (cleaned && !cleaned.toLowerCase().includes("your_")) {
            res.push(cleaned);
        }
    });
    return Array.from(new Set(res));
}

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
            await loadJobs(false, false, true);
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

    // Слушатели для модального окна лимитов и автоостановки
    initLimitsModalListeners();

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
    const tabOpenaiBtn = document.getElementById("tab-openai-btn");
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

    if (tabOpenaiBtn) {
        tabOpenaiBtn.addEventListener("click", () => {
            switchKeyManagerTab("openai");
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

    // Слушатель выбора пресета OpenAI-провайдера
    const presetSelect = document.getElementById("sys-openai-preset-select");
    if (presetSelect) {
        presetSelect.addEventListener("change", (e) => {
            handleOpenaiPresetChange(e.target.value);
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

    const dashboardAiStatusPill = document.getElementById("dashboard-ai-status-pill");
    if (dashboardAiStatusPill) {
        dashboardAiStatusPill.addEventListener("click", () => {
            openSystemSettings();
        });
    }

    // Переключение вкладок в модальном окне настроек LLM
    const tabModelsBtn = document.getElementById("llm-tab-btn-models");
    const tabPromptBtn = document.getElementById("llm-tab-btn-prompt");
    const tabModelsContent = document.getElementById("llm-tab-content-models");
    const tabPromptContent = document.getElementById("llm-tab-content-prompt");

    function switchLlmTab(tab) {
        if (tab === "prompt") {
            if (tabPromptBtn) {
                tabPromptBtn.classList.add("btn-primary");
                tabPromptBtn.classList.remove("btn-secondary");
            }
            if (tabModelsBtn) {
                tabModelsBtn.classList.remove("btn-primary");
                tabModelsBtn.classList.add("btn-secondary");
            }
            if (tabPromptContent) tabPromptContent.style.display = "block";
            if (tabModelsContent) tabModelsContent.style.display = "none";
            if (promptEditor) setTimeout(() => promptEditor.focus(), 50);
        } else {
            if (tabModelsBtn) {
                tabModelsBtn.classList.add("btn-primary");
                tabModelsBtn.classList.remove("btn-secondary");
            }
            if (tabPromptBtn) {
                tabPromptBtn.classList.remove("btn-primary");
                tabPromptBtn.classList.add("btn-secondary");
            }
            if (tabModelsContent) tabModelsContent.style.display = "block";
            if (tabPromptContent) tabPromptContent.style.display = "none";
        }
    }

    if (tabModelsBtn) {
        tabModelsBtn.addEventListener("click", () => switchLlmTab("models"));
    }
    if (tabPromptBtn) {
        tabPromptBtn.addEventListener("click", () => switchLlmTab("prompt"));
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

    const quickSavePromptBtn = document.getElementById("sys-quick-save-prompt-btn");
    if (quickSavePromptBtn) {
        quickSavePromptBtn.addEventListener("click", async () => {
            await saveSystemSettings(false);
        });
    }

    // Горячие клавиши Ctrl+S / Cmd+S для сохранения промпта и постфикса
    const postfixEl = document.getElementById("sys-cover-letter-postfix");
    [promptEditor, postfixEl].forEach(el => {
        if (el) {
            el.addEventListener("keydown", async (e) => {
                if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
                    e.preventDefault();
                    await saveSystemSettings(false);
                }
            });
        }
    });

    if (saveSystemSettingsBtn) {
        saveSystemSettingsBtn.addEventListener("click", async () => {
            await saveSystemSettings(true);
        });
    }

    // Кастомный выпадающий список моделей (Liquid Glass Custom Select)
    const customModelTrigger = document.getElementById("custom-model-select-trigger");
    const customModelDropdown = document.getElementById("custom-model-dropdown");
    const customModelSearchInput = document.getElementById("custom-model-search-input");
    const filterAllBtn = document.getElementById("filter-all-models-btn");
    const filterFreeBtn = document.getElementById("filter-free-models-btn");

    if (customModelTrigger && customModelDropdown) {
        customModelTrigger.addEventListener("click", (e) => {
            e.stopPropagation();
            const isHidden = customModelDropdown.classList.contains("hide");
            if (isHidden) {
                customModelDropdown.classList.remove("hide");
                if (customModelSearchInput) {
                    customModelSearchInput.value = "";
                    customModelSearchInput.focus();
                    filterCustomModelOptions("");
                }
            } else {
                customModelDropdown.classList.add("hide");
            }
        });
    }

    // Закрытие кастомного селекта при клике вне его области
    document.addEventListener("click", (e) => {
        if (customModelDropdown && !customModelDropdown.classList.contains("hide")) {
            const container = document.getElementById("custom-model-select-container");
            if (container && !container.contains(e.target)) {
                customModelDropdown.classList.add("hide");
            }
        }
    });

    if (customModelSearchInput) {
        customModelSearchInput.addEventListener("input", (e) => {
            filterCustomModelOptions(e.target.value);
        });
        customModelSearchInput.addEventListener("click", (e) => {
            e.stopPropagation();
        });
    }

    if (filterAllBtn && filterFreeBtn) {
        filterAllBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            filterAllBtn.classList.add("active");
            filterFreeBtn.classList.remove("active");
            currentFreeOnlyFilter = false;
            filterCustomModelOptions(customModelSearchInput ? customModelSearchInput.value : "");
        });
        filterFreeBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            filterFreeBtn.classList.add("active");
            filterAllBtn.classList.remove("active");
            currentFreeOnlyFilter = true;
            filterCustomModelOptions(customModelSearchInput ? customModelSearchInput.value : "");
        });
    }

    // Кнопки синхронизации моделей с API
    const syncModelsBtn = document.getElementById("sync-models-btn");
    const dropdownSyncBtn = document.getElementById("dropdown-sync-btn");
    if (syncModelsBtn) {
        syncModelsBtn.addEventListener("click", () => syncProviderModels(activeUnifiedProvider));
    }
    if (dropdownSyncBtn) {
        dropdownSyncBtn.addEventListener("click", () => syncProviderModels(activeUnifiedProvider));
    }

    // Модальное окно каталога всех моделей
    const openCatalogBtn = document.getElementById("open-catalog-modal-btn");
    const catalogModal = document.getElementById("models-catalog-modal");
    const catalogCloseBtn = document.getElementById("models-catalog-close-btn");
    const catalogDoneBtn = document.getElementById("models-catalog-done-btn");
    const catalogSyncAllBtn = document.getElementById("catalog-sync-all-btn");
    const catalogSearchInput = document.getElementById("catalog-search-input");

    if (openCatalogBtn) {
        openCatalogBtn.addEventListener("click", () => {
            openModelsCatalogModal(activeUnifiedProvider || "all");
        });
    }
    if (catalogCloseBtn && catalogModal) {
        catalogCloseBtn.addEventListener("click", () => {
            catalogModal.classList.add("hide");
        });
    }
    if (catalogDoneBtn && catalogModal) {
        catalogDoneBtn.addEventListener("click", () => {
            catalogModal.classList.add("hide");
        });
    }
    if (catalogSyncAllBtn) {
        catalogSyncAllBtn.addEventListener("click", () => {
            syncProviderModels("all");
        });
    }
    if (catalogSearchInput) {
        catalogSearchInput.addEventListener("input", (e) => {
            renderCatalogCards();
        });
    }

    // Добавление кастомного OpenAI провайдера
    const openAddCustomBtn = document.getElementById("open-add-custom-provider-btn");
    const acpCloseBtn = document.getElementById("acp-close-btn");
    const acpCancelBtn = document.getElementById("acp-cancel-btn");
    const acpModal = document.getElementById("add-custom-provider-modal");
    const acpSaveBtn = document.getElementById("acp-save-btn");

    if (openAddCustomBtn) {
        openAddCustomBtn.addEventListener("click", () => openAddCustomProviderModal());
    }
    if (acpCloseBtn && acpModal) {
        acpCloseBtn.addEventListener("click", () => acpModal.classList.add("hide"));
    }
    if (acpCancelBtn && acpModal) {
        acpCancelBtn.addEventListener("click", () => acpModal.classList.add("hide"));
    }
    if (acpSaveBtn) {
        acpSaveBtn.addEventListener("click", () => saveCustomProvider());
    }

    // Детальное модальное окно провайдера
    const pdmCloseBtn = document.getElementById("pdm-close-btn");
    const pdmCancelBtn = document.getElementById("pdm-cancel-btn");
    const pdmModal = document.getElementById("provider-detail-modal");
    const pdmSaveBtn = document.getElementById("pdm-save-btn");
    const pdmDeleteBtn = document.getElementById("pdm-delete-btn");
    const pdmTempSlider = document.getElementById("pdm-temperature");
    const pdmTempVal = document.getElementById("pdm-temp-value");
    const pdmAddKeyBtn = document.getElementById("pdm-add-key-btn");
    const pdmNewKeyInput = document.getElementById("pdm-new-key-input");
    const pdmSyncBtn = document.getElementById("pdm-sync-models-btn");

    if (pdmCloseBtn && pdmModal) {
        pdmCloseBtn.addEventListener("click", () => {
            pdmModal.classList.add("hide");
            renderProvidersCards();
            updateDashboardAiStatus();
        });
    }
    if (pdmCancelBtn && pdmModal) {
        pdmCancelBtn.addEventListener("click", () => {
            pdmModal.classList.add("hide");
            renderProvidersCards();
            updateDashboardAiStatus();
        });
    }
    if (pdmSaveBtn) {
        pdmSaveBtn.addEventListener("click", () => saveProviderDetailSettings());
    }
    if (pdmDeleteBtn) {
        pdmDeleteBtn.addEventListener("click", () => deleteCustomProvider(currentDetailProviderId));
    }
    if (pdmTempSlider && pdmTempVal) {
        pdmTempSlider.addEventListener("input", (e) => {
            pdmTempVal.textContent = parseFloat(e.target.value).toFixed(2);
        });
    }

    async function handleAddDetailKey() {
        if (!pdmNewKeyInput) return;
        const keyVal = pdmNewKeyInput.value.trim();
        if (!keyVal) {
            showToast("Введите API-ключ", "error");
            return;
        }
        const parsed = parseKeysList(keyVal);
        if (parsed.length === 0) {
            showToast("Не удалось извлечь валидный ключ", "error");
            return;
        }
        let addedCount = 0;
        parsed.forEach(k => {
            if (!currentDetailKeys.includes(k)) {
                currentDetailKeys.push(k);
                addedCount++;
            }
        });
        if (addedCount === 0) {
            showToast("Указанные ключи уже есть в пуле", "warning");
        } else {
            showToast(`Добавлено ключей: ${addedCount}`, "success");
        }
        pdmNewKeyInput.value = "";
        renderProviderDetailKeys();
        await autoSaveProviderKeys();
    }

    if (pdmAddKeyBtn) pdmAddKeyBtn.addEventListener("click", handleAddDetailKey);
    if (pdmNewKeyInput) {
        pdmNewKeyInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                handleAddDetailKey();
            }
        });
    }

    if (pdmSyncBtn) {
        pdmSyncBtn.addEventListener("click", async () => {
            const syncIcon = document.getElementById("pdm-sync-icon");
            const syncText = document.getElementById("pdm-sync-btn-text");
            if (syncIcon) syncIcon.classList.add("spinning");
            if (syncText) syncText.textContent = "Синхронизация...";
            try {
                const res = await fetch("/api/models/sync", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ provider: currentDetailProviderId || "all" })
                });
                if (res.ok) {
                    const data = await res.json();
                    showToast(`Синхронизировано моделей: ${data.total || 0}`, "success");
                    const pRes = await fetch(`/api/providers/${encodeURIComponent(currentDetailProviderId)}`);
                    if (pRes.ok) {
                        const pData = await pRes.json();
                        let models = pData.models || [];
                        if (currentDetailProviderId === "openrouter") {
                            models = models.filter(m => !DEAD_OPENROUTER_MODELS.has(m.model_id || m.id));
                        }
                        const modelSelect = document.getElementById("pdm-model-select");
                        if (modelSelect) {
                            let currentSelected = modelSelect.value;
                            if (currentDetailProviderId === "openrouter" && (!currentSelected || DEAD_OPENROUTER_MODELS.has(currentSelected))) {
                                currentSelected = "openrouter/free";
                            }
                            modelSelect.innerHTML = "";
                            models.forEach(m => {
                                const opt = document.createElement("option");
                                opt.value = m.model_id;
                                let label = m.display_name || m.model_id;
                                if (m.context_window) label += ` [${m.context_window}]`;
                                opt.textContent = label;
                                if (m.model_id === currentSelected) opt.selected = true;
                                modelSelect.appendChild(opt);
                            });
                            if (currentSelected && models.some(m => m.model_id === currentSelected)) {
                                modelSelect.value = currentSelected;
                            }
                        }
                    }
                } else {
                    showToast("Ошибка синхронизации моделей", "error");
                }
            } catch (err) {
                showToast("Сетевая ошибка при синхронизации", "error");
            } finally {
                if (syncIcon) syncIcon.classList.remove("spinning");
                if (syncText) syncText.textContent = "Синхронизировать с API";
            }
        });
    }

    // Проверка всех ключей текущего провайдера
    const pdmProbeAllKeysBtn = document.getElementById("pdm-probe-all-keys-btn");
    if (pdmProbeAllKeysBtn) {
        pdmProbeAllKeysBtn.addEventListener("click", () => probeAllDetailKeys());
    }

    // Проверка всех настроенных провайдеров
    const probeAllProvidersBtn = document.getElementById("probe-all-providers-btn");
    if (probeAllProvidersBtn) {
        probeAllProvidersBtn.addEventListener("click", () => probeAllProviders());
    }

    // Фильтры карточек по статусам
    setupProviderFilterPills();

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
            
            const oAvail = modelData.openai ? modelData.openai.available : 0;
            const oTotal = modelData.openai ? modelData.openai.total : 0;
            const gAvail = modelData.gemini ? modelData.gemini.available : 0;
            const gTotal = modelData.gemini ? modelData.gemini.total : 0;
            const mAvail = modelData.mistral ? modelData.mistral.available : 0;
            const mTotal = modelData.mistral ? modelData.mistral.total : 0;

            const activeProviders = [];
            if (oTotal > 0) {
                const oPresetKey = (modelData.openai && modelData.openai.preset) || (userSettings && userSettings.openai_provider_preset) || "groq";
                const pObj = unifiedProvidersMap[oPresetKey] || openaiPresetsMap[oPresetKey];
                let oName = pObj ? (pObj.name || "").replace(/^[^\wа-яА-ЯёЁ]+/, '').trim() : "";
                if (!oName) {
                    const presetNames = { groq: "Groq", openrouter: "OpenRouter", cerebras: "Cerebras", github: "GitHub Models", custom: "Custom OpenAI" };
                    oName = presetNames[oPresetKey] || "OpenAI";
                }
                activeProviders.push(`${oName} (${oAvail}/${oTotal})`);
            }
            if (gTotal > 0) activeProviders.push(`Gemini (${gAvail}/${gTotal})`);
            if (mTotal > 0) activeProviders.push(`Mistral (${mAvail}/${mTotal})`);

            if (activeProviders.length > 0) {
                aiLabel.textContent = activeProviders.join(" + ");
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
        
        if (userSettings.gemini_api_keys !== undefined || userSettings.mistral_api_keys !== undefined || userSettings.openai_api_keys !== undefined) {
            updateKeysPoolFromSettings(userSettings.gemini_api_keys, userSettings.mistral_api_keys, userSettings.openai_api_keys);
        }

        // Загрузка условий автоостановки в модальное окно и бейдж
        updateLimitsModalUIFromSettings();
        
        // Подгружаем список доступных моделей
        await loadModelsDropdown(userSettings.gemini_model, userSettings.mistral_model);
        
        // Подгружаем пресеты OpenAI провайдеров
        await loadOpenaiPresets();
        
        // Подгружаем единые провайдеры
        await loadUnifiedProviders();
        
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
            const mistralModels = data.mistral || ["open-mistral-nemo", "ministral-8b-latest", "ministral-3b-latest", "mistral-small-latest", "codestral-latest", "mistral-large-latest"];
            mistralSelects.forEach(sel => {
                const currentMistralVal = selectedMistralModel || sel.value || "open-mistral-nemo";
                sel.innerHTML = "";
                mistralModels.forEach(m => {
                    const opt = document.createElement("option");
                    opt.value = m;
                    let desc = m;
                    if (m === "open-mistral-nemo") desc += " (Рекомендуемая, 12B, бесплатная)";
                    else if (m === "ministral-8b-latest") desc += " (8B, быстрая)";
                    else if (m === "ministral-3b-latest") desc += " (3B, сверхбыстрая)";
                    else if (m === "codestral-latest") desc += " (Для кода и IT)";
                    else if (m === "mistral-small-latest") desc += " (Платный тариф)";
                    else if (m === "mistral-large-latest") desc += " (Максимальное качество, платная)";
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

// Загрузка пресетов бесплатных OpenAI провайдеров
async function loadOpenaiPresets() {
    try {
        const response = await fetch("/api/openai-presets");
        if (response.ok) {
            const data = await response.json();
            if (data.presets && Array.isArray(data.presets)) {
                openaiPresetsMap = {};
                data.presets.forEach(p => {
                    openaiPresetsMap[p.id] = p;
                });
            }
        }
    } catch (e) {
        console.error("Error loading OpenAI presets:", e);
    }
}

// Обработка переключения пресета OpenAI провайдера
function handleOpenaiPresetChange(presetId, selectedModel = null, customBaseUrl = null) {
    const preset = openaiPresetsMap[presetId];
    const descEl = document.getElementById("sys-openai-preset-desc");
    const keyLinkEl = document.getElementById("sys-openai-get-key-link");
    const modelSelect = document.getElementById("sys-openai-model-select");
    const urlContainer = document.getElementById("sys-openai-url-container");
    const baseUrlInput = document.getElementById("sys-openai-base-url");

    if (!preset) return;

    if (descEl) {
        descEl.textContent = `${preset.badge || "⚡"} ${preset.description || ""}`;
    }

    if (keyLinkEl) {
        if (preset.get_key_url) {
            keyLinkEl.style.display = "inline-flex";
            keyLinkEl.href = preset.get_key_url;
            keyLinkEl.innerHTML = `<span>🔗 Получить ключ ${preset.name}</span><span>↗</span>`;
        } else {
            keyLinkEl.style.display = "none";
        }
    }

    if (modelSelect) {
        modelSelect.innerHTML = "";
        const models = preset.models || [preset.default_model];
        models.forEach(m => {
            const opt = document.createElement("option");
            opt.value = m;
            let label = m;
            if (m === preset.default_model) {
                label += " (Рекомендуемая)";
            }
            opt.textContent = label;
            modelSelect.appendChild(opt);
        });

        const targetModel = selectedModel || preset.default_model;
        if (models.includes(targetModel)) {
            modelSelect.value = targetModel;
        } else if (presetId === "custom" && targetModel) {
            const customOpt = document.createElement("option");
            customOpt.value = targetModel;
            customOpt.textContent = targetModel;
            modelSelect.appendChild(customOpt);
            modelSelect.value = targetModel;
        } else if (modelSelect.options.length > 0) {
            modelSelect.value = modelSelect.options[0].value;
        }
    }

    if (baseUrlInput) {
        if (customBaseUrl !== null && customBaseUrl !== undefined && customBaseUrl !== "") {
            baseUrlInput.value = customBaseUrl;
        } else {
            baseUrlInput.value = preset.base_url || "";
        }
    }

    if (urlContainer) {
        urlContainer.style.display = presetId === "custom" ? "block" : "none";
    }

    // Если сейчас открыт таб OpenAI в менеджере ключей, обновим там тоже вендорную подсказку
    if (activeKeyManagerTab === "openai") {
        const vendorDesc = document.getElementById("key-vendor-desc");
        const vendorLink = document.getElementById("key-vendor-link");
        const newKeyInput = document.getElementById("new-key-input");
        if (vendorDesc) vendorDesc.textContent = `${preset.badge || "⚡"} ${preset.name}: ${preset.description}`;
        if (vendorLink) {
            if (preset.get_key_url) {
                vendorLink.style.display = "inline-flex";
                vendorLink.href = preset.get_key_url;
                vendorLink.innerHTML = `<span>Получить ключ ${preset.name}</span><span>↗</span>`;
            } else {
                vendorLink.style.display = "none";
            }
        }
        if (newKeyInput && preset.key_prefix_hint) {
            newKeyInput.placeholder = `Вставьте ключ (${preset.key_prefix_hint})`;
        }
    }
}

// ----------------------------------------------------
// Единая система LLM-провайдеров и динамический каталог моделей
// ----------------------------------------------------

// Загрузка метаданных всех доступных провайдеров
async function loadUnifiedProviders() {
    try {
        const response = await fetch("/api/providers");
        if (response.ok) {
            const data = await response.json();
            if (data.providers && Array.isArray(data.providers)) {
                unifiedProvidersMap = {};
                data.providers.forEach(p => {
                    unifiedProvidersMap[p.id] = p;
                });
            }
            updateDashboardAiStatus();
        }
    } catch (e) {
        console.error("Error loading unified providers:", e);
    }
}

// Обновление живого AI статуса в шапке дашборда и в боковой панели
function updateDashboardAiStatus() {
    const pill = document.getElementById("dashboard-ai-status-pill");
    const dot = document.getElementById("dashboard-ai-status-dot");
    const text = document.getElementById("dashboard-ai-status-text");

    const aiSideDot = document.getElementById("ai-status-dot");
    const aiSideLabel = document.getElementById("ai-status-label");

    const providers = Object.values(unifiedProvidersMap);
    if (providers.length === 0) {
        if (pill && dot && text) {
            pill.className = "ai-status-pill status-unconfigured";
            dot.className = "status-dot gray";
            text.textContent = "AI: не настроен";
            pill.title = "Провайдеры не настроены. Нажмите для открытия настроек.";
        }
        return;
    }

    const activeList = providers.filter(p => p.operational_status === "active" && p.is_enabled !== false);
    const rateLimitedList = providers.filter(p => p.operational_status === "rate_limited" && p.is_enabled !== false);
    
    // Преобразуем legacy "openai" в конкретный пресет
    let primaryId = activeUnifiedProvider || (userSettings && userSettings.primary_provider) || "groq";
    if (primaryId === "openai") {
        primaryId = (userSettings && userSettings.openai_provider_preset) || "groq";
    }
    const primaryProvider = unifiedProvidersMap[primaryId] || activeList[0];

    if (activeList.length > 0) {
        const primaryBadge = primaryProvider ? (primaryProvider.badge || "⚡") : "⚡";
        let rawName = primaryProvider ? (primaryProvider.name || "AI") : "AI";
        const primaryName = rawName.replace(/^[^\wа-яА-ЯёЁ]+/, '').trim() || rawName;

        if (pill && dot && text) {
            pill.className = "ai-status-pill status-active";
            dot.className = "status-dot green";
            text.textContent = `${primaryBadge} ${primaryName}: ${activeList.length} активен`;
            pill.title = `Активных AI-провайдеров: ${activeList.length}. Основной: ${primaryName}. Нажмите для детальных настроек.`;
        }

        if (aiSideDot && aiSideLabel) {
            aiSideDot.className = "pulse-dot active";
            aiSideDot.style.backgroundColor = "";
            const activeSummary = activeList.map(p => {
                const clean = (p.name || "").replace(/^[^\wа-яА-ЯёЁ]+/, '').trim() || p.id;
                const kCount = p.status_info && p.status_info.available_keys_count !== undefined 
                    ? p.status_info.available_keys_count 
                    : (p.keys_count || (p.api_keys ? p.api_keys.length : 0));
                const totalK = p.keys_count || (p.api_keys ? p.api_keys.length : 0);
                return `${clean} (${kCount}/${totalK})`;
            }).join(" + ");
            aiSideLabel.textContent = activeSummary || `${primaryName} (OK)`;
            aiSideLabel.style.color = "var(--accent-green)";
        }
    } else if (rateLimitedList.length > 0) {
        if (pill && dot && text) {
            pill.className = "ai-status-pill status-rate_limited";
            dot.className = "status-dot amber";
            text.textContent = `⚠️ AI: лимит исчерпан (${rateLimitedList.length})`;
            pill.title = `У ${rateLimitedList.length} провайдеров исчерпаны лимиты (HTTP 429). Нажмите для добавления ключей.`;
        }

        if (aiSideDot && aiSideLabel) {
            aiSideDot.className = "pulse-dot";
            aiSideDot.style.backgroundColor = "var(--accent-amber, #f59e0b)";
            aiSideDot.style.boxShadow = "0 0 10px rgba(245, 158, 11, 0.4)";
            aiSideLabel.textContent = `Лимиты исчерпаны (429)`;
            aiSideLabel.style.color = "#fbbf24";
        }
    } else {
        if (pill && dot && text) {
            pill.className = "ai-status-pill status-unconfigured";
            dot.className = "status-dot gray";
            text.textContent = "⚪️ AI: не настроен";
            pill.title = "Нет добавленных API-ключей. Нажмите для настройки провайдеров.";
        }

        if (aiSideDot && aiSideLabel) {
            aiSideDot.className = "pulse-dot";
            aiSideDot.style.backgroundColor = "var(--accent-red)";
            aiSideDot.style.boxShadow = "0 0 10px var(--accent-red)";
            aiSideLabel.textContent = "Ключи не настроены";
            aiSideLabel.style.color = "var(--accent-red)";
        }
    }
}

// Настройка интерактивных фильтров по статусам провайдеров
function setupProviderFilterPills() {
    const container = document.getElementById("provider-status-filters");
    if (!container) return;
    const pills = container.querySelectorAll(".provider-filter-pill");
    pills.forEach(pill => {
        pill.onclick = () => {
            pills.forEach(p => p.classList.remove("active"));
            pill.classList.add("active");
            currentProviderStatusFilter = pill.getAttribute("data-status-filter") || "all";
            renderProvidersCards();
        };
    });
}

// Проверка всех настроенных провайдеров
async function probeAllProviders() {
    const btn = document.getElementById("probe-all-providers-btn");
    const icon = document.getElementById("probe-all-icon");
    if (btn) btn.disabled = true;
    if (icon) icon.className = "spin-icon";

    try {
        const res = await fetch("/api/providers/probe-all", { method: "POST" });
        if (res.ok) {
            const data = await res.json();
            if (data.providers && Array.isArray(data.providers)) {
                unifiedProvidersMap = {};
                data.providers.forEach(p => {
                    unifiedProvidersMap[p.id] = p;
                });
            }
            renderProvidersCards();
            updateDashboardAiStatus();
            const activeCount = Object.values(unifiedProvidersMap).filter(p => p.operational_status === "active").length;
            const rateLimitedCount = Object.values(unifiedProvidersMap).filter(p => p.operational_status === "rate_limited").length;
            showToast(`Проверка завершена: ${activeCount} активны, ${rateLimitedCount} исчерпали лимит`, activeCount > 0 ? "success" : "warning");
        } else {
            showToast("Не удалось проверить статус провайдеров", "error");
        }
    } catch (e) {
        console.error("Error probing all providers:", e);
        showToast("Ошибка сети при проверке провайдеров", "error");
    } finally {
        if (btn) btn.disabled = false;
        if (icon) icon.className = "";
    }
}

// Создание элемента карточки провайдера
function createProviderCardElement(p) {
    const isPrimary = (p.id === activeUnifiedProvider) || (p.is_primary);
    const opStatus = p.operational_status || "unconfigured";
    const statusInfo = p.status_info || {};

    const card = document.createElement("div");
    card.className = `provider-card ${isPrimary ? 'primary' : ''} ${!p.is_enabled ? 'disabled' : ''} status-${opStatus}`;
    card.setAttribute("data-provider-id", p.id);

    const keysCount = p.keys_count !== undefined ? p.keys_count : (p.api_keys ? p.api_keys.length : 0);

    // Определяем индикаторы и бейдж статуса
    let dotClass = "gray";
    let statusBadgeClass = "status-unconfigured";
    let statusText = "Не настроен";

    if (!p.is_enabled) {
        dotClass = "red";
        statusBadgeClass = "status-disabled";
        statusText = "Отключен";
    } else if (opStatus === "active") {
        dotClass = "green";
        statusBadgeClass = "status-active";
        const avail = statusInfo.available_keys_count !== undefined ? statusInfo.available_keys_count : keysCount;
        statusText = `Активен (${avail} кл.)`;
    } else if (opStatus === "rate_limited") {
        dotClass = "amber";
        statusBadgeClass = "status-rate_limited";
        statusText = "Лимит исчерпан (429)";
    } else if (opStatus === "error" || opStatus === "unavailable") {
        dotClass = "red";
        statusBadgeClass = "status-error";
        statusText = "Ошибка";
    } else {
        dotClass = "gray";
        statusBadgeClass = "status-unconfigured";
        statusText = "Не настроен";
    }

    // Заметка / подсказка внутри карточки
    let noticeHtml = "";
    if (!p.is_enabled) {
        noticeHtml = `<div class="provider-card-notice muted"><span>🔴</span> <span>Провайдер отключен в настройках</span></div>`;
    } else if (opStatus === "rate_limited") {
        noticeHtml = `<div class="provider-card-notice warning"><span>⚠️</span> <span>Лимиты исчерпаны (429). Ротация переключит на fallback.</span></div>`;
    } else if (opStatus === "error" || opStatus === "unavailable") {
        noticeHtml = `<div class="provider-card-notice error" style="background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.25); color: #fca5a5;"><span>❌</span> <span>${escapeHtml(statusInfo.message || 'Ошибка валидации ключа или модели. Нажмите для настройки.')}</span></div>`;
    } else if (opStatus === "unconfigured") {
        noticeHtml = `<div class="provider-card-notice muted"><span>➕</span> <span>Ключи не добавлены. Нажмите для ввода.</span></div>`;
    } else if (opStatus === "active") {
        noticeHtml = `<div class="provider-card-notice success"><span>✓</span> <span>Готов к анализу вакансий (ротация активна)</span></div>`;
    }

    const providerIcon = p.icon || (p.name && p.name.match(/^[\p{Emoji}\u2600-\u27BF]/u) ? p.name.match(/^[\p{Emoji}\u2600-\u27BF]/u)[0] : (p.id === 'openrouter' ? '🌐' : (p.id === 'groq' ? '⚡' : (p.id === 'gemini' ? '✨' : (p.id === 'mistral' ? '🌊' : (p.id === 'github' ? '🐙' : (p.id === 'cerebras' ? '🧠' : '⚡')))))));
    const cleanName = (p.name || '').replace(/^[\p{Emoji}\u2600-\u27BF\s]+/u, '').trim() || p.name || p.id;

    card.innerHTML = `
        <div class="provider-card-header">
            <div class="provider-card-title-wrap">
                <span class="provider-card-icon">${providerIcon}</span>
                <div>
                    <div class="provider-card-name">${escapeHtml(cleanName)}</div>
                    <div style="font-size: 10px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.05em;">
                        ${p.is_custom ? 'Кастомный OpenAI' : (p.protocol || 'API')}
                    </div>
                </div>
            </div>
            <div class="provider-card-badges">
                ${isPrimary ? '<span class="glass-badge badge-def" style="background: rgba(99, 102, 241, 0.25); color: #a5b4fc;">★ Основной</span>' : ''}
                <div class="provider-status-badge ${statusBadgeClass}">
                    <span class="status-dot ${dotClass}"></span>
                    <span>${escapeHtml(statusText)}</span>
                </div>
            </div>
        </div>

        <div class="provider-card-desc">
            ${escapeHtml(p.description || 'OpenAI-совместимый эндпоинт')}
        </div>

        ${noticeHtml}

        <div class="provider-card-meta">
            <div class="provider-card-model" title="Активная модель: ${escapeHtml(p.active_model || p.default_model || 'не выбрана')}">
                <span>🤖</span>
                <span>${escapeHtml(p.active_model || p.default_model || 'не выбрана')}</span>
            </div>
            <div style="display: flex; align-items: center; gap: 8px;">
                <span style="color: #60a5fa; font-size: 10.5px; font-weight: 600;">T: ${p.temperature !== undefined ? Number(p.temperature).toFixed(2) : '0.20'}</span>
                <span class="provider-card-action">Настроить ⚙️</span>
            </div>
        </div>
    `;

    card.addEventListener("click", () => {
        openProviderDetailModal(p.id);
    });

    return card;
}

// Отрисовка секции провайдеров со своим заголовком и подсеткой
function renderProviderSection(title, count, badgeClass, subtitle, dotClass, providerList) {
    const sec = document.createElement("div");
    sec.className = "providers-section";

    const header = document.createElement("div");
    header.className = "providers-section-header";
    header.innerHTML = `
        <div class="providers-section-title-wrap">
            <span class="status-dot ${dotClass}"></span>
            <span class="providers-section-title">${escapeHtml(title)}</span>
            <span class="providers-section-badge ${badgeClass}">${count}</span>
        </div>
        <div class="providers-section-subtitle">${escapeHtml(subtitle)}</div>
    `;
    sec.appendChild(header);

    const subgrid = document.createElement("div");
    subgrid.className = "providers-cards-subgrid";
    providerList.forEach(p => {
        subgrid.appendChild(createProviderCardElement(p));
    });
    sec.appendChild(subgrid);

    return sec;
}

// Отрисовка карточек провайдеров в основном окне настроек
function renderProvidersCards() {
    const grid = document.getElementById("providers-cards-grid");
    const countBadge = document.getElementById("providers-count-badge");
    const primarySelect = document.getElementById("sys-main-primary-select");

    const providers = Object.values(unifiedProvidersMap);
    if (countBadge) {
        countBadge.textContent = `${providers.length} ${providers.length === 1 ? 'провайдер' : 'провайдеров'}`;
    }

    // Подсчет количества по статусам
    const activeCount = providers.filter(p => p.operational_status === "active" && p.is_enabled !== false).length;
    const rateLimitedCount = providers.filter(p => p.operational_status === "rate_limited" && p.is_enabled !== false).length;
    const errorCount = providers.filter(p => (p.operational_status === "error" || p.operational_status === "unavailable") && p.is_enabled !== false).length;
    const unconfiguredCount = providers.filter(p => 
        p.is_enabled !== false && 
        p.operational_status !== "active" && 
        p.operational_status !== "rate_limited" && 
        p.operational_status !== "error" && 
        p.operational_status !== "unavailable"
    ).length;

    const countAllEl = document.getElementById("filter-count-all");
    const countActiveEl = document.getElementById("filter-count-active");
    const countRateLimitedEl = document.getElementById("filter-count-rate-limited");
    const countErrorEl = document.getElementById("filter-count-error");
    const countUnconfiguredEl = document.getElementById("filter-count-unconfigured");

    if (countAllEl) countAllEl.textContent = providers.length;
    if (countActiveEl) countActiveEl.textContent = activeCount;
    if (countRateLimitedEl) countRateLimitedEl.textContent = rateLimitedCount;
    if (countErrorEl) countErrorEl.textContent = errorCount;
    if (countUnconfiguredEl) countUnconfiguredEl.textContent = unconfiguredCount;

    // Заполняем селектор основного рабочего провайдера
    if (primarySelect) {
        let currentPrimary = activeUnifiedProvider || (userSettings && userSettings.primary_provider) || "groq";
        if (currentPrimary === "openai") {
            currentPrimary = (userSettings && userSettings.openai_provider_preset) || "groq";
        }
        primarySelect.innerHTML = "";
        providers.forEach(p => {
            const opt = document.createElement("option");
            opt.value = p.id;
            let statusSuffix = "";
            if (!p.is_enabled) statusSuffix = " (отключен)";
            else if (p.operational_status === "rate_limited") statusSuffix = " (лимит 429)";
            else if (p.operational_status === "error" || p.operational_status === "unavailable") statusSuffix = " (ошибка)";
            else if (p.operational_status === "unconfigured") statusSuffix = " (нет ключа)";
            const pIcon = p.icon || (p.name && p.name.match(/^[\p{Emoji}\u2600-\u27BF]/u) ? p.name.match(/^[\p{Emoji}\u2600-\u27BF]/u)[0] : (p.id === 'openrouter' ? '🌐' : '⚡'));
            const cName = (p.name || '').replace(/^[\p{Emoji}\u2600-\u27BF\s]+/u, '').trim() || p.name || p.id;
            opt.textContent = `${pIcon} ${cName}${statusSuffix}`;
            if (p.id === currentPrimary) opt.selected = true;
            primarySelect.appendChild(opt);
        });
        primarySelect.onchange = (e) => {
            activeUnifiedProvider = e.target.value;
            renderProvidersCards();
            updateDashboardAiStatus();
        };
    }

    if (!grid) return;
    grid.innerHTML = "";

    // Если включен фильтр конкретного статуса
    if (currentProviderStatusFilter && currentProviderStatusFilter !== "all") {
        let filtered = [];
        let secTitle = "Провайдеры";
        let secBadge = "neutral";
        let secDot = "gray";
        let secSub = "";

        if (currentProviderStatusFilter === "active") {
            filtered = providers.filter(p => p.operational_status === "active");
            secTitle = "Активные провайдеры";
            secBadge = "green";
            secDot = "green";
            secSub = "Готовы к анализу вакансий и генерации писем";
        } else if (currentProviderStatusFilter === "rate_limited") {
            filtered = providers.filter(p => p.operational_status === "rate_limited");
            secTitle = "Лимит исчерпан (HTTP 429)";
            secBadge = "amber";
            secDot = "amber";
            secSub = "Превышена квота запросов вендора, активен fallback";
        } else if (currentProviderStatusFilter === "error") {
            filtered = providers.filter(p => p.operational_status === "error" || p.operational_status === "unavailable");
            secTitle = "Требуется внимание (Ошибки)";
            secBadge = "red";
            secDot = "red";
            secSub = "Ключи не прошли валидацию или модель недоступна";
        } else if (currentProviderStatusFilter === "unconfigured") {
            filtered = providers.filter(p => 
                p.operational_status !== "active" && 
                p.operational_status !== "rate_limited" && 
                p.operational_status !== "error" && 
                p.operational_status !== "unavailable"
            );
            secTitle = "Не настроены";
            secBadge = "neutral";
            secDot = "gray";
            secSub = "Требуется ввод API-ключа для активации";
        }

        if (filtered.length === 0) {
            const filterTitle = currentProviderStatusFilter === 'active' ? 'Активные' : (currentProviderStatusFilter === 'rate_limited' ? 'Лимит исчерпан' : (currentProviderStatusFilter === 'error' ? 'Ошибки' : 'Не настроены'));
            grid.innerHTML = `
                <div style="text-align: center; padding: 26px 16px; color: var(--text-secondary); font-size: 12.5px; border: 1px dashed rgba(255,255,255,0.08); border-radius: 8px;">
                    В статусе «${escapeHtml(filterTitle)}» нет провайдеров.
                </div>
            `;
            return;
        }

        grid.appendChild(renderProviderSection(secTitle, filtered.length, secBadge, secSub, secDot, filtered));
        return;
    }

    // Режим "all": разделяем визуально на секции (Активные, Ошибки, Лимиты исчерпаны, Не настроены, Отключенные)
    const activeGroup = providers.filter(p => p.is_enabled !== false && p.operational_status === "active");
    const errorGroup = providers.filter(p => p.is_enabled !== false && (p.operational_status === "error" || p.operational_status === "unavailable"));
    const rateLimitedGroup = providers.filter(p => p.is_enabled !== false && p.operational_status === "rate_limited");
    const disabledGroup = providers.filter(p => p.is_enabled === false);

    // В unconfiguredGroup гарантированно собираем всех оставшихся, чтобы ни один провайдер не потерялся
    const unconfiguredGroup = providers.filter(p => 
        p.is_enabled !== false && 
        !activeGroup.includes(p) && 
        !errorGroup.includes(p) && 
        !rateLimitedGroup.includes(p)
    );

    if (activeGroup.length > 0) {
        grid.appendChild(renderProviderSection("Активные провайдеры", activeGroup.length, "green", "Готовы к анализу вакансий и автооткликам", "green", activeGroup));
    }

    if (errorGroup.length > 0) {
        grid.appendChild(renderProviderSection("Требуется внимание (Ошибки ключа / Модели)", errorGroup.length, "red", "Ключи не прошли валидацию или модель недоступна. Нажмите для настройки", "red", errorGroup));
    }

    if (rateLimitedGroup.length > 0) {
        grid.appendChild(renderProviderSection("Лимиты исчерпаны (HTTP 429)", rateLimitedGroup.length, "amber", "Квота вендора временно превышена, запросы перенаправляются на fallback", "amber", rateLimitedGroup));
    }

    if (unconfiguredGroup.length > 0) {
        grid.appendChild(renderProviderSection("Не настроены", unconfiguredGroup.length, "neutral", "Добавьте API-ключ в карточке провайдера для подключения", "gray", unconfiguredGroup));
    }

    if (disabledGroup.length > 0) {
        grid.appendChild(renderProviderSection("Отключенные провайдеры", disabledGroup.length, "red", "Выключены в настройках и не участвуют в ротации", "red", disabledGroup));
    }

    if (providers.length === 0) {
        grid.innerHTML = `
            <div style="text-align: center; padding: 26px 16px; color: var(--text-secondary); font-size: 12.5px; border: 1px dashed rgba(255,255,255,0.08); border-radius: 8px;">
                Нет доступных провайдеров.
            </div>
        `;
    }
}

// Обновление статус-баннера в модальном окне провайдера
function updatePdmStatusBanner(p) {
    const statusBanner = document.getElementById("pdm-status-banner");
    const statusDot = document.getElementById("pdm-status-dot");
    const statusTitle = document.getElementById("pdm-status-title");
    const statusDesc = document.getElementById("pdm-status-desc");
    if (!statusBanner || !statusDot || !statusTitle || !statusDesc) return;

    const opStatus = p.operational_status || "unconfigured";
    const sInfo = p.status_info || {};

    statusBanner.className = `pdm-status-banner ${opStatus === 'rate_limited' ? 'rate-limited' : opStatus}`;

    if (!p.is_enabled) {
        statusDot.className = "status-dot red";
        statusTitle.textContent = "🔴 Провайдер отключен";
        statusDesc.textContent = "Провайдер выключен и не участвует в обработке вакансий. Переключите тумблер ниже для включения.";
    } else if (opStatus === "active") {
        statusDot.className = "status-dot green";
        statusTitle.textContent = "🟢 Провайдер активен и готов к работе";
        const avail = sInfo.available_keys_count !== undefined ? sInfo.available_keys_count : currentDetailKeys.length;
        const totalK = sInfo.keys_count !== undefined ? sInfo.keys_count : currentDetailKeys.length;
        statusDesc.textContent = `Доступно рабочих ключей: ${avail} из ${totalK}. Модель: ${p.active_model || p.default_model || 'стандартная'}.`;
    } else if (opStatus === "rate_limited") {
        statusDot.className = "status-dot amber";
        statusTitle.textContent = "🟡 Лимит запросов исчерпан (HTTP 429 Quota Exceeded)";
        statusDesc.textContent = "Все ключи этого провайдера временно превысили квоту вендора. Ротация автоматически переключит анализ на резервный fallback-провайдер.";
    } else if (opStatus === "error" || opStatus === "unavailable") {
        statusDot.className = "status-dot red";
        statusTitle.textContent = "🔴 Ошибка подключения / валидации ключей";
        statusDesc.textContent = sInfo.message || "Один или несколько ключей не прошли валидацию, либо выбранная модель недоступна. Проверьте правильность ключа и модели.";
    } else {
        statusDot.className = "status-dot gray";
        statusTitle.textContent = "⚪️ Провайдер не настроен";
        statusDesc.textContent = "Не добавлено ни одного API-ключа. Вставьте ваш API-ключ ниже и нажмите «+ Добавить», чтобы активировать провайдер.";
    }
}

// Фоновое обновление статуса текущего открытого провайдера
async function refreshCurrentDetailProviderStatus() {
    if (!currentDetailProviderId) return;
    try {
        const res = await fetch(`/api/providers/${encodeURIComponent(currentDetailProviderId)}`);
        if (res.ok) {
            const data = await res.json();
            const p = data.provider || data || {};
            currentDetailKeysData = p.keys_detail || [];
            updatePdmStatusBanner(p);
            unifiedProvidersMap[p.id] = p;
            updateDashboardAiStatus();
            renderProvidersCards();
        }
    } catch (e) {
        console.error("Error refreshing provider status:", e);
    }
}

// Открытие модального окна детальной настройки провайдера
async function openProviderDetailModal(providerId) {
    currentDetailProviderId = providerId;
    const modal = document.getElementById("provider-detail-modal");
    if (!modal) return;

    modal.classList.remove("hide");

    const titleEl = document.getElementById("pdm-title");
    const iconEl = document.getElementById("pdm-icon");
    const protoBadge = document.getElementById("pdm-protocol-badge");
    const customBadge = document.getElementById("pdm-custom-badge");
    const deleteBtn = document.getElementById("pdm-delete-btn");
    const bannerDesc = document.getElementById("pdm-banner-desc");
    const getKeyLink = document.getElementById("pdm-get-key-link");
    const enabledToggle = document.getElementById("pdm-enabled-toggle");
    const primaryToggle = document.getElementById("pdm-primary-toggle");
    const modelSelect = document.getElementById("pdm-model-select");
    const tempSlider = document.getElementById("pdm-temperature");
    const tempVal = document.getElementById("pdm-temp-value");
    const baseUrlContainer = document.getElementById("pdm-base-url-container");
    const baseUrlInput = document.getElementById("pdm-base-url-input");

    try {
        const res = await fetch(`/api/providers/${encodeURIComponent(providerId)}`);
        if (!res.ok) throw new Error("Failed to load provider details");
        const data = await res.json();
        const p = data.provider || data || {};
        const models = data.models || p.models || [];

        currentDetailKeysData = p.keys_detail || [];

        const pIcon = p.icon || (p.name && p.name.match(/^[\p{Emoji}\u2600-\u27BF]/u) ? p.name.match(/^[\p{Emoji}\u2600-\u27BF]/u)[0] : (p.id === 'openrouter' ? '🌐' : (p.id === 'groq' ? '⚡' : (p.id === 'gemini' ? '✨' : (p.id === 'mistral' ? '🌊' : (p.id === 'github' ? '🐙' : (p.id === 'cerebras' ? '🧠' : '⚡')))))));
        const cleanName = (p.name || '').replace(/^[\p{Emoji}\u2600-\u27BF\s]+/u, '').trim() || p.name || providerId;

        if (titleEl) titleEl.textContent = cleanName;
        if (iconEl) iconEl.textContent = pIcon;
        if (protoBadge) protoBadge.textContent = p.protocol || "openai_compatible";
        if (customBadge) customBadge.style.display = p.is_custom ? "inline-block" : "none";
        if (deleteBtn) deleteBtn.style.display = p.is_custom ? "inline-flex" : "none";

        // Обновляем баннер статуса
        updatePdmStatusBanner(p);

        if (bannerDesc) bannerDesc.textContent = p.description || "";
        if (getKeyLink) {
            if (p.get_key_url) {
                getKeyLink.style.display = "inline-flex";
                getKeyLink.href = p.get_key_url;
                getKeyLink.innerHTML = `<span>🔗 Получить ключ ${escapeHtml(p.name)}</span><span>↗</span>`;
            } else {
                getKeyLink.style.display = "none";
            }
        }

        if (enabledToggle) enabledToggle.checked = p.is_enabled !== false;
        if (primaryToggle) primaryToggle.checked = (p.id === activeUnifiedProvider) || p.is_primary;

        const currentTemp = p.temperature !== undefined ? p.temperature : 0.2;
        if (tempSlider) tempSlider.value = currentTemp;
        if (tempVal) tempVal.textContent = parseFloat(currentTemp).toFixed(2);

        if (baseUrlContainer) {
            baseUrlContainer.style.display = (p.protocol === "gemini" || p.protocol === "mistral") ? "none" : "block";
        }
        if (baseUrlInput) {
            baseUrlInput.value = p.base_url || "";
        }

        // Заполняем модели
        if (modelSelect) {
            modelSelect.innerHTML = "";
            let filteredModels = (p.id === "openrouter")
                ? models.filter(m => !DEAD_OPENROUTER_MODELS.has(m.model_id || m.id))
                : models;
            let activeModel = p.active_model || p.default_model || (filteredModels[0] ? filteredModels[0].model_id : "");
            if (p.id === "openrouter" && (!activeModel || DEAD_OPENROUTER_MODELS.has(activeModel))) {
                activeModel = "openrouter/free";
            }
            
            if (filteredModels.length === 0 && activeModel) {
                const opt = document.createElement("option");
                opt.value = activeModel;
                opt.textContent = activeModel;
                modelSelect.appendChild(opt);
            } else {
                filteredModels.forEach(m => {
                    const opt = document.createElement("option");
                    opt.value = m.model_id;
                    let label = m.display_name || m.model_id;
                    if (m.context_window) label += ` [${m.context_window}]`;
                    opt.textContent = label;
                    if (m.model_id === activeModel) opt.selected = true;
                    modelSelect.appendChild(opt);
                });
            }
            if (activeModel) modelSelect.value = activeModel;
        }

        // Загружаем ключи
        currentDetailKeys = parseKeysList(p.api_keys);
        renderProviderDetailKeys();

    } catch (e) {
        console.error("Error opening provider detail modal:", e);
        showToast("Ошибка загрузки данных провайдера", "error");
    }
}

// Проверка всех ключей в модалке текущего провайдера
async function probeAllDetailKeys() {
    if (!currentDetailProviderId || currentDetailKeys.length === 0) {
        showToast("Нет ключей для проверки", "warning");
        return;
    }
    const btn = document.getElementById("pdm-probe-all-keys-btn");
    const origText = btn ? btn.innerHTML : "";
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = "<span>⏳ Проверка...</span>";
    }

    let successCount = 0;
    let rateLimitedCount = 0;
    let errorCount = 0;

    for (let i = 0; i < currentDetailKeys.length; i++) {
        const cleanKey = String(currentDetailKeys[i] || "").trim().replace(/^["'\[]+|["'\]]+$/g, '');
        try {
            const res = await fetch(`/api/providers/${encodeURIComponent(currentDetailProviderId)}/probe`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ api_key: cleanKey })
            });
            const result = await res.json();
            
            let status = "invalid";
            if (result.success) {
                status = "valid";
                successCount++;
            } else if (result.reason === "rate_limited" || result.status_code === 429) {
                status = "rate_limited";
                rateLimitedCount++;
            } else {
                status = "error";
                errorCount++;
            }

            const existingIdx = currentDetailKeysData.findIndex(kd => kd.key === cleanKey);
            const entry = {
                key: cleanKey,
                masked: cleanKey.length > 14 ? `${cleanKey.substring(0, 6)}••••••••${cleanKey.substring(cleanKey.length - 4)}` : cleanKey,
                status: status,
                reason: result.reason || result.error || null,
                detail: result.detail || result.error || null,
                latency_ms: result.latency_ms || 0
            };
            if (existingIdx >= 0) {
                currentDetailKeysData[existingIdx] = entry;
            } else {
                currentDetailKeysData.push(entry);
            }
        } catch (err) {
            errorCount++;
        }
    }

    renderProviderDetailKeys();
    await refreshCurrentDetailProviderStatus();

    if (btn) {
        btn.disabled = false;
        btn.innerHTML = origText;
    }

    showToast(`Проверено ключей: ${successCount} активны, ${rateLimitedCount} лимит 429, ${errorCount} ошибок`, successCount > 0 ? "success" : "warning");
}

// Отрисовка списка ключей внутри модалки провайдера
function renderProviderDetailKeys() {
    const container = document.getElementById("pdm-keys-list");
    const countEl = document.getElementById("pdm-keys-count");
    if (countEl) countEl.textContent = currentDetailKeys.length;
    if (!container) return;

    container.innerHTML = "";

    if (currentDetailKeys.length === 0) {
        container.innerHTML = `
            <div style="text-align: center; padding: 14px; color: var(--text-secondary); font-size: 11.5px; border: 1px dashed rgba(255,255,255,0.08); border-radius: 6px;">
                Ключи еще не добавлены. Вставьте ключ выше и нажмите «+ Добавить».
            </div>
        `;
        return;
    }

    currentDetailKeys.forEach((key, idx) => {
        const card = document.createElement("div");
        card.className = "key-item-card";
        card.style.cssText = "display: flex; align-items: center; justify-content: space-between; padding: 7px 10px; background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.08); border-radius: 6px; gap: 8px;";

        const cleanKey = String(key || "").trim().replace(/^["'\[]+|["'\]]+$/g, '');
        const maskedKey = cleanKey.length > 14 ? `${cleanKey.substring(0, 6)}••••••••${cleanKey.substring(cleanKey.length - 4)}` : cleanKey;
        const kd = (currentDetailKeysData || []).find(k => k.key === cleanKey) || {};
        const kStatus = kd.status || "untested";

        let keyStatusBadge = '<span class="glass-badge" style="background: rgba(148, 163, 184, 0.12); color: #94a3b8; font-size: 10px;">⚪️ Не проверен</span>';
        if (kStatus === "valid") {
            const msText = kd.latency_ms ? ` (${kd.latency_ms}ms)` : '';
            keyStatusBadge = `<span class="glass-badge" style="background: rgba(16, 185, 129, 0.15); color: #34d399; font-size: 10px;">🟢 Работает${msText}</span>`;
        } else if (kStatus === "rate_limited") {
            keyStatusBadge = `<span class="glass-badge" style="background: rgba(245, 158, 11, 0.2); color: #fbbf24; font-size: 10px;">🟡 429 Лимит исчерпан</span>`;
        } else if (kStatus === "invalid" || kStatus === "error") {
            keyStatusBadge = `<span class="glass-badge" style="background: rgba(239, 68, 68, 0.18); color: #f87171; font-size: 10px;">🔴 Ошибка</span>`;
        }

        let detailMsgHtml = "";
        if (kd.detail && (kStatus === "rate_limited" || kStatus === "invalid" || kStatus === "error")) {
            detailMsgHtml = `<div style="font-size: 10px; color: #fca5a5; margin-top: 3px; max-width: 480px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(kd.detail)}</div>`;
        }

        card.innerHTML = `
            <div style="display: flex; flex-direction: column; gap: 2px; flex: 1; min-width: 0;">
                <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                    <span style="font-size: 11px; color: var(--text-secondary); font-weight: 600; flex-shrink: 0;">#${idx + 1}</span>
                    <span class="pdm-key-text" style="font-family: monospace; font-size: 11.5px; color: #e2e8f0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; word-break: break-all;">${escapeHtml(maskedKey)}</span>
                    ${keyStatusBadge}
                </div>
                ${detailMsgHtml}
            </div>
            <div style="display: flex; align-items: center; gap: 4px; flex-shrink: 0;">
                <button type="button" class="btn btn-secondary btn-xs pdm-key-reveal" title="Показать/скрыть" style="padding: 2px 6px; font-size: 11px;">👁️</button>
                <button type="button" class="btn btn-secondary btn-xs pdm-key-copy" title="Скопировать ключ в буфер" style="padding: 2px 6px; font-size: 11px;">📋</button>
                <button type="button" class="btn btn-secondary btn-xs pdm-key-edit" title="Редактировать ключ" style="padding: 2px 6px; font-size: 11px;">✏️</button>
                <button type="button" class="btn btn-secondary btn-xs pdm-key-probe" title="Проверить ключ через API" style="padding: 2px 8px; font-size: 10.5px; color: #34d399;">Проверить</button>
                <button type="button" class="btn btn-secondary btn-xs pdm-key-delete" title="Удалить ключ" style="padding: 2px 6px; font-size: 11px; color: #f87171;">🗑️</button>
            </div>
        `;

        let revealed = false;
        const textSpan = card.querySelector(".pdm-key-text");
        const revealBtn = card.querySelector(".pdm-key-reveal");
        const copyBtn = card.querySelector(".pdm-key-copy");
        const editBtn = card.querySelector(".pdm-key-edit");
        const probeBtn = card.querySelector(".pdm-key-probe");
        const delBtn = card.querySelector(".pdm-key-delete");

        revealBtn.addEventListener("click", () => {
            revealed = !revealed;
            if (revealed) {
                textSpan.textContent = cleanKey;
                textSpan.style.whiteSpace = "normal";
                textSpan.style.overflow = "visible";
                revealBtn.textContent = "🔒";
            } else {
                textSpan.textContent = maskedKey;
                textSpan.style.whiteSpace = "nowrap";
                textSpan.style.overflow = "hidden";
                revealBtn.textContent = "👁️";
            }
        });

        copyBtn.addEventListener("click", async () => {
            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    await navigator.clipboard.writeText(cleanKey);
                } else {
                    const ta = document.createElement("textarea");
                    ta.value = cleanKey;
                    document.body.appendChild(ta);
                    ta.select();
                    document.execCommand("copy");
                    document.body.removeChild(ta);
                }
                showToast("Ключ скопирован в буфер обмена", "success");
            } catch (err) {
                showToast("Не удалось скопировать ключ", "error");
            }
        });

        editBtn.addEventListener("click", async () => {
            const updated = prompt(`Редактировать API-ключ #${idx + 1}:`, cleanKey);
            if (updated !== null) {
                const cleanedUpdated = String(updated).trim().replace(/^["'\[]+|["'\]]+$/g, '');
                if (!cleanedUpdated) {
                    showToast("Ключ не может быть пустым", "error");
                    return;
                }
                currentDetailKeys[idx] = cleanedUpdated;
                renderProviderDetailKeys();
                await autoSaveProviderKeys();
                showToast("Ключ обновлен", "success");
            }
        });

        probeBtn.addEventListener("click", async () => {
            const origText = probeBtn.textContent;
            probeBtn.textContent = "Тест...";
            probeBtn.disabled = true;
            try {
                const res = await fetch(`/api/providers/${encodeURIComponent(currentDetailProviderId)}/probe`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ api_key: cleanKey })
                });
                const result = await res.json();
                
                let status = "invalid";
                if (result.success) {
                    status = "valid";
                    showToast(`Ключ #${idx + 1} валиден! (${result.latency_ms || 0} ms)`, "success");
                } else if (result.reason === "rate_limited" || result.status_code === 429) {
                    status = "rate_limited";
                    showToast(`Ключ #${idx + 1}: Лимит запросов исчерпан (429)`, "warning");
                } else {
                    status = "error";
                    showToast(`Ошибка ключа: ${result.error || result.detail || result.reason || 'Невалидный ключ'}`, "error");
                }

                const existingIdx = currentDetailKeysData.findIndex(kd => kd.key === cleanKey);
                const entry = {
                    key: cleanKey,
                    masked: cleanKey.length > 14 ? `${cleanKey.substring(0, 6)}••••••••${cleanKey.substring(cleanKey.length - 4)}` : cleanKey,
                    status: status,
                    reason: result.reason || result.error || null,
                    detail: result.detail || result.error || null,
                    latency_ms: result.latency_ms || 0
                };
                if (existingIdx >= 0) {
                    currentDetailKeysData[existingIdx] = entry;
                } else {
                    currentDetailKeysData.push(entry);
                }

                renderProviderDetailKeys();
                await refreshCurrentDetailProviderStatus();

            } catch (err) {
                showToast("Ошибка сетевого запроса проверки", "error");
                probeBtn.textContent = origText;
            } finally {
                probeBtn.disabled = false;
            }
        });

        delBtn.addEventListener("click", async () => {
            const removedKey = currentDetailKeys[idx];
            currentDetailKeys.splice(idx, 1);
            if (currentDetailKeysData) {
                currentDetailKeysData = currentDetailKeysData.filter(kd => kd.key !== removedKey);
            }
            renderProviderDetailKeys();
            await autoSaveProviderKeys();
            showToast("Ключ удален", "info");
        });

        container.appendChild(card);
    });
}

// Автоматическое сохранение API-ключей провайдера в базу при добавлении/удалении/изменении
async function autoSaveProviderKeys() {
    if (!currentDetailProviderId) return;
    try {
        const payload = {
            api_keys: currentDetailKeys
        };
        const res = await fetch(`/api/providers/${encodeURIComponent(currentDetailProviderId)}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        if (res.ok) {
            await refreshCurrentDetailProviderStatus();
        }
    } catch (err) {
        console.error("Auto-save provider keys error:", err);
    }
}

// Сохранение настроек провайдера из модалки
async function saveProviderDetailSettings() {
    if (!currentDetailProviderId) return;

    const saveBtn = document.getElementById("pdm-save-btn");
    const modal = document.getElementById("provider-detail-modal");
    if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.textContent = "Сохранение...";
    }

    try {
        const modelSelect = document.getElementById("pdm-model-select");
        const tempSlider = document.getElementById("pdm-temperature");
        const baseUrlInput = document.getElementById("pdm-base-url-input");
        const enabledToggle = document.getElementById("pdm-enabled-toggle");
        const primaryToggle = document.getElementById("pdm-primary-toggle");

        const payload = {
            active_model: modelSelect ? modelSelect.value : undefined,
            temperature: tempSlider ? parseFloat(tempSlider.value) : 0.2,
            base_url: baseUrlInput ? baseUrlInput.value.trim() : undefined,
            is_enabled: enabledToggle ? enabledToggle.checked : true,
            is_primary: primaryToggle ? primaryToggle.checked : false,
            api_keys: currentDetailKeys
        };

        const res = await fetch(`/api/providers/${encodeURIComponent(currentDetailProviderId)}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        if (res.ok) {
            if (payload.is_primary) {
                activeUnifiedProvider = currentDetailProviderId;
            }
            showToast("Настройки провайдера сохранены!", "success");
            if (modal) modal.classList.add("hide");

            await loadUnifiedProviders();
            renderProvidersCards();
            updateDashboardAiStatus();
        } else {
            const err = await res.json().catch(() => ({}));
            showToast(`Ошибка сохранения: ${err.detail || 'Не удалось сохранить'}`, "error");
        }
    } catch (e) {
        console.error("Error saving provider settings:", e);
        showToast("Сетевая ошибка при сохранении", "error");
    } finally {
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.textContent = "Сохранить настройки";
        }
    }
}

// Открытие модального окна добавления кастомного OpenAI провайдера
function openAddCustomProviderModal() {
    const modal = document.getElementById("add-custom-provider-modal");
    if (!modal) return;
    const nameIn = document.getElementById("acp-name-input");
    const urlIn = document.getElementById("acp-url-input");
    const modelIn = document.getElementById("acp-model-input");
    const keyIn = document.getElementById("acp-key-input");
    const tempIn = document.getElementById("acp-temp-input");
    const linkIn = document.getElementById("acp-link-input");

    if (nameIn) nameIn.value = "";
    if (urlIn) urlIn.value = "";
    if (modelIn) modelIn.value = "";
    if (keyIn) keyIn.value = "";
    if (tempIn) tempIn.value = "0.2";
    if (linkIn) linkIn.value = "";

    modal.classList.remove("hide");
}

// Сохранение нового кастомного OpenAI провайдера
async function saveCustomProvider() {
    const nameInput = document.getElementById("acp-name-input");
    const urlInput = document.getElementById("acp-url-input");
    const modelInput = document.getElementById("acp-model-input");
    const keyInput = document.getElementById("acp-key-input");
    const tempInput = document.getElementById("acp-temp-input");
    const linkInput = document.getElementById("acp-link-input");
    const saveBtn = document.getElementById("acp-save-btn");
    const modal = document.getElementById("add-custom-provider-modal");

    const name = nameInput ? nameInput.value.trim() : "";
    const baseUrl = urlInput ? urlInput.value.trim() : "";
    const defaultModel = modelInput ? modelInput.value.trim() : "";
    const initialKey = keyInput ? keyInput.value.trim() : "";
    const temp = tempInput ? parseFloat(tempInput.value) : 0.2;
    const getKeyUrl = linkInput ? linkInput.value.trim() : "";

    if (!name) {
        showToast("Введите название провайдера", "error");
        if (nameInput) nameInput.focus();
        return;
    }
    if (!baseUrl) {
        showToast("Введите Base URL эндпоинта", "error");
        if (urlInput) urlInput.focus();
        return;
    }
    if (!defaultModel) {
        showToast("Укажите имя модели по умолчанию", "error");
        if (modelInput) modelInput.focus();
        return;
    }

    if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.textContent = "Создание...";
    }

    try {
        const payload = {
            name: name,
            base_url: baseUrl,
            default_model: defaultModel,
            initial_key: initialKey || undefined,
            temperature: isNaN(temp) ? 0.2 : temp,
            get_key_url: getKeyUrl || undefined
        };

        const res = await fetch("/api/providers/custom", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        if (res.ok) {
            const data = await res.json();
            showToast(`Провайдер «${name}» успешно создан!`, "success");
            if (modal) modal.classList.add("hide");

            await loadUnifiedProviders();
            renderProvidersCards();

            // Открываем детальную настройку нового провайдера
            if (data.provider && data.provider.id) {
                openProviderDetailModal(data.provider.id);
            }
        } else {
            const err = await res.json().catch(() => ({}));
            showToast(`Ошибка создания: ${err.detail || 'Не удалось создать провайдер'}`, "error");
        }
    } catch (e) {
        console.error("Error creating custom provider:", e);
        showToast("Сетевая ошибка при создании провайдера", "error");
    } finally {
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.textContent = "Создать провайдер";
        }
    }
}

// Удаление кастомного провайдера
async function deleteCustomProvider(providerId) {
    if (!providerId) return;
    const provider = unifiedProvidersMap[providerId];
    const name = provider ? provider.name : providerId;

    const confirmed = await showConfirm(`Удалить кастомный провайдер «${name}» и все его настройки?`);
    if (!confirmed) return;

    try {
        const res = await fetch(`/api/providers/custom/${encodeURIComponent(providerId)}`, {
            method: "DELETE"
        });

        if (res.ok) {
            showToast(`Провайдер «${name}» удален`, "success");
            const modal = document.getElementById("provider-detail-modal");
            if (modal) modal.classList.add("hide");

            if (activeUnifiedProvider === providerId) {
                activeUnifiedProvider = "groq";
            }

            await loadUnifiedProviders();
            renderProvidersCards();
        } else {
            const err = await res.json().catch(() => ({}));
            showToast(`Ошибка удаления: ${err.detail || 'Не удалось удалить'}`, "error");
        }
    } catch (e) {
        console.error("Error deleting custom provider:", e);
        showToast("Сетевая ошибка при удалении", "error");
    }
}

// Отрисовка интерактивных чипов провайдеров в модальном окне настроек
function renderProviderChips(selectedProviderId) {
    const container = document.getElementById("sys-provider-chips");
    if (!container) return;
    container.innerHTML = "";

    const providers = Object.values(unifiedProvidersMap);
    if (providers.length === 0) return;

    providers.forEach(p => {
        const chip = document.createElement("div");
        chip.className = `provider-chip ${p.id === selectedProviderId ? 'active' : ''}`;
        chip.setAttribute("data-provider", p.id);
        chip.innerHTML = `
            <div class="provider-chip-title">
                <span>${p.badge || "⚡"}</span>
                <span>${escapeHtml(p.name)}</span>
            </div>
        `;
        chip.addEventListener("click", () => {
            selectUnifiedProvider(p.id);
        });
        container.appendChild(chip);
    });
}

// Выбор активного единого провайдера
async function selectUnifiedProvider(providerId, targetModelId = null, customBaseUrl = null) {
    activeUnifiedProvider = providerId || "groq";
    const provider = unifiedProvidersMap[activeUnifiedProvider];
    if (!provider) return;

    // Обновляем визуальный активный чип
    const chips = document.querySelectorAll("#sys-provider-chips .provider-chip");
    chips.forEach(c => {
        if (c.getAttribute("data-provider") === activeUnifiedProvider) {
            c.classList.add("active");
        } else {
            c.classList.remove("active");
        }
    });

    // Обновляем информационный баннер провайдера
    const bannerIcon = document.getElementById("sys-provider-banner-icon");
    const bannerTitle = document.getElementById("sys-provider-banner-title");
    const bannerDesc = document.getElementById("sys-provider-banner-desc");
    const getKeyLink = document.getElementById("sys-provider-get-key-link");
    const providerNameLabel = document.getElementById("sys-selected-provider-name");

    if (bannerIcon) bannerIcon.textContent = provider.badge || "⚡";
    if (bannerTitle) bannerTitle.textContent = provider.name;
    if (bannerDesc) bannerDesc.textContent = provider.description || "";
    if (providerNameLabel) providerNameLabel.textContent = provider.name;

    if (getKeyLink) {
        if (provider.get_key_url) {
            getKeyLink.style.display = "inline-flex";
            getKeyLink.href = provider.get_key_url;
            getKeyLink.innerHTML = `<span>🔗 Получить ключ ${provider.name}</span><span>↗</span>`;
        } else {
            getKeyLink.style.display = "none";
        }
    }

    // Base URL для Custom
    const urlContainer = document.getElementById("sys-provider-url-container");
    const baseUrlInput = document.getElementById("sys-provider-base-url");
    if (urlContainer) {
        urlContainer.style.display = activeUnifiedProvider === "custom" ? "block" : "none";
    }
    if (baseUrlInput && customBaseUrl !== null && customBaseUrl !== undefined) {
        baseUrlInput.value = customBaseUrl;
    }

    // Синхронизируем скрытые legacy контролы для совместимости
    const primarySelect = document.getElementById("sys-primary-provider");
    const presetSelect = document.getElementById("sys-openai-preset-select");
    const legacyBaseUrl = document.getElementById("sys-openai-base-url");

    if (primarySelect) {
        if (activeUnifiedProvider === "gemini") {
            primarySelect.value = "gemini";
        } else if (activeUnifiedProvider === "mistral") {
            primarySelect.value = "mistral";
        } else {
            primarySelect.value = "openai";
            if (presetSelect) presetSelect.value = activeUnifiedProvider;
        }
    }
    if (legacyBaseUrl && baseUrlInput) {
        legacyBaseUrl.value = baseUrlInput.value;
    }

    // Загружаем список моделей для этого провайдера в кастомный дропдаун
    await loadProviderModels(activeUnifiedProvider, targetModelId);
}

// Загрузка моделей провайдера из SQLite кэша / API
async function loadProviderModels(providerId, preferredModelId = null) {
    const displayNameEl = document.getElementById("custom-model-display-name");
    const badgesEl = document.getElementById("custom-model-badges");
    const optionsList = document.getElementById("custom-model-options-list");
    if (!optionsList) return;

    if (displayNameEl) displayNameEl.textContent = "Загрузка моделей...";
    if (badgesEl) badgesEl.innerHTML = "";

    try {
        const res = await fetch(`/api/models?provider=${encodeURIComponent(providerId)}&catalog=true`);
        if (res.ok) {
            const data = await res.json();
            currentProviderModels = data.catalog || [];
        } else {
            currentProviderModels = [];
        }
    } catch (e) {
        console.error("Error loading models for provider:", providerId, e);
        currentProviderModels = [];
    }

    // Если моделей в БД еще нет, формируем из дефолтных настроек провайдера
    if (currentProviderModels.length === 0) {
        const provider = unifiedProvidersMap[providerId];
        const defaultModels = (provider && provider.default_models) ? provider.default_models : [];
        currentProviderModels = defaultModels.map(m => ({
            provider: providerId,
            model_id: m,
            display_name: m,
            description: "",
            context_window: "",
            is_free: provider ? provider.is_free : false,
            is_default: provider ? (m === provider.default_model) : false
        }));
    }

    // Определяем активную модель
    let activeModel = null;
    if (preferredModelId) {
        activeModel = currentProviderModels.find(m => m.model_id === preferredModelId);
    }
    if (!activeModel) {
        activeModel = currentProviderModels.find(m => m.is_default) || currentProviderModels[0];
    }

    if (activeModel) {
        activeSelectedModelId = activeModel.model_id;
        if (displayNameEl) displayNameEl.textContent = activeModel.display_name || activeModel.model_id;
        if (badgesEl) {
            badgesEl.innerHTML = `
                ${activeModel.is_free ? '<span class="glass-badge badge-free">Free</span>' : ''}
                ${activeModel.context_window ? `<span class="glass-badge badge-ctx">${escapeHtml(activeModel.context_window)}</span>` : ''}
                ${activeModel.is_default ? '<span class="glass-badge badge-def">Default</span>' : ''}
            `;
        }
        syncLegacyModelSelects(providerId, activeSelectedModelId);
    } else {
        if (displayNameEl) displayNameEl.textContent = "Нет доступных моделей";
        if (badgesEl) badgesEl.innerHTML = "";
    }

    renderCustomModelOptions();
}

// Отрисовка опций в кастомном выпадающем списке моделей
function renderCustomModelOptions() {
    const optionsList = document.getElementById("custom-model-options-list");
    if (!optionsList) return;
    optionsList.innerHTML = "";

    const searchInput = document.getElementById("custom-model-search-input");
    const searchVal = searchInput ? searchInput.value.toLowerCase().trim() : "";

    const filtered = currentProviderModels.filter(m => {
        if (currentFreeOnlyFilter && !m.is_free) return false;
        if (searchVal) {
            const str = `${m.model_id} ${m.display_name || ''} ${m.description || ''}`.toLowerCase();
            return str.includes(searchVal);
        }
        return true;
    });

    if (filtered.length === 0) {
        optionsList.innerHTML = `
            <div style="padding: 16px; text-align: center; color: var(--text-secondary); font-size: 12px;">
                Модели не найдены. Нажмите «Обновить из API» ниже.
            </div>
        `;
        return;
    }

    filtered.forEach(m => {
        const isSelected = m.model_id === activeSelectedModelId;
        const opt = document.createElement("div");
        opt.className = `glass-select-option ${isSelected ? 'selected' : ''}`;
        opt.innerHTML = `
            <div class="option-name-box">
                <span class="option-title">${escapeHtml(m.display_name || m.model_id)}</span>
                ${m.description ? `<span class="option-desc">${escapeHtml(m.description)}</span>` : ''}
            </div>
            <div class="option-badges-box">
                ${m.is_free ? '<span class="glass-badge badge-free">Free</span>' : ''}
                ${m.context_window ? `<span class="glass-badge badge-ctx">${escapeHtml(m.context_window)}</span>` : ''}
                ${m.is_default ? '<span class="glass-badge badge-def">Default</span>' : ''}
            </div>
        `;
        opt.addEventListener("click", () => {
            selectActiveModelInDropdown(m);
        });
        optionsList.appendChild(opt);
    });
}

// Фильтрация опций кастомного селекта
function filterCustomModelOptions(searchVal) {
    renderCustomModelOptions();
}

// Выбор модели в кастомном выпадающем списке
function selectActiveModelInDropdown(model) {
    activeSelectedModelId = model.model_id;
    const displayNameEl = document.getElementById("custom-model-display-name");
    const badgesEl = document.getElementById("custom-model-badges");
    const dropdown = document.getElementById("custom-model-dropdown");

    if (displayNameEl) displayNameEl.textContent = model.display_name || model.model_id;
    if (badgesEl) {
        badgesEl.innerHTML = `
            ${model.is_free ? '<span class="glass-badge badge-free">Free</span>' : ''}
            ${model.context_window ? `<span class="glass-badge badge-ctx">${escapeHtml(model.context_window)}</span>` : ''}
            ${model.is_default ? '<span class="glass-badge badge-def">Default</span>' : ''}
        `;
    }

    if (dropdown) dropdown.classList.add("hide");
    syncLegacyModelSelects(model.provider || activeUnifiedProvider, model.model_id);
    renderCustomModelOptions();
}

// Синхронизация скрытых нативных select-элементов для обратной совместимости
function syncLegacyModelSelects(provider, modelId) {
    const sysGeminiSelect = document.getElementById("sys-gemini-model-select");
    const sysMistralSelect = document.getElementById("sys-mistral-model-select");
    const sysOpenaiModelSelect = document.getElementById("sys-openai-model-select");

    if (provider === "gemini" && sysGeminiSelect) {
        sysGeminiSelect.value = modelId;
    } else if (provider === "mistral" && sysMistralSelect) {
        sysMistralSelect.value = modelId;
    } else if (sysOpenaiModelSelect) {
        sysOpenaiModelSelect.value = modelId;
    }
}

// Запуск динамической синхронизации моделей через API провайдера
async function syncProviderModels(provider = "all") {
    const syncIcons = [
        document.getElementById("sync-models-icon"),
        document.getElementById("dropdown-sync-btn") ? document.getElementById("dropdown-sync-btn").querySelector(".spin-icon") : null,
        document.getElementById("catalog-sync-icon")
    ].filter(Boolean);

    const btnTexts = [
        document.getElementById("sync-models-btn-text"),
        document.getElementById("catalog-sync-btn-text")
    ].filter(Boolean);

    syncIcons.forEach(icon => icon.classList.add("spinning"));
    btnTexts.forEach(btn => {
        btn.dataset.orig = btn.textContent;
        btn.textContent = "Синхронизация...";
    });

    try {
        const response = await fetch("/api/models/sync", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ provider: provider || "all" })
        });

        if (response.ok) {
            const data = await response.json();
            const total = data.total || 0;
            showToast(`Синхронизировано моделей: ${total}`, "success");
            
            // Перезагружаем модели для текущего активного провайдера
            await loadProviderModels(activeUnifiedProvider, activeSelectedModelId);

            // Если открыт каталог, обновляем и его
            const catalogModal = document.getElementById("models-catalog-modal");
            if (catalogModal && !catalogModal.classList.contains("hide")) {
                await loadAndRenderCatalog(currentCatalogProviderFilter);
            }
        } else {
            const err = await response.json().catch(() => ({}));
            showToast(`Ошибка синхронизации: ${err.detail || 'Не удалось получить модели'}`, "error");
        }
    } catch (e) {
        console.error("Error syncing models:", e);
        showToast("Сетевая ошибка при синхронизации моделей с API", "error");
    } finally {
        syncIcons.forEach(icon => icon.classList.remove("spinning"));
        btnTexts.forEach(btn => {
            if (btn.dataset.orig) btn.textContent = btn.dataset.orig;
        });
    }
}

// ----------------------------------------------------
// Модальное окно: Каталог моделей LLM
// ----------------------------------------------------

async function openModelsCatalogModal(providerFilter = "all") {
    const modal = document.getElementById("models-catalog-modal");
    if (!modal) return;
    modal.classList.remove("hide");
    await loadAndRenderCatalog(providerFilter);
}

async function loadAndRenderCatalog(providerFilter = "all") {
    currentCatalogProviderFilter = providerFilter || "all";
    const grid = document.getElementById("models-catalog-grid");
    if (grid) grid.innerHTML = '<div style="grid-column: 1 / -1; padding: 24px; text-align: center; color: var(--text-secondary);">Загрузка каталога моделей...</div>';

    try {
        const res = await fetch("/api/models?catalog=true");
        if (res.ok) {
            const data = await res.json();
            cachedCatalogModels = data.catalog || [];
        }
    } catch (e) {
        console.error("Error loading catalog models:", e);
    }

    renderCatalogTabs();
    renderCatalogCards();
}

function renderCatalogTabs() {
    const container = document.getElementById("catalog-provider-tabs");
    if (!container) return;
    container.innerHTML = "";

    const tabs = [
        { id: "all", name: "Все провайдеры", badge: "📋" },
        ...Object.values(unifiedProvidersMap)
    ];

    tabs.forEach(t => {
        const btn = document.createElement("button");
        btn.type = "button";
        const isActive = t.id === currentCatalogProviderFilter;
        btn.className = `btn ${isActive ? 'btn-primary' : 'btn-secondary'} btn-xs`;
        btn.style.cssText = "font-size: 11px; padding: 4px 10px; display: flex; align-items: center; gap: 4px;";
        btn.innerHTML = `<span>${t.badge || "⚡"}</span><span>${escapeHtml(t.name)}</span>`;
        btn.addEventListener("click", () => {
            currentCatalogProviderFilter = t.id;
            renderCatalogTabs();
            renderCatalogCards();
        });
        container.appendChild(btn);
    });
}

function renderCatalogCards() {
    const grid = document.getElementById("models-catalog-grid");
    const statsInfo = document.getElementById("catalog-stats-info");
    const searchInput = document.getElementById("catalog-search-input");
    if (!grid) return;

    const searchVal = searchInput ? searchInput.value.toLowerCase().trim() : "";

    const filtered = cachedCatalogModels.filter(m => {
        if (currentCatalogProviderFilter !== "all" && m.provider !== currentCatalogProviderFilter) {
            return false;
        }
        if (searchVal) {
            const text = `${m.provider} ${m.model_id} ${m.display_name || ''} ${m.description || ''}`.toLowerCase();
            return text.includes(searchVal);
        }
        return true;
    });

    if (statsInfo) {
        statsInfo.textContent = `Показано: ${filtered.length} из ${cachedCatalogModels.length} моделей`;
    }

    if (filtered.length === 0) {
        grid.innerHTML = `
            <div style="grid-column: 1 / -1; padding: 32px; text-align: center; color: var(--text-secondary); border: 1px dashed rgba(255,255,255,0.1); border-radius: 10px;">
                По заданным критериям модели не найдены.
            </div>
        `;
        return;
    }

    grid.innerHTML = "";
    filtered.forEach(m => {
        const providerInfo = unifiedProvidersMap[m.provider] || { name: m.provider, badge: "⚡" };
        const isActive = (m.provider === activeUnifiedProvider && m.model_id === activeSelectedModelId) || m.is_default;
        
        const card = document.createElement("div");
        card.className = `model-catalog-card ${isActive ? 'active' : ''}`;
        card.innerHTML = `
            <div>
                <div class="model-card-header">
                    <div style="display: flex; align-items: center; gap: 6px;">
                        <span style="font-size: 14px;">${providerInfo.badge || "⚡"}</span>
                        <span style="font-size: 11px; color: var(--text-secondary); font-weight: 600; text-transform: uppercase;">${escapeHtml(providerInfo.name)}</span>
                    </div>
                    <div style="display: flex; gap: 4px;">
                        ${m.is_free ? '<span class="glass-badge badge-free">Free</span>' : ''}
                        ${m.context_window ? `<span class="glass-badge badge-ctx">${escapeHtml(m.context_window)}</span>` : ''}
                    </div>
                </div>
                <div class="model-card-name" style="margin-top: 6px;">${escapeHtml(m.display_name || m.model_id)}</div>
                <div style="font-size: 10.5px; font-family: monospace; color: #94a3b8; margin-top: 2px;">${escapeHtml(m.model_id)}</div>
                <div class="model-card-desc" style="margin-top: 6px;">${escapeHtml(m.description || 'Модель провайдера')}</div>
            </div>
            <div class="model-card-footer">
                <div style="font-size: 10px; color: var(--text-secondary);">
                    ${m.last_synced ? `Синхр: ${escapeHtml(m.last_synced.split('T')[0] || '')}` : ''}
                </div>
                <button type="button" class="btn ${isActive ? 'btn-secondary' : 'btn-primary'} btn-xs" style="padding: 4px 12px; font-size: 11px;">
                    ${isActive ? '✓ Активна' : 'Выбрать'}
                </button>
            </div>
        `;

        const actionBtn = card.querySelector("button");
        if (!isActive && actionBtn) {
            actionBtn.addEventListener("click", async () => {
                await selectModelFromCatalog(m.provider, m.model_id);
            });
        }
        grid.appendChild(card);
    });
}

async function selectModelFromCatalog(providerId, modelId) {
    try {
        const res = await fetch("/api/models/select", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ provider: providerId, model_id: modelId })
        });

        if (res.ok) {
            showToast(`Модель ${modelId} выбрана активной!`, "success");
            // Если провайдер совпадает с выбранным в настройках, обновляем дропдаун
            if (providerId === activeUnifiedProvider) {
                activeSelectedModelId = modelId;
                await loadProviderModels(providerId, modelId);
            }
            // Перерисовываем карточки каталога
            await loadAndRenderCatalog(currentCatalogProviderFilter);
        } else {
            showToast("Не удалось активировать модель", "error");
        }
    } catch (e) {
        console.error("Error selecting model:", e);
        showToast("Ошибка при выборе модели", "error");
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
    const selectedMistralModel = mistralModelSelect ? mistralModelSelect.value : (userSettings.mistral_model || "open-mistral-nemo");

    const openaiPresetSelect = document.getElementById("sys-openai-preset-select");
    const openaiModelSelect = document.getElementById("sys-openai-model-select");
    const openaiBaseUrlInput = document.getElementById("sys-openai-base-url");

    const modalAppsInput = document.getElementById("modal-limit-apps-input");
    const modalProcInput = document.getElementById("modal-limit-proc-input");

    const payload = {
        queries: queries,
        area_id: document.getElementById("area-select").value,
        threshold: parseInt(document.getElementById("threshold-range").value, 10),
        resume_id: document.getElementById("resume-select").value,
        dry_run: document.getElementById("dryrun-toggle").checked,
        gemini_api_keys: geminiKeys,
        gemini_model: selectedModel,
        mistral_api_keys: mistralKeys,
        mistral_model: selectedMistralModel,
        openai_api_keys: currentOpenaiKeys.join(","),
        openai_provider_preset: openaiPresetSelect ? openaiPresetSelect.value : (userSettings.openai_provider_preset || "groq"),
        openai_base_url: openaiBaseUrlInput ? openaiBaseUrlInput.value.trim() : (userSettings.openai_base_url || "https://api.groq.com/openai/v1"),
        openai_model: openaiModelSelect ? openaiModelSelect.value : (userSettings.openai_model || "llama-3.3-70b-versatile"),
        stop_condition: currentSelectedLimitMode || userSettings.stop_condition || "both",
        limit_applications: modalAppsInput ? (parseInt(modalAppsInput.value, 10) || 10) : (userSettings.limit_applications || 10),
        limit_processed: modalProcInput ? (parseInt(modalProcInput.value, 10) || 20) : (userSettings.limit_processed || 20)
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
            updateLimitsModalUIFromSettings();
        }
    } catch (e) {
        console.error("Error saving settings:", e);
    }
}

// ==========================================================================
// Логика модального окна лимитов и автоостановки (Dedicated Limits Modal)
// ==========================================================================
let currentSelectedLimitMode = "both";

function initLimitsModalListeners() {
    const openLimitsBtn = document.getElementById("sys-open-limits-modal-btn");
    const limitsModal = document.getElementById("limits-settings-modal");
    const closeLimitsBtn = document.getElementById("limits-settings-close-btn");
    const cancelLimitsBtn = document.getElementById("limits-settings-cancel-btn");
    const saveLimitsBtn = document.getElementById("limits-settings-save-btn");

    const modeCards = document.querySelectorAll(".limit-mode-card");
    const appsInput = document.getElementById("modal-limit-apps-input");
    const procInput = document.getElementById("modal-limit-proc-input");
    const presetPills = document.querySelectorAll(".preset-pill");

    // Открытие окна
    if (openLimitsBtn && limitsModal) {
        openLimitsBtn.addEventListener("click", () => {
            updateLimitsModalUIFromSettings();
            limitsModal.classList.remove("hide");
        });
    }

    // Закрытие окна
    const closeLimits = () => {
        if (limitsModal) limitsModal.classList.add("hide");
    };
    if (closeLimitsBtn) closeLimitsBtn.addEventListener("click", closeLimits);
    if (cancelLimitsBtn) cancelLimitsBtn.addEventListener("click", closeLimits);

    // Клик по карточкам режима
    modeCards.forEach(card => {
        card.addEventListener("click", () => {
            const mode = card.dataset.mode;
            setLimitMode(mode, true);
        });
    });

    // Изменение значений в инпутах
    if (appsInput) {
        appsInput.addEventListener("input", () => {
            syncPresetPillActiveState("modal-limit-apps-input", appsInput.value);
            updateLimitsSummaryText();
        });
    }
    if (procInput) {
        procInput.addEventListener("input", () => {
            syncPresetPillActiveState("modal-limit-proc-input", procInput.value);
            updateLimitsSummaryText();
        });
    }

    // Быстрые пресеты (5, 10, 20, 25, 50, 100)
    presetPills.forEach(pill => {
        pill.addEventListener("click", (e) => {
            e.preventDefault();
            const targetId = pill.dataset.target;
            const val = pill.dataset.val;
            const targetInput = document.getElementById(targetId);
            if (targetInput) {
                targetInput.value = val;
                syncPresetPillActiveState(targetId, val);
                updateLimitsSummaryText();
            }
        });
    });

    // Сохранение из модального окна лимитов
    if (saveLimitsBtn) {
        saveLimitsBtn.addEventListener("click", async () => {
            await saveLimitsSettingsFromModal();
        });
    }
}

function setLimitMode(mode, animated = true) {
    currentSelectedLimitMode = mode;
    const modeCards = document.querySelectorAll(".limit-mode-card");
    modeCards.forEach(c => {
        if (c.dataset.mode === mode) {
            c.classList.add("active");
        } else {
            c.classList.remove("active");
        }
    });

    const appsWrapper = document.getElementById("limit-applications-wrapper");
    const procWrapper = document.getElementById("limit-processed-wrapper");

    if (mode === "applications") {
        if (appsWrapper) appsWrapper.classList.remove("collapsed");
        if (procWrapper) procWrapper.classList.add("collapsed");
    } else if (mode === "processed") {
        if (appsWrapper) appsWrapper.classList.add("collapsed");
        if (procWrapper) procWrapper.classList.remove("collapsed");
    } else {
        // both
        if (appsWrapper) appsWrapper.classList.remove("collapsed");
        if (procWrapper) procWrapper.classList.remove("collapsed");
    }

    updateLimitsSummaryText();
}

function updateLimitsSummaryText() {
    const summaryText = document.getElementById("modal-limit-summary-text");
    const appsInput = document.getElementById("modal-limit-apps-input");
    const procInput = document.getElementById("modal-limit-proc-input");
    const badge = document.getElementById("sys-limits-badge");

    const nApps = appsInput ? (parseInt(appsInput.value, 10) || 10) : 10;
    const nProc = procInput ? (parseInt(procInput.value, 10) || 20) : 20;

    let text = "";
    let badgeText = "";

    if (currentSelectedLimitMode === "applications") {
        text = `🛑 Пайплайн автоматически остановится строго после ${nApps} отправленных откликов (независимо от общего числа просмотренных вакансий).`;
        badgeText = `Отклики: ${nApps}`;
    } else if (currentSelectedLimitMode === "processed") {
        text = `🛑 Пайплайн автоматически остановится после оценки ${nProc} вакансий моделью ИИ (независимо от количества подходящих откликов).`;
        badgeText = `Оценки: ${nProc}`;
    } else {
        text = `🛑 Пайплайн остановится после ${nApps} отправленных откликов или после анализа ${nProc} вакансий (что из этого наступит раньше).`;
        badgeText = `Оба: ${nApps}/${nProc}`;
    }

    if (summaryText) summaryText.textContent = text;
    if (badge) badge.textContent = badgeText;
}

function syncPresetPillActiveState(targetId, currentVal) {
    const pills = document.querySelectorAll(`.preset-pill[data-target="${targetId}"]`);
    pills.forEach(p => {
        if (String(p.dataset.val) === String(currentVal)) {
            p.classList.add("active");
        } else {
            p.classList.remove("active");
        }
    });
}

function updateLimitsModalUIFromSettings() {
    const mode = userSettings.stop_condition || "both";
    currentSelectedLimitMode = mode;

    const appsInput = document.getElementById("modal-limit-apps-input");
    const procInput = document.getElementById("modal-limit-proc-input");

    const nApps = userSettings.limit_applications !== undefined ? userSettings.limit_applications : 10;
    const nProc = userSettings.limit_processed !== undefined ? userSettings.limit_processed : 20;

    if (appsInput) {
        appsInput.value = nApps;
        syncPresetPillActiveState("modal-limit-apps-input", nApps);
    }
    if (procInput) {
        procInput.value = nProc;
        syncPresetPillActiveState("modal-limit-proc-input", nProc);
    }

    setLimitMode(mode, false);
    updateLimitsSummaryText();
}

async function saveLimitsSettingsFromModal() {
    const saveBtn = document.getElementById("limits-settings-save-btn");
    const limitsModal = document.getElementById("limits-settings-modal");
    const appsInput = document.getElementById("modal-limit-apps-input");
    const procInput = document.getElementById("modal-limit-proc-input");

    const nApps = appsInput ? (parseInt(appsInput.value, 10) || 10) : 10;
    const nProc = procInput ? (parseInt(procInput.value, 10) || 20) : 20;

    const updatedSettings = {
        ...userSettings,
        stop_condition: currentSelectedLimitMode,
        limit_applications: nApps,
        limit_processed: nProc
    };

    if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.textContent = "Сохранение...";
    }

    try {
        const response = await fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(updatedSettings)
        });

        if (response.ok) {
            userSettings = updatedSettings;
            updateLimitsModalUIFromSettings();
            showToast("✅ Параметры автоостановки успешно сохранены!", "success");
            if (limitsModal) limitsModal.classList.add("hide");
        } else {
            showToast("Ошибка сохранения лимитов", "error");
        }
    } catch (e) {
        console.error("Error saving limits settings:", e);
        showToast("Сетевая ошибка при сохранении лимитов", "error");
    } finally {
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.textContent = "Сохранить лимиты";
        }
    }
}

// Запуск сканирования
async function startScanning() {
    const btn = document.getElementById("start-scan-btn");
    if (btn && btn.hasAttribute("disabled")) {
        const reason = btn.getAttribute("title") || "Запуск анализа недоступен: проверьте авторизацию на hh.ru и статус AI провайдеров.";
        showToast(`⚠️ ${reason}`, "warning");
        return;
    }
    if (isPolling) return;
    
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
            await loadJobs(false, false, true);
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

function hasJobsChanged(oldList, newList, append) {
    if (append) return true;
    if (!oldList || !newList) return true;
    if (oldList.length !== newList.length) return true;
    for (let i = 0; i < newList.length; i++) {
        if (oldList[i]?.id !== newList[i]?.id || 
            oldList[i]?.status !== newList[i]?.status || 
            oldList[i]?.match_score !== newList[i]?.match_score) {
            return true;
        }
    }
    return false;
}

// Загрузка обработанных вакансий из БД (с пагинацией и защитой от race conditions)
async function loadJobs(reset = false, append = false, isSilent = false) {
    if (reset) {
        currentOffset = 0;
        currentJobs = [];
    }
    
    const fetchId = ++currentFetchId;
    const requestedFilter = currentFilter;
    const requestedOffset = currentOffset;
    
    try {
        if (!isSilent && !append) {
            setStatsLoading(true);
        }
        const response = await fetch(`/api/jobs?status=${requestedFilter}&limit=${itemsPerPage}&offset=${requestedOffset}`);
        if (!response.ok) {
            if (fetchId === currentFetchId && !isSilent) {
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
        const changed = hasJobsChanged(currentJobs, newJobs, append);
        
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
        
        if (!isSilent) {
            setStatsLoading(false);
        }
        renderStatsDom(data.stats);
        
        if (changed || reset) {
            renderJobsList(append);
        }
        
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
        if (fetchId === currentFetchId && !isSilent) {
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
    const fragment = document.createDocumentFragment();
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
        fragment.appendChild(header);
        
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
                const genBtnHtml = (job.status === "ignored" && !job.cover_letter)
                    ? `<button class="btn btn-secondary" style="font-size: 11px; padding: 4px 8px; border-radius: var(--radius-pill); border-color: rgba(168, 85, 247, 0.4); background: rgba(168, 85, 247, 0.15); color: #d8b4fe; white-space: nowrap;" onclick="event.stopPropagation(); window.handleGenerateLetterQuick('${job.id}')" title="Сгенерировать сопроводительное письмо с ИИ">✨ Письмо</button>`
                    : '';
                quickBtnHtml = `${genBtnHtml}<button class="btn btn-secondary" style="font-size: 11px; padding: 4px 10px; border-radius: var(--radius-pill); border-color: rgba(99, 102, 241, 0.4); background: rgba(99, 102, 241, 0.15); color: #c7d2fe; white-space: nowrap;" onclick="event.stopPropagation(); window.handleQuickApply('${job.id}')" title="Сгенерировать письмо, ответить на вопросы и отправить">⚡ ИИ-отклик</button>`;
            }
            
            const resumeBadgeHtml = job.applied_resume_title 
                ? `<span class="resume-badge" style="font-size: 11px; background: rgba(56, 189, 248, 0.12); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.25); border-radius: var(--radius-pill); padding: 2px 7px; font-weight: 500; display: inline-flex; align-items: center; gap: 4px; white-space: nowrap;" title="Резюме, выбранное для этого отклика">📄 ${escapeHtml(job.applied_resume_title)}</span>` 
                : '';

            const modelBadgeHtml = (job.analyzed_by_provider || job.analyzed_by_model)
                ? `<span class="model-badge" style="font-size: 11px; background: rgba(168, 85, 247, 0.12); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.25); border-radius: var(--radius-pill); padding: 2px 7px; font-weight: 500; display: inline-flex; align-items: center; gap: 4px; white-space: nowrap;" title="Модель и провайдер анализа">🤖 ${escapeHtml(job.analyzed_by_provider ? job.analyzed_by_provider + (job.analyzed_by_model ? ' (' + job.analyzed_by_model + ')' : '') : job.analyzed_by_model)}</span>`
                : '';

            let scoreTooltip = `${job.match_score}% Match`;
            if (job.scores_data) {
                try {
                    const s = typeof job.scores_data === "string" ? JSON.parse(job.scores_data) : job.scores_data;
                    if (s) {
                        const blocker = s.has_hard_blocker ? `\n⛔ Блокер: ${s.blocker_reason || 'Есть'}` : '';
                        scoreTooltip = `Стек: ${s.stack_score || 0}/30 • Опыт: ${s.experience_score || 0}/25 • Грейд: ${s.grade_score || 0}/20 • Домен: ${s.domain_score || 0}/15 • Формат: ${s.format_score || 0}/10 = ${job.match_score}%${blocker}`;
                    }
                } catch (e) {}
            }

            card.innerHTML = `
                <div style="display: flex; align-items: center; gap: 14px; flex: 1; min-width: 0;">
                    ${logoHtml}
                    <div class="v-info" style="flex: 1; min-width: 0;">
                        <h4 class="v-title" style="margin-bottom: 3px; font-weight: 600; line-height: 1.3; color: white;">${escapeHtml(job.title)}</h4>
                        <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                            <span class="v-company-salary" style="font-size: 13px; color: var(--text-secondary);">${escapeHtml(job.company)}</span>
                            ${resumeBadgeHtml}
                            ${modelBadgeHtml}
                            <a href="https://hh.ru/vacancy/${job.id}" target="_blank" class="v-link" onclick="event.stopPropagation()">Открыть на hh.ru ↗</a>
                        </div>
                    </div>
                </div>
                <div class="v-actions" style="display: flex; align-items: center; gap: 8px; flex-shrink: 0;">
                    ${quickBtnHtml}
                    <span class="score-badge ${scoreClass}" title="${escapeHtml(scoreTooltip)}">${job.match_score}% Match</span>
                    <span class="status-badge status-${job.status}">${statusLabel}</span>
                </div>
            `;
            
            card.addEventListener("click", () => openModal(job));
            fragment.appendChild(card);
        });
    });
    container.appendChild(fragment);
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
    let metaText = job.company;
    if (job.applied_resume_title) metaText += ` • 📄 Резюме: ${job.applied_resume_title}`;
    if (job.analyzed_by_provider || job.analyzed_by_model) {
        const provStr = job.analyzed_by_provider ? `${job.analyzed_by_provider} (${job.analyzed_by_model || ''})` : job.analyzed_by_model;
        metaText += ` • 🤖 Анализ: ${provStr}`;
    }
    document.getElementById("modal-vacancy-meta").textContent = metaText;
    document.getElementById("modal-vacancy-link").setAttribute("href", `https://hh.ru/vacancy/${job.id}`);
    document.getElementById("modal-reasoning-text").textContent = job.reasoning || "Обоснование отсутствует.";
    
    const textarea = document.getElementById("modal-cover-letter-input");
    textarea.value = job.cover_letter || "";
    
    document.getElementById("modal-score-val").textContent = `${job.match_score}%`;
    const circle = document.getElementById("modal-progress-bar");
    const strokeOffset = 220 - (220 * job.match_score) / 100;
    circle.style.strokeDashoffset = strokeOffset;

    // Отображение 5 шкал соответствия (Multifactor Breakdown)
    const breakdownCard = document.getElementById("modal-scores-breakdown-card");
    const totalPill = document.getElementById("modal-scores-total-pill");
    const blockerAlert = document.getElementById("modal-blocker-alert");
    const blockerText = document.getElementById("modal-blocker-text");

    let scores = null;
    if (job.scores_data) {
        try {
            scores = typeof job.scores_data === "string" ? JSON.parse(job.scores_data) : job.scores_data;
        } catch (e) {
            console.error("Error parsing scores_data:", e);
        }
    }

    if (scores && typeof scores === "object") {
        if (breakdownCard) breakdownCard.classList.remove("hide");
        if (totalPill) totalPill.textContent = `${job.match_score} / 100`;

        const stack = scores.stack_score !== undefined ? scores.stack_score : 0;
        const exp = scores.experience_score !== undefined ? scores.experience_score : 0;
        const grade = scores.grade_score !== undefined ? scores.grade_score : 0;
        const domain = scores.domain_score !== undefined ? scores.domain_score : 0;
        const format = scores.format_score !== undefined ? scores.format_score : 0;

        const setScale = (valId, fillId, val, max) => {
            const valEl = document.getElementById(valId);
            const fillEl = document.getElementById(fillId);
            if (valEl) valEl.textContent = `${val}/${max}`;
            if (fillEl) fillEl.style.width = `${Math.min(100, Math.round((val / max) * 100))}%`;
        };

        setScale("scale-stack-val", "scale-stack-fill", stack, 30);
        setScale("scale-exp-val", "scale-exp-fill", exp, 25);
        setScale("scale-grade-val", "scale-grade-fill", grade, 20);
        setScale("scale-domain-val", "scale-domain-fill", domain, 15);
        setScale("scale-format-val", "scale-format-fill", format, 10);

        if (scores.has_hard_blocker && scores.blocker_reason) {
            if (blockerAlert) blockerAlert.classList.remove("hide");
            if (blockerText) blockerText.textContent = `Блокирующий фактор: ${scores.blocker_reason}`;
        } else {
            if (blockerAlert) blockerAlert.classList.add("hide");
        }
    } else {
        if (breakdownCard) breakdownCard.classList.add("hide");
        if (blockerAlert) blockerAlert.classList.add("hide");
    }
    
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
    
    const genLetterBtn = document.getElementById("modal-generate-letter-btn");
    const genLetterText = document.getElementById("modal-generate-letter-text");
    const genLetterIcon = document.getElementById("modal-generate-letter-icon");
    const letterHint = document.getElementById("modal-letter-hint");

    const updateLetterBtnState = () => {
        if (!genLetterBtn) return;
        if (job.status === "applied" || job.status === "already_applied") {
            genLetterBtn.classList.add("hide");
        } else {
            genLetterBtn.classList.remove("hide");
            genLetterBtn.removeAttribute("disabled");
            if (genLetterIcon) genLetterIcon.textContent = "✨";
            if (genLetterText) {
                genLetterText.textContent = textarea.value.trim() ? "Перегенерировать с ИИ" : "Сгенерировать письмо с ИИ";
            }
        }
        if (letterHint) {
            if (job.status === "ignored" && !textarea.value.trim()) {
                letterHint.classList.remove("hide");
            } else {
                letterHint.classList.add("hide");
            }
        }
    };
    updateLetterBtnState();
    textarea.oninput = updateLetterBtnState;
    textarea.onblur = async () => {
        const text = textarea.value.trim();
        if (job.status !== "applied" && job.status !== "already_applied") {
            try {
                await fetch(`/api/vacancies/${job.id}/save-draft`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ cover_letter: text })
                });
                job.cover_letter = text;
            } catch (e) {
                console.error("Auto-save draft error:", e);
            }
        }
    };

    if (genLetterBtn) {
        genLetterBtn.onclick = async () => {
            await doGenerateCoverLetter(job, textarea, genLetterBtn, genLetterText, genLetterIcon, letterHint);
        };
    }
    
    const defaultApplyText = "Откликнуться";
    
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
        let coverLetter = textarea.value.trim();
        if (!coverLetter) {
            showToast("Сопроводительное письмо отсутствует. Запускаем генерацию с ИИ...", "info");
            await doGenerateCoverLetter(job, textarea, genLetterBtn, genLetterText, genLetterIcon, letterHint);
            coverLetter = textarea.value.trim();
            if (!coverLetter) {
                showToast("Пожалуйста, напишите сопроводительное письмо перед отправкой.", "error");
                return;
            }
            showToast("Письмо сгенерировано! Проверьте текст и нажмите «Откликнуться сейчас».", "info");
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
    const tabOpenaiBtn = document.getElementById("tab-openai-btn");
    const tabGeminiBtn = document.getElementById("tab-gemini-btn");
    const tabMistralBtn = document.getElementById("tab-mistral-btn");
    const addKeyLabel = document.getElementById("add-key-label");
    const newKeyInput = document.getElementById("new-key-input");
    const keysPoolTitle = document.getElementById("keys-pool-title");
    const vendorBanner = document.getElementById("key-vendor-banner");
    const vendorDesc = document.getElementById("key-vendor-desc");
    const vendorLink = document.getElementById("key-vendor-link");

    // Сброс стилей всех табов
    [tabOpenaiBtn, tabGeminiBtn, tabMistralBtn].forEach(btn => {
        if (btn) {
            btn.style.background = "rgba(255, 255, 255, 0.05)";
            btn.style.color = "var(--text-secondary)";
            btn.style.borderColor = "rgba(255, 255, 255, 0.1)";
        }
    });

    if (tab === "openai") {
        if (tabOpenaiBtn) {
            tabOpenaiBtn.style.background = "rgba(59, 130, 246, 0.2)";
            tabOpenaiBtn.style.color = "#60a5fa";
            tabOpenaiBtn.style.borderColor = "rgba(59, 130, 246, 0.4)";
        }
        const activePreset = openaiPresetsMap[userSettings.openai_provider_preset || "groq"];
        if (activePreset) {
            if (addKeyLabel) addKeyLabel.textContent = `Добавить ключ ${activePreset.name} API`;
            if (newKeyInput) newKeyInput.placeholder = activePreset.key_prefix_hint ? `Вставьте ключ (${activePreset.key_prefix_hint})` : "Вставьте API ключ";
            if (keysPoolTitle) keysPoolTitle.textContent = `Пул ключей ${activePreset.name}`;
            if (vendorBanner) vendorBanner.style.display = "flex";
            if (vendorDesc) vendorDesc.textContent = `${activePreset.badge || "⚡"} ${activePreset.name}: ${activePreset.description || "Бесплатные лимиты"}`;
            if (vendorLink) {
                if (activePreset.get_key_url) {
                    vendorLink.style.display = "inline-flex";
                    vendorLink.href = activePreset.get_key_url;
                    vendorLink.innerHTML = `<span>Получить ключ ${activePreset.name}</span><span>↗</span>`;
                } else {
                    vendorLink.style.display = "none";
                }
            }
        } else {
            if (addKeyLabel) addKeyLabel.textContent = "Добавить ключ OpenAI / Groq / OpenRouter API";
            if (newKeyInput) newKeyInput.placeholder = "Вставьте ключ (например: gsk_... для Groq, sk-or-... для OpenRouter)";
            if (keysPoolTitle) keysPoolTitle.textContent = "Пул ключей OpenAI / Groq";
            if (vendorBanner) vendorBanner.style.display = "flex";
            if (vendorDesc) vendorDesc.textContent = "⚡ Groq: сверхбыстрый LPU инференс Llama 3.3 70B.";
            if (vendorLink) {
                vendorLink.style.display = "inline-flex";
                vendorLink.href = "https://console.groq.com/keys";
                vendorLink.innerHTML = "<span>Получить ключ Groq</span><span>↗</span>";
            }
        }
    } else if (tab === "gemini") {
        if (tabGeminiBtn) {
            tabGeminiBtn.style.background = "rgba(59, 130, 246, 0.2)";
            tabGeminiBtn.style.color = "#60a5fa";
            tabGeminiBtn.style.borderColor = "rgba(59, 130, 246, 0.4)";
        }
        if (addKeyLabel) addKeyLabel.textContent = "Добавить ключ Google Gemini API";
        if (newKeyInput) newKeyInput.placeholder = "Вставьте ключ Gemini (например: AIzaSy...)";
        if (keysPoolTitle) keysPoolTitle.textContent = "Пул ключей Gemini API";
        if (vendorBanner) vendorBanner.style.display = "flex";
        if (vendorDesc) vendorDesc.textContent = "🔵 Google AI Studio: мультимодальные модели Flash.";
        if (vendorLink) {
            vendorLink.href = "https://aistudio.google.com/app/apikey";
            vendorLink.innerHTML = "<span>Получить ключ Gemini</span><span>↗</span>";
        }
    } else {
        if (tabMistralBtn) {
            tabMistralBtn.style.background = "rgba(249, 115, 22, 0.2)";
            tabMistralBtn.style.color = "#fdba74";
            tabMistralBtn.style.borderColor = "rgba(249, 115, 22, 0.4)";
        }
        if (addKeyLabel) addKeyLabel.textContent = "Добавить ключ Mistral AI API";
        if (newKeyInput) newKeyInput.placeholder = "Вставьте ключ Mistral (например: mistral_...)";
        if (keysPoolTitle) keysPoolTitle.textContent = "Пул ключей Mistral AI";
        if (vendorBanner) vendorBanner.style.display = "flex";
        if (vendorDesc) vendorDesc.textContent = "🟠 Mistral AI: европейский провайдер с бесплатной 12B моделью open-mistral-nemo.";
        if (vendorLink) {
            vendorLink.href = "https://console.mistral.ai/api-keys/";
            vendorLink.innerHTML = "<span>Получить ключ Mistral</span><span>↗</span>";
        }
    }

    renderKeysList();
}

// Обновление пула ключей из загруженных настроек
function updateKeysPoolFromSettings(geminiKeysStr, mistralKeysStr, openaiKeysStr) {
    currentGeminiKeys = parseKeysList(geminiKeysStr);
    currentMistralKeys = parseKeysList(mistralKeysStr);
    currentOpenaiKeys = parseKeysList(openaiKeysStr);

    currentApiKeys = currentOpenaiKeys.length > 0 ? currentOpenaiKeys : currentGeminiKeys;
    updateKeyManagerBadge();
}

function updateKeyManagerBadge() {
    const badge = document.getElementById("key-manager-badge");
    const openaiBadge = document.getElementById("openai-keys-badge");
    const geminiBadge = document.getElementById("gemini-keys-badge");
    const mistralBadge = document.getElementById("mistral-keys-badge");

    const totalKeys = currentOpenaiKeys.length + currentGeminiKeys.length + currentMistralKeys.length;

    if (openaiBadge) openaiBadge.textContent = currentOpenaiKeys.length;
    if (geminiBadge) geminiBadge.textContent = currentGeminiKeys.length;
    if (mistralBadge) mistralBadge.textContent = currentMistralKeys.length;

    if (badge) {
        badge.textContent = `${currentOpenaiKeys.length} O | ${currentGeminiKeys.length} G | ${currentMistralKeys.length} M`;
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
    
    switchKeyManagerTab(activeKeyManagerTab || "openai");
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
    
    let activeList = currentOpenaiKeys;
    let providerName = "OpenAI / Groq";
    if (activeKeyManagerTab === "gemini") {
        activeList = currentGeminiKeys;
        providerName = "Gemini API";
    } else if (activeKeyManagerTab === "mistral") {
        activeList = currentMistralKeys;
        providerName = "Mistral AI";
    }

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
    
    let activeList = currentOpenaiKeys;
    let placeholderFilter = "your_openai_api_key";
    let providerName = "OpenAI / Groq";

    if (activeKeyManagerTab === "gemini") {
        activeList = currentGeminiKeys;
        placeholderFilter = "your_gemini_api_key";
        providerName = "Gemini API";
    } else if (activeKeyManagerTab === "mistral") {
        activeList = currentMistralKeys;
        placeholderFilter = "your_mistral_api_key";
        providerName = "Mistral AI";
    }

    const parts = parseKeysList(raw);
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
        showToast(`Добавлено ключей в пул ${providerName}: ${addedCount}`, "success");
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
            mistral_model: userSettings.mistral_model || "open-mistral-nemo",
            openai_api_keys: currentOpenaiKeys.join(","),
            openai_provider_preset: userSettings.openai_provider_preset || "groq",
            openai_base_url: userSettings.openai_base_url || "https://api.groq.com/openai/v1",
            openai_model: userSettings.openai_model || "llama-3.3-70b-versatile"
        };
        
        const response = await fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        
        if (response.ok) {
            userSettings.gemini_api_keys = payload.gemini_api_keys;
            userSettings.mistral_api_keys = payload.mistral_api_keys;
            userSettings.openai_api_keys = payload.openai_api_keys;
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
            const parts = [];
            if (data.openai && data.openai.total > 0) parts.push(`OpenAI/Groq (${data.openai.available}/${data.openai.total})`);
            if (data.gemini && data.gemini.total > 0) parts.push(`Gemini (${data.gemini.available}/${data.gemini.total})`);
            if (data.mistral && data.mistral.total > 0) parts.push(`Mistral (${data.mistral.available}/${data.mistral.total})`);
            if (parts.length > 0) {
                toastMsg = `Проверка: ${parts.join(", ")}`;
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
            const postfixInput = document.getElementById("sys-cover-letter-postfix");
            if (postfixInput) {
                postfixInput.value = data.cover_letter_postfix || "";
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

            // Подгружаем и заполняем пресеты OpenAI (обратная совместимость)
            if (data.openai_presets && Array.isArray(data.openai_presets)) {
                openaiPresetsMap = {};
                data.openai_presets.forEach(p => {
                    openaiPresetsMap[p.id] = p;
                });
            } else if (Object.keys(openaiPresetsMap).length === 0) {
                await loadOpenaiPresets();
            }

            const sysOpenaiPresetSelect = document.getElementById("sys-openai-preset-select");
            if (sysOpenaiPresetSelect) {
                if (Object.keys(openaiPresetsMap).length > 0) {
                    sysOpenaiPresetSelect.innerHTML = "";
                    Object.values(openaiPresetsMap).forEach(p => {
                        const opt = document.createElement("option");
                        opt.value = p.id;
                        opt.textContent = `${p.badge || "⚡"} ${p.name}`;
                        sysOpenaiPresetSelect.appendChild(opt);
                    });
                }
                const activePreset = data.openai_provider_preset || userSettings.openai_provider_preset || "groq";
                sysOpenaiPresetSelect.value = activePreset;
                handleOpenaiPresetChange(activePreset, data.openai_model || userSettings.openai_model, data.openai_base_url || userSettings.openai_base_url);
            }
            
            await loadModelsDropdown(data.gemini_model || userSettings.gemini_model, data.mistral_model || userSettings.mistral_model);

            // Инициализация единых провайдеров и карточек
            await loadUnifiedProviders();
            
            let activeProv = "groq";
            if (data.primary_provider === "gemini") {
                activeProv = "gemini";
            } else if (data.primary_provider === "mistral") {
                activeProv = "mistral";
            } else if (data.openai_provider_preset) {
                activeProv = data.openai_provider_preset;
            } else if (userSettings.openai_provider_preset) {
                activeProv = userSettings.openai_provider_preset;
            }

            activeUnifiedProvider = activeProv;
            renderProvidersCards();

            let targetModel = null;
            if (activeProv === "gemini") {
                targetModel = data.gemini_model || userSettings.gemini_model;
            } else if (activeProv === "mistral") {
                targetModel = data.mistral_model || userSettings.mistral_model;
            } else {
                targetModel = data.openai_model || userSettings.openai_model;
            }

            await selectUnifiedProvider(activeProv, targetModel, data.openai_base_url || userSettings.openai_base_url);
        }
    } catch (e) {
        console.error("Error loading system settings:", e);
        showToast("Не удалось загрузить системные настройки.", "error");
    }
}

// Сохранение системных настроек
async function saveSystemSettings(closeModal = true) {
    const saveBtn = document.getElementById("system-settings-save-btn");
    const quickSaveBtn = document.getElementById("sys-quick-save-prompt-btn");
    const modal = document.getElementById("system-settings-modal");
    
    if (saveBtn) {
        saveBtn.setAttribute("disabled", "true");
        saveBtn.textContent = "Сохранение...";
    }
    if (quickSaveBtn) {
        quickSaveBtn.setAttribute("disabled", "true");
        quickSaveBtn.innerHTML = `<span>⏳</span><span>Сохранение...</span>`;
    }

    try {
        const promptEditor = document.getElementById("sys-prompt-editor");
        const fallbackToggle = document.getElementById("sys-fallback-toggle");
        const postfixInput = document.getElementById("sys-cover-letter-postfix");
        const mainPrimarySelect = document.getElementById("sys-main-primary-select");

        const selectedPrimary = (mainPrimarySelect && mainPrimarySelect.value) 
            ? mainPrimarySelect.value 
            : (activeUnifiedProvider || (userSettings && userSettings.primary_provider) || "groq");

        let primaryProvider = "openai";
        let openaiPreset = selectedPrimary;

        if (selectedPrimary === "gemini") {
            primaryProvider = "gemini";
        } else if (selectedPrimary === "mistral") {
            primaryProvider = "mistral";
        } else {
            primaryProvider = "openai";
            openaiPreset = selectedPrimary;
        }

        const payload = {
            system_prompt: promptEditor ? promptEditor.value : "",
            cover_letter_postfix: postfixInput ? postfixInput.value : "",
            primary_provider: primaryProvider,
            fallback_enabled: fallbackToggle ? fallbackToggle.checked : true,
            openai_provider_preset: openaiPreset
        };

        const response = await fetch("/api/system-settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        if (response.ok) {
            userSettings.primary_provider = primaryProvider;
            userSettings.openai_provider_preset = openaiPreset;
            activeUnifiedProvider = selectedPrimary;

            // Синхронизируем флаг is_primary для выбранного провайдера
            fetch(`/api/providers/${encodeURIComponent(selectedPrimary)}`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ is_primary: true })
            }).catch(() => {});

            showToast("Системные настройки успешно сохранены!", "success");
            if (closeModal && modal) {
                modal.classList.add("hide");
            }
            await loadUnifiedProviders();
            renderProvidersCards();
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
        if (quickSaveBtn) {
            quickSaveBtn.removeAttribute("disabled");
            quickSaveBtn.innerHTML = `<span>💾</span><span>Сохранить</span>`;
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

async function doGenerateCoverLetter(job, textarea, btn, btnText, btnIcon, hintElem) {
    if (btn) btn.setAttribute("disabled", "true");
    if (btnIcon) btnIcon.textContent = "⏳";
    if (btnText) btnText.textContent = "Генерация ИИ...";
    showToast("✨ ИИ составляет персонализированное сопроводительное письмо...", "info");

    try {
        const response = await fetch(`/api/generate-cover-letter/${job.id}`, {
            method: "POST"
        });
        const data = await response.json();
        if (response.ok && data.status === "ok") {
            textarea.value = data.cover_letter;
            job.cover_letter = data.cover_letter;
            const found = currentJobs.find(j => String(j.id) === String(job.id));
            if (found) found.cover_letter = data.cover_letter;
            showToast("✅ Сопроводительное письмо успешно составлено!", "success");
            if (hintElem) hintElem.classList.add("hide");
        } else {
            showToast("Ошибка генерации письма: " + (data.message || data.detail || "не удалось сгенерировать"), "error");
        }
    } catch (e) {
        console.error("Error generating cover letter:", e);
        showToast("Сетевая ошибка при генерации письма.", "error");
    } finally {
        if (btn) btn.removeAttribute("disabled");
        if (btnIcon) btnIcon.textContent = "✨";
        if (btnText) btnText.textContent = textarea.value.trim() ? "Перегенерировать с ИИ" : "Сгенерировать письмо с ИИ";
    }
}

async function handleGenerateLetterQuick(jobId) {
    const job = currentJobs.find(j => String(j.id) === String(jobId));
    const title = job ? job.title : jobId;
    showToast(`✨ ИИ составляет письмо для "${title}"...`, "info");
    try {
        const response = await fetch(`/api/generate-cover-letter/${jobId}`, { method: "POST" });
        const data = await response.json();
        if (response.ok && data.status === "ok") {
            if (job) job.cover_letter = data.cover_letter;
            showToast(`✅ Письмо для "${title}" готово! Нажмите на вакансию для просмотра.`, "success");
            renderJobsList(false);
        } else {
            showToast("Ошибка при генерации письма: " + (data.message || data.detail || "не удалось составить"), "error");
        }
    } catch (e) {
        console.error("Error generating letter quick:", e);
        showToast("Сетевая ошибка при генерации письма.", "error");
    }
}
window.handleGenerateLetterQuick = handleGenerateLetterQuick;

/**
 * Liquid Glass Dynamic Specular Highlights & Pointer Morphology
 */
function initLiquidGlassInteractivity() {
    let lastMove = 0;
    document.addEventListener("pointermove", (e) => {
        const now = performance.now();
        if (now - lastMove < 32) return; // ~30fps throttle is visually seamless & zero CPU load
        lastMove = now;
        
        const target = (e.target && typeof e.target.closest === "function") 
            ? e.target.closest(".stat-card, .btn-primary, .modal-content, .auth-box") 
            : null;
        if (target) {
            target.style.setProperty("--mouse-x", `${e.offsetX}px`);
            target.style.setProperty("--mouse-y", `${e.offsetY}px`);
        }
    }, { passive: true });
}
