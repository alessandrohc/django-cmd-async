# coding=utf-8
"""The three views: the form page, the task status poller and the worker probe.

The broker and the result backend are always replaced -- see
``test_cmdasync.harness``. What is asserted here is the JSON contract the bundled
JavaScript consumes, since that is the actual public interface of these views.
"""
import json
from unittest import mock

from django.test import TestCase

from commands_async.views import TaskFormView

from test_cmdasync.harness import (AJAX, INDEX_URL, WORKERS_URL, async_result,
                                   create_user, grant, queued_task, status_url)

# The two throwaway commands shipped by the test app, which is what makes the
# command listing assertions independent of the Django version under test.
TEST_APP = 'test_cmdasync'
ECHO = 'cmdasync_echo'
EXIT = 'cmdasync_exit'


def payload(app_command='test_cmdasync.cmdasync_echo', args='', kwargs=''):
    return {'app_command': app_command, 'args': args, 'kwargs': kwargs}


class TaskFormViewGetTest(TestCase):
    """The form page: which commands it offers and how they are grouped."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user()

    def setUp(self):
        self.client.force_login(self.user)

    def test_get_renders_the_index_template(self):
        response = self.client.get(INDEX_URL)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'cmdasync/index.html')

    def test_get_groups_commands_by_app_sorted_by_name(self):
        response = self.client.get(INDEX_URL)
        self.assertEqual(response.context['commands'][TEST_APP], [ECHO, EXIT])

    def test_get_exposes_the_app_settings_to_the_template(self):
        # The template branches on COMMANDS_ASYNC_LIST and builds the login
        # redirect from COMMANDS_ASYNC_*, so the module has to reach the context.
        response = self.client.get(INDEX_URL)
        self.assertTrue(hasattr(response.context['settings'], 'COMMANDS_ASYNC_LOGIN_URL'))

    def test_get_omits_a_command_ignored_by_its_short_name(self):
        with mock.patch.object(TaskFormView, 'commands_skip', [ECHO]):
            response = self.client.get(INDEX_URL)
        self.assertEqual(response.context['commands'][TEST_APP], [EXIT])

    def test_get_omits_a_command_ignored_by_its_dotted_name(self):
        with mock.patch.object(TaskFormView, 'commands_skip',
                               ['{0}.{1}'.format(TEST_APP, ECHO)]):
            response = self.client.get(INDEX_URL)
        self.assertEqual(response.context['commands'][TEST_APP], [EXIT])


class TaskFormViewPostTest(TestCase):
    """Submitting the form: what reaches the broker, and what comes back."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user()

    def setUp(self):
        self.client.force_login(self.user)

    def test_valid_submit_returns_the_queued_task_id_and_command_name(self):
        with mock.patch('commands_async.views.command_exec') as task:
            task.apply_async.return_value = queued_task('queued-1')
            response = self.client.post(INDEX_URL, payload(), **AJAX)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content),
                         {'task': {'id': 'queued-1', 'command': {'name': ECHO}}})

    def test_valid_submit_forwards_command_args_and_kwargs_to_the_broker(self):
        with mock.patch('commands_async.views.command_exec') as task:
            task.apply_async.return_value = queued_task()
            self.client.post(INDEX_URL,
                             payload(args='"first", "second"', kwargs='"tag": "x"'),
                             **AJAX)

        task.apply_async.assert_called_once_with(args=(ECHO, 'first', 'second'),
                                                kwargs={'tag': 'x'},
                                                priority=0)

    def test_submit_with_unparsable_args_returns_a_field_error(self):
        with mock.patch('commands_async.views.command_exec') as task:
            response = self.client.post(INDEX_URL, payload(args='"unterminated'), **AJAX)

        self.assertEqual(response.status_code, 400)
        self.assertIn('args', json.loads(response.content)['form']['errors'])
        task.apply_async.assert_not_called()

    def test_submit_with_unparsable_kwargs_returns_a_field_error(self):
        with mock.patch('commands_async.views.command_exec') as task:
            response = self.client.post(INDEX_URL, payload(kwargs='"tag": '), **AJAX)

        self.assertEqual(response.status_code, 400)
        self.assertIn('kwargs', json.loads(response.content)['form']['errors'])
        task.apply_async.assert_not_called()

    def test_broker_failure_returns_a_field_error_and_is_logged(self):
        with mock.patch('commands_async.views.logger') as logger, \
                mock.patch('commands_async.views.command_exec') as task:
            task.apply_async.side_effect = RuntimeError('broker is down')
            response = self.client.post(INDEX_URL, payload(), **AJAX)

        self.assertEqual(response.status_code, 400)
        self.assertIn('app_command', json.loads(response.content)['form']['errors'])
        self.assertTrue(logger.exception.called,
                        msg='a broker failure must leave a traceback behind')


