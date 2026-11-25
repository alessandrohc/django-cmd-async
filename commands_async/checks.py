# coding=utf-8
"""System check of the cache that keeps the owner of each launched task (#8864)."""
from django.core import checks
from django.core.cache import InvalidCacheBackendError, caches
from django.core.cache.backends.dummy import DummyCache

from . import settings


@checks.register()
def check_owner_cache(app_configs, **kwargs):
    """Warns when the owner of the launched tasks has nowhere to be kept.

    With the owner check on, the status route answers 404 to a user who has no record of the task. A cache alias that is not
    in ``CACHES``, or that is a ``DummyCache`` (the host's page cache with the cache switched off), keeps nothing, so the
    user who launched a task would be answered 404 on his own task. The local-memory cache does not warn: it serves a
    single-process development server (the README says it is not shared between processes).
    """
    if not settings.COMMANDS_ASYNC_TASK_OWNERSHIP:
        return []
    alias = settings.COMMANDS_ASYNC_OWNER_CACHE_ALIAS
    try:
        cache = caches[alias]
    except InvalidCacheBackendError:
        problem = 'is not in CACHES'
    else:
        if not isinstance(cache, DummyCache):
            return []
        problem = 'is a DummyCache, which keeps nothing'
    return [checks.Warning(
        'COMMANDS_ASYNC_OWNER_CACHE_ALIAS names the cache alias {0!r}, which {1}: the user who launches a task would be '
        'answered 404 on his own task.'.format(alias, problem),
        hint='Point COMMANDS_ASYNC_OWNER_CACHE_ALIAS to a cache that the processes share and nobody clears, '
             'or set COMMANDS_ASYNC_TASK_OWNERSHIP to False.',
        id='commands_async.W001',
    )]
