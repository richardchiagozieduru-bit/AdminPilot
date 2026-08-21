"""
Settings shared by every environment.

Nothing environment-specific belongs here — no secrets, no database
credentials, no DEBUG. Those live in dev.py / (later) prod.py, read from
the environment. See docs/07_Implementation_Roadmap.md Phase 0.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# settings/base.py -> AdminPilot/ -> repo root
BASE_DIR = Path(__file__).resolve().parent.parent.parent

load_dotenv(BASE_DIR / ".env")


def env(name, default=None, required=False):
    """Read an environment variable, failing loudly when one is required.

    A variable present but blank counts as unset. `.env` ships with its keys
    listed and empty (`SECRET_KEY=`), so `os.environ.get` finds `""` and a
    caller's default is never reached — which silently defeats every dev-only
    fallback in dev.py. `required` already read blank as missing; this makes the
    default path agree with it.

    Callers that want blank to mean something pass "" as the default and get it
    back either way — DB_USER/DB_PASSWORD do exactly that, where empty is the
    signal for Windows integrated auth.
    """
    value = os.environ.get(name, default)
    if value is None or value == "":
        value = default
    if required and not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def env_bool(name, default=False):
    return env(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


# --- Applications ---------------------------------------------------------
#
# django.contrib.admin is deliberately absent (docs/06_CR_Process.md CR-004):
# pointed at RLS-protected tables it needs session-context plumbing built
# solely for its benefit, and nothing in the specs uses it.

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

# Seven apps mirroring the module boundaries in docs/03_Views_and_Endpoints.md
# (docs/01_Architecture.md ADR-003). "platform" shadows the stdlib module name,
# so it is imported as a package under the project, never bare.
LOCAL_APPS = [
    "core",
    "accounts",
    "platform_admin",
    "academic",
    "students",
    "billing",
    "reports",
]

INSTALLED_APPS = LOCAL_APPS + DJANGO_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Sets SESSION_CONTEXT for SQL Server RLS on every request. Must come
    # after AuthenticationMiddleware (needs request.user) and before any
    # middleware that queries tenant-scoped tables.
    # docs/01_Architecture.md — highest-risk detail in the platform.
    "core.middleware.TenantContextMiddleware",
]

ROOT_URLCONF = "AdminPilot.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # Sidebar entries filtered by role, server-side. The sidebar is
                # built from core.permissions so it cannot disagree with the
                # gate on the views themselves (docs/08_UI_UX.md).
                "core.context_processors.navigation",
            ],
        },
    },
]

WSGI_APPLICATION = "AdminPilot.wsgi.application"
ASGI_APPLICATION = "AdminPilot.asgi.application"


# --- Database -------------------------------------------------------------
#
# SQL Server via mssql-django. Row-Level Security policies are applied by a
# hand-written RunSQL migration in core/ — they do not come out of models.py
# (docs/02_Database.md).

# Windows Authentication is the default for local dev: leave DB_USER and
# DB_PASSWORD empty and mssql-django adds Trusted_Connection=yes to the
# connection string, so the logged-in Windows account is used. Setting both
# switches to SQL Server authentication without any other change here.
DB_USER = env("DB_USER", "")
DB_PASSWORD = env("DB_PASSWORD", "")

DATABASES = {
    "default": {
        "ENGINE": "mssql",
        "NAME": env("DB_NAME", "adminpilot_dev"),
        # Named instance — SERVER=HOST\INSTANCE, resolved by the SQL Browser
        # service, which is why DB_PORT stays empty. A host/port pair would
        # set DB_PORT instead and drop the instance suffix.
        "HOST": env("DB_HOST", r"O2JUNE\SQLEXPRESS"),
        "PORT": env("DB_PORT", ""),
        "USER": DB_USER,
        "PASSWORD": DB_PASSWORD,
        "OPTIONS": {
            "driver": env("DB_DRIVER", "ODBC Driver 17 for SQL Server"),
            "extra_params": env("DB_EXTRA_PARAMS", "TrustServerCertificate=yes"),
        },
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- Authentication -------------------------------------------------------

AUTH_USER_MODEL = "accounts.User"

# Two backends because Super Admins are a different model, not a role (CR-004).
# Order matters: an institution login attempt should not walk the platform
# table first. Neither backend is Django's ModelBackend — the institution one
# needs the RLS auth-lookup exemption around every read of `users`, which
# ModelBackend knows nothing about (accounts/backends.py).
AUTHENTICATION_BACKENDS = [
    "accounts.backends.InstitutionUserBackend",
    "platform_admin.backends.PlatformUserBackend",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"


# --- Internationalization -------------------------------------------------
#
# Per-institution timezone is stored on the Institution row and applied per
# request; this is only the platform default (docs/02_Database.md).

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# --- Static files ---------------------------------------------------------

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
