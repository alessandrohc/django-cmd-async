# coding=utf-8
"""Mirror of the host project's CSP nonce context processor.

The package does not ship one: it consumes whatever the project exposes as
``csp_nonce``. This stands in for that so the suite can assert the templates pick
the value up. In the real project the value is a constant placeholder that a
later middleware swaps for a fresh nonce, which is why a fixed string here is a
faithful stand-in rather than a simplification.
"""
NONCE = 'test-nonce-placeholder'


def csp_nonce(request):
    return {'csp_nonce': NONCE}
