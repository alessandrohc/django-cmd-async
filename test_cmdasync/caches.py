# coding=utf-8
"""Caches de teste do registro do dono da tarefa (#8864).

O registro vive num cache do Django, e dois defeitos do cache real mudam o que o dono vê: o cache que levanta erro
(o Redis fora do ar, no dev) e o que descarta a gravação sem erro (o `IGNORE_EXCEPTIONS` do alias Redis do `plus_base`,
`plus/utils/redis.py:271`). Cada classe é um alias pronto para o `override_settings(CACHES=…)`.
"""
from django.core.cache.backends.locmem import LocMemCache


class DroppingCache(LocMemCache):
    """Cache que descarta toda gravação sem levantar erro: o `get` volta sempre vazio."""

    def set(self, *args, **kwargs):
        return None

    def add(self, *args, **kwargs):
        return False

    def set_many(self, *args, **kwargs):
        return []

    def touch(self, *args, **kwargs):
        return False


class RaisingCache(LocMemCache):
    """Cache que levanta erro em toda leitura e gravação, como o Redis fora do ar."""

    def _down(self, *args, **kwargs):
        raise ConnectionError('owner registry down')

    get = set = add = get_many = set_many = delete = has_key = touch = incr = _down
