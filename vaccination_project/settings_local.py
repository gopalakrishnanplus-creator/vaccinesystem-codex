from pathlib import Path
import os

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env.test")

SECRET_KEY = os.getenv("SECRET_KEY", "local-dev-secret-key")
DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
CSRF_TRUSTED_ORIGINS = ["http://127.0.0.1:8000", "http://localhost:8000"]

PATIENT_DATA_FERNET_KEY = os.getenv("PATIENT_DATA_FERNET_KEY", "").encode()
if not PATIENT_DATA_FERNET_KEY:
    raise ValueError("PATIENT_DATA_FERNET_KEY is required in .env.test")

INSTALLED_APPS = [
    "whitenoise.runserver_nostatic",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "vaccinations.apps.VaccinationsConfig",
]

MIDDLEWARE = [
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "vaccination_project.urls"
WSGI_APPLICATION = "vaccination_project.wsgi.application"
ASGI_APPLICATION = "vaccination_project.asgi.application"

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
            ],
        },
    },
]

db_path = BASE_DIR / "local_test.sqlite3"
sqlite_config = {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": str(db_path),
}

DATABASES = {
    "default": sqlite_config,
    "masters": sqlite_config.copy(),
    "patients": sqlite_config.copy(),
}

# Local testing uses a single SQLite file for all aliases. This router keeps
# migrations on the default connection while allowing relations across the
# local aliases used throughout the codebase.
DATABASE_ROUTERS = ["vaccinations.local_router.LocalSqliteRouter"]

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = 1209600
SESSION_COOKIE_SECURE = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_SAVE_EVERY_REQUEST = True

LOGIN_URL = "/auth/google/start/"
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "local-google-client-id")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "local-google-client-secret")
GOOGLE_OAUTH_REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "http://127.0.0.1:8000/auth/google/callback/")
GOOGLE_OAUTH = {
    "CLIENT_ID": GOOGLE_CLIENT_ID,
    "CLIENT_SECRET": GOOGLE_CLIENT_SECRET,
    "REDIRECT_URI": GOOGLE_OAUTH_REDIRECT_URI,
    "SCOPES": ["openid", "email", "profile"],
}

PHONE_HASH_SALT = os.getenv("PHONE_HASH_SALT", "local-phone-salt")
AUTO_SEND_TO_PARENT_ON_ADD = False
