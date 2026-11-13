import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

#: The key a local stack runs on. It is printed in this repository, so it signs nothing
#: anyone should trust: with it, a session cookie or a password-reset link can be forged
#: by whoever has read this file.
DEV_SECRET_KEY = "dev-only-not-for-production"

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "").strip() or DEV_SECRET_KEY

#: Off unless asked for. The default is what runs wherever nobody set the variable, which
#: is exactly where a debug build does the most damage: DEBUG=True serves a traceback with
#: local variables and settings to anyone who triggers an error. docker-compose.yml sets
#: DJANGO_DEBUG=1 for local development, so the development flow is unaffected.
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"

# With debug off this is an installation someone else can reach, and it starts only on a
# key of its own. docker-compose.yml passes the development key when .env sets none, so
# the check is on the value, and the default local stack (debug on) starts as before.
if not DEBUG and SECRET_KEY == DEV_SECRET_KEY:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be set to a value of this installation's own when "
        "DJANGO_DEBUG is off. Add a long random DJANGO_SECRET_KEY to .env, or set "
        "DJANGO_DEBUG=1 for local development, where the built-in development key is used."
    )

#: Which Host headers this installation answers to, as a comma-separated list.
#:
#: Django compares the request's Host header against this list and rejects anything else
#: with a 400. The check is what stops an attacker-supplied Host header being reflected
#: into password-reset links, absolute URLs and cache keys, so "*" disables a real
#: protection rather than merely relaxing a formality.
#:
#: The default covers local access only. A deployment reachable under any other name must
#: set DJANGO_ALLOWED_HOSTS to its own hostnames, e.g.
#:     DJANGO_ALLOWED_HOSTS=annotations.example.org,10.0.0.7
#: A leading dot matches subdomains (".example.org"). When DEBUG is on, Django additionally
#: allows localhost itself, which is why a misconfigured production host fails loudly here
#: instead of only once DEBUG is turned off.
ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
    if h.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "annotations.apps.AnnotationsConfig",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "annotations"),
        "USER": os.environ.get("POSTGRES_USER", "annotations"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "annotations"),
        "HOST": os.environ.get("POSTGRES_HOST", "db"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticatedOrReadOnly"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
}

# In development Vue runs on 5173 and Django on 8000
CORS_ALLOWED_ORIGINS = os.environ.get(
    "CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
).split(",")
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS

#: Dataset mount point. A path inside the container; Corpus.root_path stores a path
#: underneath it.
DATA_ROOT = os.environ.get("DATA_ROOT", "/data")

LANGUAGE_CODE = "en-us"
#: UTC by default. Timestamps are stored in UTC either way (USE_TZ is on); this decides
#: only how they are rendered, and a server-local default makes a log line or an
#: imported_at mean something different depending on where the process happens to run.
TIME_ZONE = os.environ.get("TZ", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024  # an F0 CSV can run to tens of thousands of rows

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}
