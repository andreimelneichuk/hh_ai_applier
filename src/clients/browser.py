import os
import re
import logging
import time
import threading
from typing import List, Dict, Any, Tuple
from playwright.sync_api import sync_playwright
from src.core.paths import get_app_data_dir

logger = logging.getLogger("HHBrowserClient")

class HHBrowserClient:
    _lock = threading.Lock()

    def __init__(self, user_data_dir: str = None):
        if not user_data_dir or user_data_dir == "playwright_session":
            self.user_data_dir = os.path.join(get_app_data_dir(), "playwright_session")
        else:
            self.user_data_dir = os.path.abspath(user_data_dir)

        if not os.path.exists(self.user_data_dir):
            os.makedirs(self.user_data_dir, exist_ok=True)
        self.playwright = None
        self.context = None
        self._cleanup_singleton_files()

    def _cleanup_singleton_files(self):
        """Удаляет Singleton-файлы Chrome, которые блокируют запуск после краша."""
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            path = os.path.join(self.user_data_dir, name)
            if os.path.exists(path) or os.path.islink(path):
                try:
                    os.remove(path)
                    logger.info(f"Удалён Singleton-файл Chrome: {path}")
                except Exception as e:
                    logger.warning(f"Не удалось удалить Singleton-файл {path}: {e}")

    def start(self, headless: bool = True, args: List[str] = None):
        """Запускает Playwright и создает постоянный контекст."""
        self._cleanup_singleton_files()
        if not self.playwright:
            self.playwright = sync_playwright().start()
        if not self.context:
            launch_args = args or []
            # Пробуем доступные каналы запуска (системный Chrome, Edge или встроенный Chromium)
            channels_to_try = ["chrome", None, "msedge"]
            last_err = None
            for channel in channels_to_try:
                try:
                    kwargs = {
                        "user_data_dir": self.user_data_dir,
                        "headless": headless,
                        "args": launch_args,
                        "no_viewport": True,
                        "slow_mo": 100 if not headless else 0
                    }
                    if channel:
                        kwargs["channel"] = channel
                    self.context = self.playwright.chromium.launch_persistent_context(**kwargs)
                    if channel:
                        logger.info(f"Браузер успешно запущен через канал channel='{channel}'")
                    break
                except Exception as e:
                    last_err = e
                    continue
            if not self.context and last_err:
                logger.error(f"Не удалось запустить браузер ни через один канал: {last_err}")
                raise last_err

    def stop(self):
        """Останавливает Playwright и закрывает контекст."""
        if self.context:
            self.context.close()
            self.context = None
        if self.playwright:
            self.playwright.stop()
            self.playwright = None

    def open_login_browser(self):
        """Открывает видимое окно браузера для прохождения ручного входа."""
        with self._lock:
            logger.info("Запуск браузера для входа...")
            # Если уже запущен, останавливаем, так как нужен headless=False
            was_running = self.context is not None
            if was_running:
                self.stop()
                
            self.start(headless=False, args=["--start-maximized"])
            page = self.context.new_page()
            page.goto("https://hh.ru/login")
            
            logger.info("Браузер запущен. Пожалуйста, пройдите авторизацию. Закройте окно браузера для продолжения.")
            
            # Ждем, пока браузер закроется
            closed = [False]
            def on_close(ctx):
                closed[0] = True
            self.context.on("close", on_close)
            
            while not closed[0]:
                try:
                    if not self.context.pages:
                        closed[0] = True
                        break
                    page.wait_for_timeout(1000)
                except Exception:
                    closed[0] = True
                    break
            logger.info("Браузер для авторизации закрыт.")
            self.stop() # закрываем после логина

    def _ensure_started(self):
        if not self.context:
            self.start(headless=True)

    def is_logged_in(self) -> bool:
        """Проверяет, авторизован ли пользователь (сессия активна)."""
        with self._lock:
            logger.info("Проверка сессии hh.ru...")
            try:
                self._ensure_started()
                page = self.context.new_page()
                page.goto("https://hh.ru/applicant/resumes", timeout=25000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                
                # 1. Проверяем через глобальный JS-объект hh.ru (самый надежный способ)
                auth_info = {}
                try:
                    auth_info = page.evaluate("""() => {
                        if (window.globalVars) {
                            return {
                                userType: window.globalVars.userType || '',
                                hhid: window.globalVars.hhid || '',
                                login: window.globalVars.login || ''
                            };
                        }
                        return null;
                    }""")
                except Exception:
                    pass

                if auth_info:
                    user_type = auth_info.get("userType", "")
                    has_id = bool(auth_info.get("hhid") or auth_info.get("login"))
                    logged_in = (user_type == "applicant") or (user_type != "anonymous" and has_id)
                    logger.info(f"Проверка сессии по globalVars: userType='{user_type}', hhid='{auth_info.get('hhid')}', Вошли: {logged_in}")
                    page.close()
                    return bool(logged_in)

                # 2. Фолбек через проверку элементов интерфейса
                current_url = page.url.lower()
                if "/login" in current_url or "/account/login" in current_url:
                    page.close()
                    return False

                # Проверяем наличие кнопки 'Войти'
                login_btn = page.query_selector('[data-qa="login"], a[href*="/login"], a[href*="/account/login"]')
                profile_elem = page.query_selector('[data-qa="mainmenu_applicantInfo"], [data-qa="mainmenu_profile"], [data-qa="profileAndResumes-button"]')

                logged_in = profile_elem is not None and login_btn is None
                logger.info(f"Проверка сессии по DOM: profile_found={profile_elem is not None}, login_btn={login_btn is not None}. Вошли: {logged_in}")
                page.close()
                return logged_in
            except Exception as e:
                logger.error(f"Ошибка при проверке авторизации: {e}")
                if 'page' in locals() and not page.is_closed():
                    page.close()
                return False

    def get_my_info(self) -> Dict[str, Any]:
        """Получает базовую информацию о пользователе со страницы резюме."""
        with self._lock:
            logger.info("Получение информации о пользователе...")
            try:
                self._ensure_started()
                page = self.context.new_page()
                page.goto("https://hh.ru/applicant/resumes", timeout=25000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                
                # Пробуем получить email из window.globalVars
                email = "Не указан"
                try:
                    g_login = page.evaluate("() => window.globalVars ? window.globalVars.login : ''")
                    if g_login:
                        email = str(g_login)
                except Exception:
                    pass

                # Извлекаем имя с шапки страницы или бокового меню
                name_elem = page.query_selector('.resume-header-name, .applicant-name, [data-qa="mainmenu_applicantInfo"], [data-qa="profileAndResumes-button"]')
                name = name_elem.text_content().strip() if name_elem else "Пользователь HH"
                if not name or "Резюме" in name:
                    name = email.split("@")[0] if "@" in email else "Пользователь HH"

                page.close()
                return {
                    "first_name": name,
                    "last_name": "",
                    "middle_name": "",
                    "email": email,
                    "is_applicant": True
                }
            except Exception as e:
                logger.error(f"Не удалось получить информацию о пользователе: {e}")
                if 'page' in locals() and not page.is_closed():
                    page.close()
                return {}

    def get_my_resumes(self) -> List[Dict[str, Any]]:
        """Получает список всех резюме пользователя с сайта."""
        with self._lock:
            logger.info("Получение списка собственных резюме...")
            resumes = []
            try:
                self._ensure_started()
                page = self.context.new_page()
                page.goto("https://hh.ru/applicant/resumes", timeout=25000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                
                # Даем время React-приложению отрендерить карточки резюме
                try:
                    page.wait_for_selector('a[href*="/resume/"], [data-qa*="resume-card-link"], [data-qa*="resume"]', timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                
                logger.info(f"Навигация успешна. URL: {page.url}, Title: {page.title()}")
                
                seen_ids = set()
                
                # 1. Поиск через ссылки и элементы в DOM
                elements = page.query_selector_all('a, [data-qa*="resume-card-link"]')
                logger.info(f"Всего элементов найдено на странице: {len(elements)}")
                
                for elem in elements:
                    href = elem.get_attribute("href") or ""
                    qa = elem.get_attribute("data-qa") or ""
                    
                    # Поиск ID резюме из href или data-qa (например data-qa="resume-card-link-6baf401...")
                    target_str = f"{href} {qa}"
                    if "/resume/" in target_str or "resume-card-link-" in target_str or "resumeHash=" in target_str:
                        if any(x in href for x in ["/edit/", "/status/", "/constructor/", "/jobs", "/history", "/new", "/article"]):
                            continue
                        match = re.search(r'(?:/resume/|resume-card-link-|resumeHash=)([a-zA-Z0-9]{20,})', target_str)
                        if match:
                            resume_id = match.group(1)
                            if resume_id not in seen_ids:
                                seen_ids.add(resume_id)
                                
                                raw_title = elem.text_content().strip()
                                clean_title = raw_title.replace("Постоянная работа, подработка", "").strip()
                                clean_title = re.split(r'(Уровень дохода|Многие не видят|Обновлено|Сделать|Поднять|Просмотры|Приглашения)', clean_title)[0].strip()
                                
                                resumes.append({
                                    "id": resume_id,
                                    "title": clean_title or "Резюме",
                                    "updated_at": "Недавно"
                                })
                
                # 2. Фолбек: если через DOM ничего не найдено или найдены не все, сканируем исходный HTML
                page_html = page.content()
                all_hashes = set(re.findall(r'(?:/resume/|resume-card-link-|resumeHash=|resume=)([a-f0-9]{32,44})', page_html))
                for r_hash in all_hashes:
                    if r_hash not in seen_ids:
                        seen_ids.add(r_hash)
                        logger.info(f"Найдено резюме из HTML разметки: {r_hash}")
                        resumes.append({
                            "id": r_hash,
                            "title": "Резюме",
                            "updated_at": "Недавно"
                        })
                
                logger.info(f"Итого найдено резюме пользователя: {len(resumes)} ({[r['id'][:8] + '...' for r in resumes]})")
                page.close()
            except Exception as e:
                logger.error(f"Не удалось загрузить список резюме: {e}")
                if 'page' in locals() and not page.is_closed():
                    page.close()
            return resumes

    _resumes_cache: Dict[str, Dict[str, Any]] = {}

    def get_resume(self, resume_id: str) -> Dict[str, Any]:
        """Получает детали конкретного резюме, открывая страницу его просмотра (кешируется на сессию)."""
        if resume_id in HHBrowserClient._resumes_cache:
            logger.info(f"Использование закешированных данных резюме {resume_id}")
            return HHBrowserClient._resumes_cache[resume_id]

        logger.info(f"Получение деталей резюме {resume_id}...")
        try:
            with self._lock:
                self._ensure_started()
                page = self.context.new_page()
                page.goto(f"https://hh.ru/resume/{resume_id}", timeout=25000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(1000)
                
                title_elem = page.query_selector('[data-qa="resume-block-title-position"], [data-qa="resume-title"], h1')
                title = title_elem.text_content().strip() if title_elem else "Специалист"
                
                skills_elem = page.query_selector('[data-qa="resume-block-skills-content"], [data-qa="resume-block-skills"]')
                skills = skills_elem.text_content().strip() if skills_elem else ""
                
                # Ключевые навыки (теги)
                key_skills = []
                for s_elem in page.query_selector_all('[data-qa="bloko-tag__text"], [data-qa="skills-element"]'):
                    txt = s_elem.text_content().strip()
                    if txt:
                        key_skills.append({"name": txt})
                
                # Полный текстовый контент резюме
                raw_text = ""
                try:
                    raw_text = page.locator("body").inner_text()
                except Exception:
                    pass

                # Общий стаж работы
                total_experience = ""
                total_exp_elem = page.query_selector('[data-qa="resume-block-experience-years"], .resume-block__title-text_sub, [data-qa="resume-block-experience"] h2 span')
                if total_exp_elem:
                    total_experience = total_exp_elem.text_content().strip()
                if not total_experience and raw_text:
                    match_exp = re.search(r'Опыт работы\s*([0-9]+(?:\s*(?:год|года|лет|месяц|месяца|месяцев))+(?:\s*[0-9]+\s*(?:месяц|месяца|месяцев))?)', raw_text, re.IGNORECASE)
                    if match_exp:
                        total_experience = match_exp.group(1).strip()

                # Опыт работы (детальный)
                experience = []
                exp_blocks = page.query_selector_all('[data-qa="resume-block-experience-position"]')
                for block in exp_blocks:
                    position = block.text_content().strip()
                    parent = block.evaluate_handle("el => el.closest('.resume-block-item-gap')")
                    desc = ""
                    company = "Компания"
                    period = ""
                    if parent:
                        desc_elem = parent.as_element().query_selector('[data-qa="resume-block-experience-description"]')
                        if desc_elem:
                            desc = desc_elem.text_content().strip()
                        comp_elem = parent.as_element().query_selector('[data-qa="resume-block-experience-company"]')
                        if comp_elem:
                            company = comp_elem.text_content().strip()
                        date_elem = parent.as_element().query_selector('[data-qa="resume-block-experience-dates"], .bloko-column_xs-4, .bloko-column_s-2, .bloko-column_m-3')
                        if date_elem:
                            period = date_elem.text_content().strip().replace('\xa0', ' ')

                    experience.append({
                        "position": position,
                        "company": company,
                        "description": desc,
                        "period": period,
                        "start": period
                    })
                    
                # Дополнительно извлекаем блок "О себе"
                about_elem = page.query_selector('[data-qa="resume-block-skills"]')
                if not skills and about_elem:
                    skills = about_elem.text_content().strip()

                # Локация кандидата / город / переезд
                address_elem = page.query_selector('[data-qa="resume-personal-address"]')
                address_text = address_elem.text_content().strip().replace('\xa0', ' ') if address_elem else ""

                # Имя кандидата
                name_elem = page.query_selector('[data-qa="resume-personal-name"]')
                full_name = name_elem.text_content().strip() if name_elem else "Кандидат"
                
                name_parts = full_name.split()
                first_name = name_parts[0] if len(name_parts) > 0 else full_name
                last_name = name_parts[1] if len(name_parts) > 1 else ""
                middle_name = " ".join(name_parts[2:]) if len(name_parts) > 2 else ""

                page.close()
                result = {
                    "first_name": first_name,
                    "middle_name": middle_name,
                    "last_name": last_name,
                    "title": title,
                    "skills": skills,
                    "key_skills": key_skills,
                    "experience": experience,
                    "total_experience": total_experience,
                    "location": address_text,
                    "raw_text": raw_text,
                    "education": {"primary": []}
                }
                HHBrowserClient._resumes_cache[resume_id] = result
                return result
        except Exception as e:
            logger.error(f"Не удалось получить детали резюме: {e}")
            if 'page' in locals() and not page.is_closed():
                page.close()
            return {}

    def get_recommended_vacancies(self, resume_id: str, area_id: str = "113", period_days: int = 3, max_pages: int = 3) -> List[Dict[str, Any]]:
        """Получает список рекомендованных (подходящих) вакансий от hh.ru для конкретного резюме."""
        logger.info(f"Получение рекомендованных вакансий для резюме {resume_id}...")
        vacancies = []
        try:
            with self._lock:
                self._ensure_started()
                page = self.context.new_page()
                
                base_url = f"https://hh.ru/search/vacancy?resume={resume_id}"
                if area_id and area_id != "113":
                    base_url += f"&area={area_id}"
                if period_days:
                    base_url += f"&search_period={period_days}"
                
                for current_page in range(max_pages):
                    url = f"{base_url}&page={current_page}"
                    logger.info(f"Переход на страницу рекомендаций {current_page}: {url}")
                    page.goto(url, timeout=20000)
                    page.wait_for_load_state("domcontentloaded")
                    
                    try:
                        page.wait_for_selector('[data-qa="serp-item__title"]', timeout=4000)
                    except Exception:
                        logger.info("Не удалось дождаться карточек вакансий на странице рекомендаций.")
                    
                    cards = page.query_selector_all('[data-qa="serp-item__title"]')
                    logger.info(f"Найдено {len(cards)} карточек рекомендаций на странице {current_page}")
                    
                    if not cards:
                        break
                        
                    for card in cards:
                        title = card.text_content().strip()
                        href = card.get_attribute("href") or ""
                        match = re.search(r'/vacancy/(\d+)', href)
                        if match:
                            vacancy_id = match.group(1)
                            company = "Не указано"
                            parent = card.evaluate_handle("el => el.closest('[data-qa=\"vacancy-serp__vacancy\"]')")
                            if parent:
                                employer_elem = parent.as_element().query_selector('[data-qa="vacancy-serp__vacancy-employer"]')
                                if employer_elem:
                                    company = employer_elem.text_content().strip()
                            
                            vacancies.append({
                                "id": vacancy_id,
                                "title": title,
                                "company": company,
                                "alternate_url": f"https://hh.ru/vacancy/{vacancy_id}"
                            })
                            
                page.close()
        except Exception as e:
            logger.error(f"Исключение при получении рекомендованных вакансий: {e}")
            if 'page' in locals() and not page.is_closed():
                page.close()
        logger.info(f"Найдено всего {len(vacancies)} рекомендованных вакансий для резюме {resume_id}")
        return vacancies

    def search_vacancies(self, query: str, area_id: str = "113", period_days: int = 3, max_pages: int = 2) -> List[Dict[str, Any]]:
        """Ищет вакансии через веб-поиск и парсит результаты."""
        logger.info(f"Поиск вакансий по запросу '{query}'...")
        vacancies = []
        try:
            with self._lock:
                self._ensure_started()
                page = self.context.new_page()
                
                import urllib.parse
                safe_query = urllib.parse.quote(query)
                search_url = f"https://hh.ru/search/vacancy?text={safe_query}&area={area_id}&search_period={period_days}"
                
                for current_page in range(max_pages):
                    url = f"{search_url}&page={current_page}"
                    logger.info(f"Переход на страницу поиска {current_page}: {url}")
                    page.goto(url, timeout=20000)
                    
                    page.wait_for_load_state("domcontentloaded")
                    try:
                        page.wait_for_selector('[data-qa="serp-item__title"]', timeout=3000)
                    except Exception:
                        logger.info("Не удалось дождаться карточек вакансий. Возможно, их нет.")
                    
                    cards = page.query_selector_all('[data-qa="serp-item__title"]')
                    logger.info(f"Найдено {len(cards)} карточек на странице {current_page}")
                    
                    if not cards:
                        logger.info("Вакансий больше не найдено на этой странице.")
                        break
                        
                    for card in cards:
                        title = card.text_content().strip()
                        href = card.get_attribute("href") or ""
                        match = re.search(r'/vacancy/(\d+)', href)
                        if match:
                            vacancy_id = match.group(1)
                            
                            company = "Не указано"
                            parent = card.evaluate_handle("el => el.closest('[data-qa=\"vacancy-serp__vacancy\"]')")
                            if parent:
                                employer_elem = parent.as_element().query_selector('[data-qa="vacancy-serp__vacancy-employer"]')
                                if employer_elem:
                                    company = employer_elem.text_content().strip()
                            
                            vacancies.append({
                                "id": vacancy_id,
                                "title": title,
                                "company": company,
                                "alternate_url": f"https://hh.ru/vacancy/{vacancy_id}"
                            })
                            
                page.close()
        except Exception as e:
            logger.error(f"Исключение при поиске вакансий: {e}")
            if 'page' in locals() and not page.is_closed():
                page.close()
        logger.info(f"Найдено всего {len(vacancies)} вакансий по запросу '{query}'")
        return vacancies

    def get_vacancy_details(self, vacancy_id: str) -> Dict[str, Any]:
        """Получает полные детали вакансии, открывая ее страницу."""
        logger.info(f"Получение деталей вакансии {vacancy_id}...")
        try:
            with self._lock:
                self._ensure_started()
                page = self.context.new_page()
                page.goto(f"https://hh.ru/vacancy/{vacancy_id}", timeout=20000)
                page.wait_for_load_state("domcontentloaded")
                
                title_elem = page.query_selector('[data-qa="vacancy-title"]')
                title = title_elem.text_content().strip() if title_elem else "Вакансия"
                
                comp_elem = page.query_selector('[data-qa="vacancy-company-name"]')
                company = comp_elem.text_content().strip() if comp_elem else "Не указано"
                
                salary_elem = page.query_selector('[data-qa="vacancy-salary"]')
                salary = salary_elem.text_content().strip() if salary_elem else "Не указана"
                
                desc_elem = page.query_selector('[data-qa="vacancy-description"]')
                description = desc_elem.text_content().strip() if desc_elem else ""

                exp_elem = page.query_selector('[data-qa="vacancy-experience"]')
                experience = exp_elem.text_content().strip() if exp_elem else "Не указан"

                emp_elem = page.query_selector('[data-qa="vacancy-view-employment-mode"], [data-qa="vacancy-view-employment-type"]')
                employment = emp_elem.text_content().strip() if emp_elem else "Не указана"

                sched_elem = page.query_selector('[data-qa="vacancy-work-schedule-type"], [data-qa="vacancy-view-work-schedule"], [data-qa="work-schedule-type"]')
                schedule = sched_elem.text_content().strip() if sched_elem else "Не указан"

                loc_elem = page.query_selector('[data-qa="vacancy-view-raw-address"], [data-qa="vacancy-view-location"], [data-qa="vacancy-address"]')
                location = loc_elem.text_content().strip() if loc_elem else "Не указана"
                
                skills = []
                for s_elem in page.query_selector_all('[data-qa="bloko-tag__text"]'):
                    skills.append(s_elem.text_content().strip())
                    
                already_applied = False
                viewed = page.query_selector('[data-qa="vacancy-response-link-viewed"]')
                already_text = page.query_selector(
                    'text="Вы уже откликнулись", text="Посмотреть отклик", text="Вам отказали", text="Отказ", [data-qa="vacancy-response-link-rejected"], .vacancy-response-link-rejected'
                )
                
                body_text = page.inner_text("body").lower()
                if viewed or already_text or "вы уже откликнулись" in body_text or "вам отказали" in body_text:
                    already_applied = True
                    
                page.close()
                return {
                    "id": vacancy_id,
                    "title": title,
                    "company": company,
                    "description": description,
                    "skills": skills,
                    "salary": salary,
                    "experience": experience,
                    "employment": employment,
                    "schedule": schedule,
                    "location": location,
                    "alternate_url": f"https://hh.ru/vacancy/{vacancy_id}",
                    "already_applied": already_applied
                }
        except Exception as e:
            logger.error(f"Не удалось получить детали вакансии {vacancy_id}: {e}")
            if 'page' in locals() and not page.is_closed():
                page.close()
            return {}

    def get_vacancy_questions(self, vacancy_id: str) -> List[Dict[str, Any]]:
        """Извлекает вопросы и тесты работодателя для вакансии, если они есть."""
        logger.info(f"Проверка наличия вопросов/теста для вакансии {vacancy_id}...")
        questions = []
        try:
            with self._lock:
                self._ensure_started()
                page = self.context.new_page()
                page.goto(f"https://hh.ru/applicant/vacancy_response?vacancyId={vacancy_id}&startedWithQuestion=false", timeout=20000)
                page.wait_for_load_state("domcontentloaded")
                
                # Проверяем, есть ли вопросы на странице
                task_questions = page.query_selector_all('[data-qa="task-question"]')
                if not task_questions:
                    # Попробуем альтернативные селекторы вопросов
                    task_questions = page.query_selector_all('[data-qa="task-body"]')

                for i, q_elem in enumerate(task_questions):
                    q_text = q_elem.text_content().strip()
                    if not q_text:
                        continue
                    
                    # Находим родительский контейнер вопроса
                    parent = q_elem.evaluate_handle('el => el.closest("[data-qa=\\\"task-body\\\"]") || el.closest("form") || el.parentElement')
                    parent_el = parent.as_element() if parent else q_elem
                    
                    # Определяем тип ввода (textarea, radio, checkbox, text input)
                    textarea = parent_el.query_selector('textarea') if parent_el else None
                    radios = parent_el.query_selector_all('input[type="radio"]') if parent_el else []
                    checkboxes = parent_el.query_selector_all('input[type="checkbox"]') if parent_el else []
                    text_input = parent_el.query_selector('input[type="text"]') if parent_el else None
                    
                    q_id = ""
                    q_type = "text"
                    options = []
                    
                    if textarea:
                        q_id = textarea.get_attribute("name") or f"task_{i}_text"
                        q_type = "text"
                    elif radios:
                        first_radio = radios[0]
                        q_id = first_radio.get_attribute("name") or f"task_{i}_radio"
                        q_type = "single_choice"
                        for r in radios:
                            # Извлекаем текст варианта
                            label = r.evaluate('el => el.closest("label") ? el.closest("label").innerText : el.parentElement.innerText')
                            opt_text = label.strip() if label else (r.get_attribute("value") or "")
                            if opt_text:
                                options.append(opt_text)
                    elif checkboxes:
                        first_cb = checkboxes[0]
                        q_id = first_cb.get_attribute("name") or f"task_{i}_cb"
                        q_type = "multi_choice"
                        for cb in checkboxes:
                            label = cb.evaluate('el => el.closest("label") ? el.closest("label").innerText : el.parentElement.innerText')
                            opt_text = label.strip() if label else (cb.get_attribute("value") or "")
                            if opt_text:
                                options.append(opt_text)
                    elif text_input:
                        q_id = text_input.get_attribute("name") or f"task_{i}_input"
                        q_type = "text"
                    else:
                        q_id = f"task_{i}"
                        q_type = "text"
                        
                    questions.append({
                        "id": q_id,
                        "text": q_text,
                        "type": q_type,
                        "options": options
                    })
                    
                page.close()
        except Exception as e:
            logger.error(f"Не удалось извлечь вопросы для вакансии {vacancy_id}: {e}")
            if 'page' in locals() and not page.is_closed():
                page.close()
        logger.info(f"Для вакансии {vacancy_id} обнаружено вопросов: {len(questions)}")
        return questions

    def apply_to_vacancy(self, vacancy_id: str, resume_title_or_id: str, cover_letter: str, answers: Dict[str, Any] = None, dry_run: bool = False) -> Tuple[bool, str]:
        """Откликается на вакансию, заполняя вопросы работодателя (если есть) и сопроводительное письмо."""
        logger.info(f"Попытка отклика на вакансию {vacancy_id} (ответов передано: {len(answers) if answers else 0})...")
        try:
            with self._lock:
                self._ensure_started()
                page = self.context.new_page()
                
                # Сначала пробуем страницу с вакансией
                page.goto(f"https://hh.ru/vacancy/{vacancy_id}", timeout=20000)
                page.wait_for_load_state("domcontentloaded")
            
                # 1. Проверяем, откликнулись ли уже или получен отказ
                viewed = page.query_selector('[data-qa="vacancy-response-link-viewed"], [data-qa="vacancy-response-link-rejected"]')
                already_text = page.query_selector('text="Вы уже откликнулись", text="Посмотреть отклик", text="Вам отказали", text="Отказ"')
                body_lower = page.inner_text("body").lower()
                
                if viewed or already_text or "вы уже откликнулись" in body_lower or "вам отказали" in body_lower:
                    logger.info(f"На вакансию {vacancy_id} уже откликались ранее или получен отказ.")
                    page.close()
                    return True, "ALREADY_APPLIED"
                    
                # 2. Ищем кнопку отклика
                apply_btn = page.query_selector('[data-qa="vacancy-response-link-top"], button:has-text("Откликнуться"), a:has-text("Откликнуться")')
                if not apply_btn:
                    apply_btn = page.query_selector('text="Откликнуться"')
                    
                if not apply_btn:
                    if "вам отказали" in body_lower or "вы уже откликнулись" in body_lower:
                        logger.info(f"Кнопка отсутствует, обнаружен статус отклика/отказа для вакансии {vacancy_id}.")
                        page.close()
                        return True, "ALREADY_APPLIED"
                    logger.warning("Кнопка отклика не найдена на странице (возможно, вакансия закрыта).")
                    page.close()
                    return False, "Кнопка отклика не найдена"
                    
                # 3. Кликаем откликнуться
                apply_btn.click()
                
                try:
                    page.wait_for_selector('[data-qa="vacancy-response-popup"], [data-qa="task-question"], [data-qa="task-body"], [data-qa="vacancy-response-letter-input"], textarea, [data-qa="resume-select-label"]', timeout=3000)
                except Exception:
                    pass
                
                # 4. Проверяем выбор резюме (если открылся выбор)
                resumes_radios = page.query_selector_all('[data-qa="resume-select-label"], .vacancy-response-resume-item')
                if resumes_radios:
                    target_radio = None
                    target = (resume_title_or_id or "").lower()
                    for radio in resumes_radios:
                        text = radio.text_content().lower()
                        input_elem = radio.query_selector("input")
                        val = (input_elem.get_attribute("value") or "").lower() if input_elem else ""
                        if target and (target in text or target in val or (val and val in target)):
                            target_radio = radio
                            break
                    if not target_radio and resumes_radios:
                        target_radio = resumes_radios[0]
                    if target_radio:
                        target_radio.click()
                        page.wait_for_timeout(300)

                # 5. Обработка вопросов работодателя (если они есть в форме/попапе)
                task_questions = page.query_selector_all('[data-qa="task-question"], [data-qa="task-body"]')
                if task_questions:
                    logger.info(f"Обнаружено {len(task_questions)} элементов вопросов на форме отклика.")
                    
                    if answers:
                        # Нормализуем переданные ответы: приводим ключи к нижнему регистру для гибкого поиска
                        normalized_answers = {}
                        for k, v in answers.items():
                            normalized_answers[str(k).strip()] = str(v).strip()
                            normalized_answers[str(k).strip().lower()] = str(v).strip()

                        # Заполняем текстовые поля вопросов
                        for q_div in page.query_selector_all('[data-qa="task-body"], [data-qa="task-question"]'):
                            q_text = q_div.text_content().strip()
                            parent = q_div.evaluate_handle('el => el.closest("[data-qa=\\\"task-body\\\"]") || el.closest("form") || el.parentElement')
                            parent_el = parent.as_element() if parent else q_div
                            
                            # Ищем textarea внутри
                            textarea = parent_el.query_selector('textarea') if parent_el else None
                            if textarea:
                                name_attr = textarea.get_attribute("name") or ""
                                # Ищем ответ по имени поля или по тексту вопроса
                                ans = normalized_answers.get(name_attr) or normalized_answers.get(q_text) or normalized_answers.get(q_text.lower())
                                if not ans:
                                    for ak, av in normalized_answers.items():
                                        if ak in q_text.lower() or q_text.lower() in ak or (name_attr and name_attr in ak):
                                            ans = av
                                            break
                                if ans:
                                    try:
                                        textarea.scroll_into_view_if_needed()
                                        textarea.click()
                                        textarea.fill(ans)
                                        logger.info(f"Заполнен ответ на вопрос '{q_text[:40]}...': {ans[:40]}...")
                                    except Exception as fill_err:
                                        logger.warning(f"Ошибка заполнения textarea вопроса: {fill_err}")

                            # Ищем радиокнопки
                            radios = parent_el.query_selector_all('input[type="radio"]') if parent_el else []
                            if radios:
                                first_name = radios[0].get_attribute("name") or ""
                                ans = normalized_answers.get(first_name) or normalized_answers.get(q_text.lower())
                                if ans:
                                    ans_lower = ans.lower()
                                    for r in radios:
                                        label = r.evaluate('el => el.closest("label") ? el.closest("label").innerText : el.parentElement.innerText')
                                        val = r.get_attribute("value") or ""
                                        if (label and (ans_lower in label.lower() or label.lower() in ans_lower)) or (val and val.lower() in ans_lower):
                                            try:
                                                r.click()
                                                logger.info(f"Выбран вариант радиокнопки: {label}")
                                                break
                                            except Exception:
                                                pass

                            # Ищем чекбоксы
                            checkboxes = parent_el.query_selector_all('input[type="checkbox"]') if parent_el else []
                            if checkboxes:
                                first_name = checkboxes[0].get_attribute("name") or ""
                                ans = normalized_answers.get(first_name) or normalized_answers.get(q_text.lower())
                                if ans:
                                    ans_lower = ans.lower()
                                    for cb in checkboxes:
                                        label = cb.evaluate('el => el.closest("label") ? el.closest("label").innerText : el.parentElement.innerText')
                                        if label and (ans_lower in label.lower() or label.lower() in ans_lower):
                                            try:
                                                if not cb.is_checked():
                                                    cb.click()
                                                logger.info(f"Отмечен чекбокс: {label}")
                                            except Exception:
                                                pass

                # 6. Раскрываем поле сопроводительного письма (на новой верстке это [data-qa="add-cover-letter"])
                letter_toggles = page.query_selector_all(
                    '[data-qa="add-cover-letter"], [data-qa="vacancy-response-letter-toggle"], button:has-text("Добавить сопроводительное"), button:has-text("Написать сопроводительное"), button:has-text("Сопроводительное письмо"), a:has-text("сопроводительное"), [data-qa="vacancy-response-letter-informer"]'
                )
                for toggle in letter_toggles:
                    try:
                        if toggle.is_visible():
                            toggle.click()
                            page.wait_for_timeout(400)
                            break
                    except Exception:
                        pass

                # 7. Ищем поле ввода сопроводительного письма
                letter_input = page.query_selector(
                    '[data-qa="vacancy-response-popup-form-letter-input"], [data-qa="vacancy-response-letter-input"], textarea[data-qa*="letter"], textarea:not([name*="task"])'
                )
                if not letter_input:
                    # Резервный поиск textarea
                    all_textareas = page.query_selector_all('textarea')
                    if len(all_textareas) == 1:
                        letter_input = all_textareas[0]
                    elif len(all_textareas) > 1:
                        # Берем последний textarea (обычно письмо в конце)
                        letter_input = all_textareas[-1]
                
                if letter_input and cover_letter:
                    try:
                        letter_input.scroll_into_view_if_needed()
                        letter_input.click()
                        letter_input.fill("")
                        letter_input.fill(cover_letter)
                        logger.info(f"Сопроводительное письмо успешно вставлено в форму ({len(cover_letter)} симв.).")
                    except Exception as input_err:
                        logger.warning(f"Не удалось заполнить поле письма: {input_err}")
                else:
                    logger.warning("Поле сопроводительного письма не найдено или письмо пустое, отправляем отклик без него.")
                    
                # 8. Отправка отклика
                if dry_run:
                    logger.info(f"[DRY RUN] Отклик на {vacancy_id} не отправлен. Письмо:\n{cover_letter}\nОтветы:\n{answers}")
                    page.close()
                    return True, "DRY_RUN_SUCCESS"
                    
                submit_btn = page.query_selector(
                    '[data-qa="vacancy-response-submit-popup"], [data-qa="vacancy-response-popup-submit"], button:has-text("Отправить отклик"), button:has-text("Откликнуться")'
                )
                
                if not submit_btn:
                    logger.error("Кнопка подтверждения отклика не найдена в попапе.")
                    page.close()
                    return False, "Кнопка отправки не найдена"
                    
                submit_btn.click()
                try:
                    # Ждем, пока исчезнет попап отклика или появится сообщение об успехе
                    page.wait_for_selector('text="Отклик отправлен", [data-qa="vacancy-response-link-viewed"]', timeout=4000)
                except Exception:
                    page.wait_for_timeout(1500)
                
                logger.info(f"Отклик успешно отправлен на вакансию {vacancy_id}!")
                page.close()
                return True, ""
        except Exception as e:
            logger.exception(f"Исключение при отклике на вакансию {vacancy_id}: {e}")
            if 'page' in locals() and not page.is_closed():
                page.close()
            return False, str(e)
