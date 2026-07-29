# coding=utf-8
"""Distribution metadata and the app's dependency surface.

These assertions are the ones that keep ``pip install django-cmd-async`` honest:
a wheel that installs but cannot import is exactly the failure mode the package
had before this suite existed.
"""
import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: tomllib arrived in 3.11
    import tomli as tomllib

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / 'commands_async'
PYPROJECT = ROOT / 'pyproject.toml'


def metadata():
    with open(PYPROJECT, 'rb') as handle:
        return tomllib.load(handle)


def requirement_name(specifier):
    """``"Django>=4.2,<6.0"`` -> ``"django"``."""
    return re.split(r'[\s\[<>=!~;]', specifier, maxsplit=1)[0].lower()


class DependencySurfaceTest(SimpleTestCase):
    """What the app imports has to be what the distribution declares."""

    def test_no_module_imports_xadmin(self):
        # The xadmin coupling was a single dead import in adminx.py. Dropping it
        # is what lets the runtime dependencies be PyPI packages only -- xadmin
        # is installed from a git URL, which is not declarable in a publishable
        # wheel.
        offenders = []
        for module in sorted(PACKAGE.rglob('*.py')):
            source = module.read_text(encoding='utf-8')
            if re.search(r'^\s*(from|import)\s+xadmin', source, re.MULTILINE):
                offenders.append(str(module.relative_to(ROOT)))
        self.assertEqual(offenders, [],
                         msg='these modules still depend on xadmin: {0}'.format(offenders))

    def test_django_and_celery_are_declared_as_dependencies(self):
        declared = {requirement_name(item)
                    for item in metadata()['project']['dependencies']}
        self.assertLessEqual({'django', 'celery'}, declared)

    def test_every_dependency_states_a_floor_and_a_ceiling(self):
        for specifier in metadata()['project']['dependencies']:
            with self.subTest(dependency=specifier):
                self.assertIn('>=', specifier,
                              msg='no minimum version declared')
                self.assertIn('<', specifier,
                              msg='no upper bound: the next major installs unannounced')


class BuildMetadataTest(SimpleTestCase):
    """PEP 517/518/621: a declared backend and declarative metadata."""

    def test_a_build_backend_is_declared(self):
        self.assertEqual(metadata()['build-system']['build-backend'],
                         'setuptools.build_meta')

    def test_setup_py_is_gone(self):
        # Two places to bump a version is one too many.
        self.assertFalse((ROOT / 'setup.py').exists(),
                         msg='setup.py still exists next to pyproject.toml')

    def test_the_version_lives_only_in_pyproject(self):
        version = metadata()['project']['version']
        self.assertRegex(version, r'^\d+\.\d+\.\d+$')

    def test_the_distribution_name_is_unchanged(self):
        self.assertEqual(metadata()['project']['name'], 'django-cmd-async')

    def test_the_readme_and_license_are_declared(self):
        project = metadata()['project']
        self.assertIn('readme', project)
        self.assertEqual(project['license'], 'MIT')
        self.assertTrue(project['license-files'])

    def test_the_supported_interpreters_are_declared(self):
        self.assertEqual(metadata()['project']['requires-python'], '>=3.10')


class ClassifiersTest(SimpleTestCase):
    """The classifiers are the public claim about what this runs on."""

    def test_the_validated_django_versions_are_claimed(self):
        classifiers = metadata()['project']['classifiers']
        for version in ('4.2', '5.0', '5.1', '5.2'):
            with self.subTest(django=version):
                self.assertIn('Framework :: Django :: {0}'.format(version), classifiers)

    def test_the_validated_python_versions_are_claimed(self):
        classifiers = metadata()['project']['classifiers']
        for version in ('3.10', '3.11', '3.12', '3.13'):
            with self.subTest(python=version):
                self.assertIn('Programming Language :: Python :: {0}'.format(version),
                              classifiers)

    def test_no_python_2_claim_survives(self):
        for classifier in metadata()['project']['classifiers']:
            self.assertNotIn('Python :: 2', classifier)
