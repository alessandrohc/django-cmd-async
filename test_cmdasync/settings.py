# coding=utf-8
"""Minimal settings for the django-cmd-async suite.

``test_cmdasync`` itself is installed as an app: it ships the throwaway
management commands (``cmdasync_echo``, ``cmdasync_exit``) that let the suite
assert on command discovery and on task output without depending on the text of
any real Django command.
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Fixed key: this is a test suite, there is nothing here to protect.
SECRET_KEY = 'django-cmd-async-test-only-not-a-secret'
DEBUG = False
ALLOWED_HOSTS = ['testserver', 'localhost', '127.0.0.1']

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'commands_async',
    'test_cmdasync',
]

MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
]

# The app's templates are Django Template Language, not Jinja2, and they rely on
# APP_DIRS to be found at all.
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                # Same slot the host project fills (plus/settings.py) -- the
                # templates stamp whatever it exposes onto every <script>.
                'test_cmdasync.context_processors.csp_nonce',
            ],
        },
    },
]

ROOT_URLCONF = 'test_cmdasync.urls'

STATIC_URL = '/static/'

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Read by commands_async.settings at import time, which is also why it is stated
# here instead of relying on the Django default: the redirect target of the
# login-protected views is asserted against it.
LOGIN_URL = '/login/'
