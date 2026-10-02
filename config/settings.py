import os
from datetime import timedelta
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def env_list(name, default=""):
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or "dev-insecure-secret-key-change-me-in-production-please"
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_filters",
    "corsheaders",
    "drf_spectacular",
    "accounts",
    "workspaces",
    "tasks",
    "integrations",
    "concierge",
    "instance",
    "mcpserver",
    "mcpauth",
    "timetracking",
    "calendars",
    "payments",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

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
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=60
    )
}

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "accounts.tokens.ApiTokenAuthentication",
        "accounts.tokens.OAuthApiAuthentication",
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    "ROTATE_REFRESH_TOKENS": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Flowly API",
    "DESCRIPTION": "Multi-project, multi-user daily task planning with swimlanes.",
    "VERSION": "1.0.0",
}

# Needed for the Django admin when it's served through the frontend's /admin proxy.
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS", "http://localhost:4200,http://localhost:8080"
)

# Public base URL of the app (frontend origin; the API is at <PUBLIC_URL>/api). Used for links in
# notifications and for GitHub App callbacks. Falls back to the request's origin when empty.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

# Integrations
GITHUB_API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com")
GITHUB_WEB_URL = os.environ.get("GITHUB_WEB_URL", "https://github.com")
# Domain whose addresses are routed to Flowly's email-in endpoint, e.g. "in.example.com".
EMAIL_INBOUND_DOMAIN = os.environ.get("EMAIL_INBOUND_DOMAIN", "")
# Optional shared secret the inbound email provider must send (?key=... or X-Flowly-Key header).
EMAIL_INBOUND_KEY = os.environ.get("EMAIL_INBOUND_KEY", "")
# Outgoing webhooks may not target private/loopback addresses unless this is enabled.
INTEGRATIONS_ALLOW_PRIVATE_URLS = os.environ.get("INTEGRATIONS_ALLOW_PRIVATE_URLS", "0") == "1"

# File storage. Uploaded attachments go to an S3-compatible bucket (AWS S3, Cloudflare R2, MinIO…);
# without S3_BUCKET, uploads are disabled and only links / cloud documents can be attached.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "")
S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", "")
S3_REGION = os.environ.get("S3_REGION", "")
S3_ADDRESSING_STYLE = os.environ.get("S3_ADDRESSING_STYLE", "")
ATTACHMENT_MAX_MB = int(os.environ.get("ATTACHMENT_MAX_MB", "25"))
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# Cloud document pickers (all optional; exposed to the browser through /api/config/).
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_APP_ID = os.environ.get("GOOGLE_APP_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
SLACK_CLIENT_ID = os.environ.get("SLACK_CLIENT_ID", "")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_PRO_MONTH = os.environ.get("STRIPE_PRICE_PRO_MONTH", "")
STRIPE_PRICE_PRO_YEAR = os.environ.get("STRIPE_PRICE_PRO_YEAR", "")
STRIPE_PRODUCT_ENTERPRISE = os.environ.get("STRIPE_PRODUCT_ENTERPRISE", "")
STRIPE_PORTAL_CONFIG = os.environ.get("STRIPE_PORTAL_CONFIG", "")
STRIPE_AUTOMATIC_TAX = os.environ.get("STRIPE_AUTOMATIC_TAX", "1") == "1"
SLACK_CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
MICROSOFT_CLIENT_ID = os.environ.get("MICROSOFT_CLIENT_ID", "")

# LLM: the platform OpenRouter key serves Pro users; workspaces can bring their own provider.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openrouter/auto")
OPENROUTER_VISION_MODEL = os.environ.get("OPENROUTER_VISION_MODEL", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# Daily token budget per user on the platform key (BYO providers aren't capped).
CONCIERGE_DAILY_TOKENS = int(os.environ.get("CONCIERGE_DAILY_TOKENS", "300000"))

# Telegram concierge bot. "polling" works without a public HTTPS URL; "webhook" needs PUBLIC API URL.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_MODE = os.environ.get("TELEGRAM_MODE", "polling")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
TELEGRAM_API_URL = os.environ.get("TELEGRAM_API_URL", "https://api.telegram.org")
# Public URL of the API itself (webhooks), e.g. https://api.flowly.net. Defaults to PUBLIC_URL.
API_PUBLIC_URL = os.environ.get("API_PUBLIC_URL", "").rstrip("/") or PUBLIC_URL

# Log errors (with tracebacks) to the container output; Django's default only emails admins.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "flowly": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
