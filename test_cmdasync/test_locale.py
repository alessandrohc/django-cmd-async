# coding=utf-8
"""The shipped ``.mo`` files have to match the ``.po`` they were compiled from.

Compiled catalogs are binary and get committed, so they drift silently. Bytes are
not comparable -- msgfmt >= 0.19 drops ``POT-Creation-Date`` from the header, and
untranslated entries never make it into a ``.mo`` at all -- so this compares the
messages plus ``Plural-Forms``.
"""
import gettext
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from django.test import SimpleTestCase

LOCALE_DIR = Path(__file__).resolve().parent.parent / 'commands_async' / 'locale'

HAS_MSGFMT = shutil.which('msgfmt') is not None


def read_catalog(mo_path):
    """Return ``(header, messages)`` for a compiled catalog."""
    with open(mo_path, 'rb') as handle:
        translation = gettext.GNUTranslations(handle)
    messages = {key: value for key, value in translation._catalog.items() if key != ''}
    return translation._info, messages


@unittest.skipUnless(HAS_MSGFMT, 'gettext msgfmt is not installed')
class CompiledCatalogTest(SimpleTestCase):
    """Every ``.po`` in the package compiles to exactly what is shipped."""

    def test_a_catalog_ships_for_every_source_po(self):
        for po_path in sorted(LOCALE_DIR.glob('*/LC_MESSAGES/django.po')):
            with self.subTest(locale=po_path.parent.parent.name):
                self.assertTrue(po_path.with_suffix('.mo').exists(),
                                msg='{0} has no compiled .mo'.format(po_path))

    def test_the_shipped_catalog_matches_a_fresh_compile(self):
        po_paths = sorted(LOCALE_DIR.glob('*/LC_MESSAGES/django.po'))
        self.assertTrue(po_paths, msg='no .po found -- the glob is wrong')

        for po_path in po_paths:
            locale = po_path.parent.parent.name
            with self.subTest(locale=locale), tempfile.TemporaryDirectory() as tmp:
                fresh_path = Path(tmp) / 'django.mo'
                subprocess.run(['msgfmt', '-o', str(fresh_path), str(po_path)],
                               check=True)

                shipped_header, shipped = read_catalog(po_path.with_suffix('.mo'))
                fresh_header, expected = read_catalog(fresh_path)

                self.assertEqual(shipped, expected,
                                 msg='{0}: the .mo is stale, recompile it'.format(locale))
                self.assertEqual(shipped_header.get('plural-forms'),
                                 fresh_header.get('plural-forms'),
                                 msg='{0}: Plural-Forms drifted'.format(locale))


class CatalogDiscoveryTest(SimpleTestCase):
    """The catalog has to be reachable from inside the installed app.

    The compare above only proves the ``.mo`` matches its ``.po``; it says nothing
    about Django finding it. A catalog in the wrong directory, or an app not listed
    in INSTALLED_APPS, fails exactly here.
    """

    def test_a_shipped_string_is_translated_under_pt_br(self):
        from django.utils import translation

        with translation.override('pt-br'):
            self.assertEqual(translation.gettext('Execute'), 'Executar')

    def test_an_untranslated_string_falls_back_to_the_msgid(self):
        from django.utils import translation

        # 'args' is left untranslated on purpose -- it is CLI terminology, spelled
        # the same in both languages.
        with translation.override('pt-br'):
            self.assertEqual(translation.gettext('args'), 'args')


@unittest.skipUnless(HAS_MSGFMT, 'gettext msgfmt is not installed')
class CatalogSyntaxTest(SimpleTestCase):
    """A ``.po`` that msgfmt rejects would leave the app untranslated."""

    def test_every_po_passes_the_msgfmt_check(self):
        for po_path in sorted(LOCALE_DIR.glob('*/LC_MESSAGES/django.po')):
            with self.subTest(locale=po_path.parent.parent.name):
                result = subprocess.run(['msgfmt', '--check-format', '-o', '/dev/null',
                                         str(po_path)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, msg=result.stderr)
