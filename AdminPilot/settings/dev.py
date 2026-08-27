"""
Local development settings.

Selected by default in manage.py. Staging/production separation is deliberately
not built yet — deferred until a deployment target exists
(docs/01_Architecture.md, Architecture Decisions Still Open).
"""

import os

from .base import *  # noqa: F403
from .base import env, env_bool, env_list

DEBUG = env_bool("DEBUG", True)

# Dev-only fallback so the project runs before .env exists. Any real
# deployment reads SECRET_KEY from the environment and this default is
# never reached (prod settings will require it).
SECRET_KEY = env("SECRET_KEY", "dev-only-insecure-key-not-for-deployment")

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "*")

# Azure App Service sets WEBSITE_HOSTNAME automatically
website_hostname = os.environ.get("WEBSITE_HOSTNAME")
if website_hostname and website_hostname not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(website_hostname)
if ".azurewebsites.net" not in ALLOWED_HOSTS and "*" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(".azurewebsites.net")

CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", "")
if website_hostname:
    azure_origin = f"https://{website_hostname}"
    if azure_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(azure_origin)

# Console backend: invite links and password-reset emails print to the terminal.
# No real email backend in V1 (docs/06_CR_Process.md CR-003).
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Log the SQL Server session-context calls and RLS-filtered queries while the
# isolation guarantee is being built and verified (Phase 1).
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "django.db.backends": {
            "handlers": ["console"],
            "level": env("SQL_LOG_LEVEL", "INFO"),
        },
        "core.middleware": {
            "handlers": ["console"],
            "level": "DEBUG",
        },
    },
}
