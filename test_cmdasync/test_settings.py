# coding=utf-8
"""The ``COMMANDS_ASYNC_*`` settings: defaults and how a project overrides them.

``commands_async.settings`` reads the project settings once, at import time, so
``override_settings`` alone has no effect on it -- the module has to be reloaded.
That snapshot behaviour is part of the contract (the view captures the ignore
list as a class attribute), so it is asserted rather than worked around.
"""
import importlib
import importlib.util
from unittest import mock

from django.contrib.auth import REDIRECT_FIELD_NAME
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from commands_async import settings as app_settings
from test_cmdasync.harness import AJAX, async_result, create_user, grant, status_url


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


def views_read_afresh():
    """``commands_async/views.py`` executed again, under another name, against the settings as they stand now.

    The view classes capture ``COMMANDS_ASYNC_PERMISSION_NAME`` in their bodies, once, at import time
    (views.py, ``CommandPermissionMixin``), so the link between the setting and the views only shows when the module
    runs again. The real ``commands_async.views`` stays as it is, because the URLconf and the other tests hold its
    classes: the copy is never put in ``sys.modules``.
    """
    from commands_async import views

    spec = importlib.util.spec_from_file_location('commands_async.views_afresh', views.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PermissionNameTest(TestCase):
    """The permission the status view and the form ask for is the one the project names in the setting.

    The gate tests elsewhere swap ``command_permission_name`` on the class (they name a permission without going
    through the setting), so nothing else reads ``COMMANDS_ASYNC_PERMISSION_NAME`` into the views.
    """

    PERMISSION_CODENAME = 'run_commands'

    def tearDown(self):
        # Restore the settings module for whatever runs next -- reload mutates it in place.
        importlib.reload(app_settings)

    def views_with_the_permission_named(self, permission):
        with override_settings(COMMANDS_ASYNC_PERMISSION_NAME=permission):
            importlib.reload(app_settings)
            return views_read_afresh()

    def test_the_status_view_and_the_form_ask_for_the_permission_the_project_names(self):
        """AC-8836-4, AC-8836-5, AC-8836-14, AC-8836-15: the named permission reaches the status view and the form."""
        unnamed = views_read_afresh()
        self.assertIsNone(unnamed.TaskFormStatus.command_permission_name,
                          msg='precondition: the copy reads the settings as they stand, and nothing is named')

        views = self.views_with_the_permission_named('auth.run_commands')

        self.assertEqual(views.TaskFormStatus.command_permission_name, 'auth.run_commands')
        self.assertEqual(views.TaskFormView.command_permission_name, 'auth.run_commands')

    def test_a_user_without_the_named_permission_is_refused_by_the_status_view_and_the_form(self):
        """AC-8836-4, AC-8836-5, AC-8836-14, AC-8836-15: with the permission named in the settings, only its holder passes."""
        runner = create_user('runner')
        plain = create_user('plain')
        permission = grant(runner, self.PERMISSION_CODENAME)
        views = self.views_with_the_permission_named(permission)

        def request_by(user, method='get', **extra):
            request = getattr(RequestFactory(), method)(status_url('abc'), **extra)
            request.user = user
            return request

        with mock.patch.object(views, 'AsyncResult', return_value=async_result(task_id='abc')), \
                mock.patch.object(views, 'celery_app') as celery_app:
            status = views.TaskFormStatus.as_view()
            for label, extra in (('navigation', {}), ('xhr', AJAX)):
                with self.subTest(request=label):
                    self.assertEqual(status(request_by(runner, **extra), task_id='abc').status_code, 200,
                                     msg='precondition: the holder reads the task')
                    self.assertEqual(status(request_by(plain, **extra), task_id='abc').status_code, 403)

                    status(request_by(runner, 'post', **extra), task_id='abc')
                    celery_app.control.revoke.assert_called_once_with('abc', terminate=True)
                    celery_app.control.revoke.reset_mock()
                    self.assertEqual(status(request_by(plain, 'post', **extra), task_id='abc').status_code, 403)
                    celery_app.control.revoke.assert_not_called()

        for user, allowed in ((runner, True), (plain, False)):
            with self.subTest(form_for=user.username):
                form = views.TaskFormView()
                form.request = request_by(user)
                self.assertIs(form.has_permission(), allowed)
