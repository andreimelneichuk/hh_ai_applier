from .auth import router as auth_router
from .settings import router as settings_router
from .vacancies import router as vacancies_router
from .pipeline import router as pipeline_router

__all__ = ["auth_router", "settings_router", "vacancies_router", "pipeline_router"]
