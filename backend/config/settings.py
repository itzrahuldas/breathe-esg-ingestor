"""
Django settings for Breathe ESG Ingestor.

Environment variables (set via .env locally, Render dashboard in production):
  DJANGO_SECRET_KEY   — required in production (auto-generated fallback for dev)
  DATABASE_URL        — Render PostgreSQL URL  (falls back to SQLite for dev/test)
  DEBUG               — 'True' locally, 'False' in production
  ALLOWED_HOSTS       — space-separated list (default: * for prototype)
  CORS_ALLOW_ALL      — 'True' to allow all origins (prototype default)
  FRONTEND_URL        — e.g. https://breathe-esg.vercel.app (for CORS origins)
"""

import os
from pathlib import Path

import dj_database_url
from decouple import config, Csv

BASE_DIR = Path(__file__).resolve().parent.parent

# ------------------------------------------------------------------
# Security
# ------------------------------------------------------------------
SECRET_KEY = config(
    "DJANGO_SECRET_KEY",
    default="dev-insecure-key-change-me-in-production-breathe-esg-2024",
)
DEBUG = config("DEBUG", default=True, cast=bool)

# Allow all hosts — prototype deployment on Render / Vercel
# Override via ALLOWED_HOSTS env var (comma-separated) if needed
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="*", cast=Csv())

# Always include the Render-assigned domain regardless of env var
RENDER_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if RENDER_HOSTNAME and RENDER_HOSTNAME not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(RENDER_HOSTNAME)

# ------------------------------------------------------------------
# Applications
# ------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "corsheaders",
    # ESG apps
    "ingestor",
]

# ------------------------------------------------------------------
# Middleware — WhiteNoise must come directly after SecurityMiddleware
# ------------------------------------------------------------------
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",   # ← serves static files in prod
    "django.middleware.common.CommonMiddleware",
]

# ------------------------------------------------------------------
# CORS — allow all origins for prototype
# ------------------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS = config("CORS_ALLOW_ALL", default=True, cast=bool)
CORS_ALLOWED_ORIGINS = config(
    "CORS_ALLOWED_ORIGINS",
    default="http://localhost:5173,http://localhost:3000",
    cast=Csv(),
)
CORS_ALLOW_CREDENTIALS = True

# ------------------------------------------------------------------
# Django REST Framework
# ------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
}

# ------------------------------------------------------------------
# URL routing & WSGI
# ------------------------------------------------------------------
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# ------------------------------------------------------------------
# Database
# Locally: SQLite  |  Render: PostgreSQL via DATABASE_URL env var
# ------------------------------------------------------------------
_default_db = f"sqlite:///{BASE_DIR / 'db.sqlite3'}"
DATABASE_URL = config("DATABASE_URL", default=_default_db)

DATABASES = {
    "default": dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# ------------------------------------------------------------------
# Static files (WhiteNoise serves from STATIC_ROOT in production)
# ------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# ------------------------------------------------------------------
# Internationalisation
# ------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ------------------------------------------------------------------
# Default primary key
# ------------------------------------------------------------------
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ------------------------------------------------------------------
# Logging (structured output for Render log drain)
# ------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "render": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "render",
        }
    },
    "root": {
        "handlers": ["console"],
        "level": config("LOG_LEVEL", default="INFO"),
    },
    "loggers": {
        "ingestor": {
            "handlers": ["console"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        }
    },
}
