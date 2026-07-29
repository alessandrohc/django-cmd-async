# coding=utf-8
"""The ``COMMANDS_ASYNC_*`` settings: defaults and how a project overrides them.

``commands_async.settings`` reads the project settings once, at import time, so
``override_settings`` alone has no effect on it -- the module has to be reloaded.
That snapshot behaviour is part of the contract (the view captures the ignore
list as a class attribute), so it is asserted rather than worked around.
"""
import importlib

from django.contrib.auth import REDIRECT_FIELD_NAME
from django.test import SimpleTestCase, TestCase, override_settings

from commands_async import settings as app_settings


class DefaultsTest(SimpleTestCase):
    """What a project gets without configuring anything."""

    def test_task_options_default_to_a_thirty_minute_expiry_without_retries(self):
        self.assertEqual(app_settings.COMMANDS_ASYNC_TASK_OPTIONS,
                         {'expires': 60 * 30, 'retries': 0})

    def test_task_priority_defaults_to_zero(self):
        self.assertEqual(app_settings.COMMANDS_ASYNC_TASK_PRIORITY, 0)

    def test_the_command_dropdown_is_off_by_default(self):
        self.assertFalse(app_settings.COMMANDS_ASYNC_LIST)

    def test_the_worker_probe_is_off_by_default(self):
        self.assertFalse(app_settings.COMMANDS_ASYNC_WORKERS_STATUS_CHECK)

    def test_no_command_is_ignored_by_default(self):
        self.assertEqual(app_settings.COMMANDS_ASYNC_COMMANDS_IGNORE, [])

    def test_no_permission_is_required_by_default(self):
        self.assertIsNone(app_settings.COMMANDS_ASYNC_PERMISSION_NAME)

    def test_the_login_url_falls_back_to_the_project_one(self):
        from django.conf import settings

        self.assertEqual(app_settings.COMMANDS_ASYNC_LOGIN_URL, settings.LOGIN_URL)

    def test_the_redirect_field_follows_django(self):
        self.assertEqual(app_settings.COMMANDS_ASYNC_REDIRECT_FIELD_NAME,
                         REDIRECT_FIELD_NAME)


class OverrideTest(TestCase):
    """A project's values are merged over the defaults, not swapped in."""

    def tearDown(self):
        # Restore the module for whatever runs next -- reload mutates it in place.
        importlib.reload(app_settings)

    def test_task_options_are_merged_over_the_defaults(self):
        with override_settings(COMMANDS_ASYNC_TASK_OPTIONS={'expires': 10}):
            importlib.reload(app_settings)
            self.assertEqual(app_settings.COMMANDS_ASYNC_TASK_OPTIONS,
                             {'expires': 10, 'retries': 0})

    def test_priority_is_taken_out_of_the_task_options(self):
        # apply_async takes priority as its own argument, so it must not be left
        # in the dict that is splatted onto the task decorator.
        with override_settings(COMMANDS_ASYNC_TASK_OPTIONS={'priority': 7}):
            importlib.reload(app_settings)
            self.assertEqual(app_settings.COMMANDS_ASYNC_TASK_PRIORITY, 7)
            self.assertNotIn('priority', app_settings.COMMANDS_ASYNC_TASK_OPTIONS)

    def test_the_login_url_can_be_pointed_somewhere_else(self):
        with override_settings(COMMANDS_ASYNC_LOGIN_URL='/elsewhere/'):
            importlib.reload(app_settings)
            self.assertEqual(app_settings.COMMANDS_ASYNC_LOGIN_URL, '/elsewhere/')

    def test_ignored_commands_come_from_the_project(self):
        with override_settings(COMMANDS_ASYNC_COMMANDS_IGNORE=['runserver', 'shell']):
            importlib.reload(app_settings)
            self.assertEqual(app_settings.COMMANDS_ASYNC_COMMANDS_IGNORE,
                             ['runserver', 'shell'])