class TaskFormViewGatingTest(TestCase):
    """Blocked commands and the optional permission -- the is_ajax() branch.

    ``HttpRequest.is_ajax()`` was removed in Django 4.0, which is why these are
    the tests that fail on the current code: the refusal path raises
    ``AttributeError`` instead of answering.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user()

    def setUp(self):
        self.client.force_login(self.user)

    def test_ignored_command_submitted_by_ajax_returns_a_field_error(self):
        with mock.patch.object(TaskFormView, 'commands_skip', [ECHO]), \
                mock.patch('commands_async.views.command_exec') as task:
            response = self.client.post(INDEX_URL, payload(), **AJAX)

        self.assertEqual(response.status_code, 400)
        self.assertIn('app_command', json.loads(response.content)['form']['errors'])
        task.apply_async.assert_not_called()

    def test_ignored_command_submitted_without_ajax_is_forbidden(self):
        # A plain form post gets the 403 page rather than JSON it cannot use.
        with mock.patch.object(TaskFormView, 'commands_skip', [ECHO]), \
                mock.patch('commands_async.views.command_exec') as task:
            response = self.client.post(INDEX_URL, payload())

        self.assertEqual(response.status_code, 403)
        task.apply_async.assert_not_called()

    def test_submit_without_the_required_permission_returns_a_field_error(self):
        with mock.patch.object(TaskFormView, 'command_permission_name', 'auth.run_commands'), \
                mock.patch('commands_async.views.command_exec') as task:
            response = self.client.post(INDEX_URL, payload(), **AJAX)

        self.assertEqual(response.status_code, 400)
        self.assertIn('app_command', json.loads(response.content)['form']['errors'])
        task.apply_async.assert_not_called()

    def test_submit_with_the_required_permission_reaches_the_broker(self):
        permission = grant(self.user, 'run_commands')
        with mock.patch.object(TaskFormView, 'command_permission_name', permission), \
                mock.patch('commands_async.views.command_exec') as task:
            task.apply_async.return_value = queued_task()
            response = self.client.post(INDEX_URL, payload(), **AJAX)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(task.apply_async.called)


class TaskFormStatusTest(TestCase):
    """The polling endpoint the JS hits until a task is done."""

    @mock.patch('commands_async.views.AsyncResult')
    def test_get_reports_the_output_of_a_finished_task(self, async_result_class):
        async_result_class.return_value = async_result(task_id='abc',
                                                       result='all done')
        response = self.client.get(status_url('abc'))

        self.assertEqual(json.loads(response.content), {
            'task': {'id': 'abc', 'ready': True, 'failed': False, 'output': 'all done'},
        })

    @mock.patch('commands_async.views.AsyncResult')
    def test_get_reports_the_traceback_of_a_failed_task(self, async_result_class):
        async_result_class.return_value = async_result(task_id='abc', failed=True,
                                                       traceback='boom')
        data = json.loads(self.client.get(status_url('abc')).content)['task']

        self.assertTrue(data['failed'])
        self.assertEqual(data['traceback'], 'boom')
        self.assertNotIn('output', data,
                         msg='a failed task must not report an output')

    @mock.patch('commands_async.views.AsyncResult')
    def test_get_reports_a_task_that_is_still_running(self, async_result_class):
        async_result_class.return_value = async_result(task_id='abc', ready=False,
                                                       result=None)
        data = json.loads(self.client.get(status_url('abc')).content)['task']

        self.assertFalse(data['ready'])

    @mock.patch('commands_async.views.celery_app')
    def test_post_revokes_the_task(self, celery_app):
        response = self.client.post(status_url('abc'))

        celery_app.control.revoke.assert_called_once_with('abc', terminate=True)
        self.assertEqual(json.loads(response.content), {'status': True})

    @mock.patch('commands_async.views.celery_app')
    def test_post_reports_a_revoke_that_could_not_be_delivered(self, celery_app):
        celery_app.control.revoke.side_effect = RuntimeError('no broker')
        data = json.loads(self.client.post(status_url('abc')).content)

        self.assertFalse(data['status'])
        self.assertEqual(data['error'], 'no broker')


class CeleryWorkerStatusTest(TestCase):
    """The worker probe behind the toast on the form page."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user()

    def setUp(self):
        self.client.force_login(self.user)

    @mock.patch('commands_async.views.celery_app')
    def test_get_lists_the_workers_reported_as_active(self, celery_app):
        celery_app.control.inspect.return_value.active.return_value = {
            'worker-1@host': [], 'worker-2@host': [],
        }
        data = json.loads(self.client.get(WORKERS_URL).content)

        self.assertTrue(data['status'])
        self.assertEqual(sorted(data['workers']), ['worker-1@host', 'worker-2@host'])

    @mock.patch('commands_async.views.celery_app')
    def test_get_reports_failure_when_inspection_raises(self, celery_app):
        celery_app.control.inspect.side_effect = RuntimeError('no broker')
        data = json.loads(self.client.get(WORKERS_URL).content)

        self.assertFalse(data['status'])
        self.assertEqual(data['message'], 'no broker')

    @mock.patch('commands_async.views.celery_app')
    def test_get_reports_failure_when_no_worker_answers(self, celery_app):
        # inspect().active() answers None when nobody is listening; the view
        # reports that as a failed probe rather than an empty worker list.
        celery_app.control.inspect.return_value.active.return_value = None
        data = json.loads(self.client.get(WORKERS_URL).content)

        self.assertFalse(data['status'])
        self.assertEqual(data['workers'], [])
