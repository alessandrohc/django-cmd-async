# coding=utf-8
"""Test-only command: echoes back what it was called with.

Exists so the suite can assert on captured output without depending on the
wording of a real Django command, which changes between releases and is
translated.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Echo the positional arguments and options received (test only)."

    def add_arguments(self, parser):
        parser.add_argument('words', nargs='*')
        parser.add_argument('--tag', dest='tag', default='')

    def handle(self, *args, **options):
        self.stdout.write('words={0}'.format(list(options['words'])))
        self.stdout.write('tag={0}'.format(options['tag']))
