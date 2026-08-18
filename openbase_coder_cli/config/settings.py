"""
Django settings for openbase_coder_cli.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/topics/settings/
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Data directory for SQLite database and other persistent data
DATA_DIR = Path(
    os.environ.get("OPENBASE_CODER_CLI_DATA_DIR", Path.home() / ".openbase")
)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get("OPENBASE_CODER_CLI_SECRET_KEY", "")
if not SECRET_KEY:
    raise ValueError(
        "SECRET_KEY is not set. Run the server command to auto-generate one, "
        "or set OPENBASE_CODER_CLI_SECRET_KEY environment variable."
    )

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get("OPENBASE_CODER_CLI_DEBUG", "false").lower() == "true"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        "OPENBASE_CODER_CLI_ALLOWED_HOSTS", "localhost,127.0.0.1"
    ).split(",")
    if host.strip()
]
if livekit_node_ip := os.environ.get("LIVEKIT_NODE_IP"):
    ALLOWED_HOSTS.append(livekit_node_ip)

# Web backend URL for JWT validation (JWKS endpoint)
WEB_BACKEND_URL = os.environ.get(
    "OPENBASE_CODER_CLI_WEB_BACKEND_URL", "https://app.openbase.cloud"
)
JWT_JWKS_URL = os.environ.get(
    "OPENBASE_CODER_CLI_JWT_JWKS_URL",
    f"{WEB_BACKEND_URL.rstrip('/')}/.well-known/jwks.json",
)
JWT_AUTH_SESSION_URL = os.environ.get(
    "OPENBASE_CODER_CLI_JWT_AUTH_SESSION_URL",
    f"{WEB_BACKEND_URL.rstrip('/')}/_allauth/app/v1/auth/session",
)
JWT_ISSUER = os.environ.get("OPENBASE_CODER_CLI_JWT_ISSUER", WEB_BACKEND_URL).rstrip(
    "/"
)
JWT_AUDIENCE = os.environ.get("OPENBASE_CODER_CLI_JWT_AUDIENCE", "openbase-coder-cli")

# Console build directory (built React app)
_console_build_dir_env = os.environ.get("OPENBASE_CODER_CLI_CONSOLE_BUILD_DIR")
if _console_build_dir_env:
    CONSOLE_BUILD_DIR: Path | None = Path(_console_build_dir_env)
else:
    from openbase_coder_cli.runtime import packaged_console_build_dir
    from openbase_coder_cli.services.installation import InstallationConfig

    if InstallationConfig.exists():
        _install_config = InstallationConfig.load()
        if _install_config.workspace_path:
            CONSOLE_BUILD_DIR = (
                Path(_install_config.workspace_path) / "console" / "dist"
            )
        else:
            CONSOLE_BUILD_DIR = packaged_console_build_dir()
    else:
        CONSOLE_BUILD_DIR = packaged_console_build_dir()


# Application definition

INSTALLED_APPS = [
    # No admin/sessions/messages: the server is a JSON API + websockets behind
    # JWT/machine-token auth. contrib.auth stays because JWTAuthentication
    # mints per-sub User rows (config/authentication.py).
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    # Third-party apps
    "channels",
    "rest_framework",
    "corsheaders",
    # Local apps
    "openbase_coder_cli.openbase_coder_cli_app",
]

# No session/auth/message/CSRF middleware: auth is stateless JWT/machine
# tokens (DRF resolves request.user itself; no cookies, so CSRF protects
# nothing here).
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "openbase_coder_cli.config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "openbase_coder_cli.config.wsgi.application"
ASGI_APPLICATION = "openbase_coder_cli.config.asgi.application"


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
    }
}


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = "static/"
STATIC_ROOT = DATA_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Django REST Framework configuration

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "openbase_coder_cli.config.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
}


# CORS configuration (for local development)

CORS_ALLOWED_ORIGINS = os.environ.get(
    "OPENBASE_CODER_CLI_CORS_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080"
).split(",")

CORS_ALLOW_CREDENTIALS = True


# Channels (WebSocket) configuration

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    },
}


# Logging configuration

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": os.environ.get("OPENBASE_CODER_CLI_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}
