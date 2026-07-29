#!/usr/bin/env python
# coding=utf-8
"""Entry point for the django-cmd-async suite, on Django's native test runner.

The package ships no models, so the suite needs no real database engine -- the
sqlite in-memory one in ``test_cmdasync.settings`` exists only for the ``auth``
tables the login-protected views require.

    python runtests.py                          # whole suite
    python runtests.py test_cmdasync.test_views # a single module
    python runtests.py -v 3 --failfast          # runner flags

With coverage:

    coverage run runtests.py && coverage report
"""
import argparse
import os
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the django-cmd-async suite.")
    parser.add_argument('labels', nargs='*', default=None,
                        help="test labels (default: test_cmdasync)")
    parser.add_argument('-v', '--verbosity', type=int, default=2, choices=[0, 1, 2, 3])
    parser.add_argument('--failfast', action='store_true')
    parser.add_argument('--keepdb', action='store_true')
    options = parser.parse_args(argv)

    root = os.path.dirname(os.path.abspath(__file__))
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'test_cmdasync.settings')

    import django
    from django.conf import settings
    from django.test.utils import get_runner

    django.setup()

    runner_class = get_runner(settings)
    runner = runner_class(verbosity=options.verbosity,
                          interactive=False,
                          failfast=options.failfast,
                          keepdb=options.keepdb)
    failures = runner.run_tests(options.labels or ['test_cmdasync'])
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
