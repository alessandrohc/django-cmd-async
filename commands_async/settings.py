# coding=utf-8
"""App settings, read from the project once at import time.

The snapshot is part of the contract: the view captures the ignore list and the
permission name as class attributes, and the task decorator is fed the options
dict when the module is first imported. Changing any COMMANDS_ASYNC_* value at
runtime -- including through override_settings -- has no effect until the process
restarts.
"""
from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME

COMMANDS_ASYNC_TASK_OPTIONS = dict(
    expires=60 * 30,  # 30 min
    retries=0
)
COMMANDS_ASYNC_TASK_OPTIONS.update(getattr(settings, 'COMMANDS_ASYNC_TASK_OPTIONS', {}))
COMMANDS_ASYNC_TASK_PRIORITY = COMMANDS_ASYNC_TASK_OPTIONS.pop('priority', 0)

# Whether to display commands in list format
COMMANDS_ASYNC_LIST = getattr(settings, "COMMANDS_ASYNC_LIST", False)
COMMANDS_ASYNC_LOGIN_URL = getattr(settings, "COMMANDS_ASYNC_LOGIN_URL", settings.LOGIN_URL)
COMMANDS_ASYNC_COMMANDS_IGNORE = getattr(settings, "COMMANDS_ASYNC_COMMANDS_IGNORE", [])
COMMANDS_ASYNC_PERMISSION_NAME = getattr(settings, "COMMANDS_ASYNC_PERMISSION_NAME", None)
COMMANDS_ASYNC_REDIRECT_FIELD_NAME = REDIRECT_FIELD_NAME
COMMANDS_ASYNC_WORKERS_STATUS_CHECK = getattr(settings, 'COMMANDS_ASYNC_WORKERS_STATUS_CHECK', False)

# Dono da tarefa na rota status (#8864): as views e a verificação do Django leem estas três DESTE módulo, na hora do uso
# (views.py:84-85, 101, 251 e 303; checks.py:19 e 21), e não as copiam em atributo de classe.
COMMANDS_ASYNC_TASK_OWNERSHIP = getattr(settings, 'COMMANDS_ASYNC_TASK_OWNERSHIP', True)
COMMANDS_ASYNC_OWNER_CACHE_ALIAS = getattr(settings, 'COMMANDS_ASYNC_OWNER_CACHE_ALIAS', 'default')
COMMANDS_ASYNC_OWNER_TIMEOUT = getattr(settings, 'COMMANDS_ASYNC_OWNER_TIMEOUT', 86400)

