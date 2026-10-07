"""Django settings, configured by docuconf (see the README's Django section)."""

from pathlib import Path

from mysite.config import Env

env = Env.load_or_exit()
SECRET_KEY = env.secret_key.get_secret_value()
DEBUG = env.debug
ALLOWED_HOSTS = env.allowed_hosts

BASE_DIR = Path(__file__).resolve().parent.parent
INSTALLED_APPS = ["django.contrib.staticfiles"]
ROOT_URLCONF = "mysite.urls"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": env.database_url.get_secret_value().removeprefix("sqlite:///"),
    }
}
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
USE_TZ = True
