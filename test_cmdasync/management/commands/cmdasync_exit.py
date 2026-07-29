# coding=utf-8
"""Test-only command: writes to stdout and then raises ``SystemExit``.

``command_exec`` swallows ``SystemExit`` when the code is 0 and re-raises it
otherwise; this command makes both halves of that rule reachable from a test.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Raise SystemExit with the given code (test only)."

    def add_arguments(self, parser):
        parser.add_argument('--code', dest='code', type=int, default=0)

    def handle(self, *args, **options):
        self.stdout.write('exiting with {0}'.format(options['code']))
        raise SystemExit(options['code'])
