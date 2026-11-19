# coding=utf-8
"""The three views: the form page, the task status poller and the worker probe.

The broker and the result backend are always replaced -- see
``test_cmdasync.harness``. What is asserted here is the JSON contract the bundled
JavaScript consumes, since that is the actual public interface of these views.
"""
import json
from contextlib import contextmanager
from unittest import mock
from urllib.parse import quote, urlencode

from django.conf import settings as project_settings
from django.contrib.auth.models import AnonymousUser
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.views.generic import View

from commands_async import settings as app_settings
from celery.app.control import Inspect
from commands_async.views import CeleryWorkerStatus, TaskFormStatus, TaskFormView

from test_cmdasync.harness import (AJAX, INDEX_URL, WORKERS_URL, async_result,
                                   create_user, grant, launch, queued_task, reset_workers_window, status_url)

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
        """AC-8864-14 (controle): a valid submit answers the id of the queued task and the command name."""
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
        """AC-8864-15 (controle): a broker that refuses the task is answered 400 with the field error, and logged."""
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
        """AC-8864-16 (controle): a command on the ignore list is refused by ajax with a field error, and nothing is queued."""
        with mock.patch.object(TaskFormView, 'commands_skip', [ECHO]), \
                mock.patch('commands_async.views.command_exec') as task:
            response = self.client.post(INDEX_URL, payload(), **AJAX)

        self.assertEqual(response.status_code, 400)
        self.assertIn('app_command', json.loads(response.content)['form']['errors'])
        task.apply_async.assert_not_called()

    def test_ignored_command_submitted_without_ajax_is_forbidden(self):
        """AC-8864-16 (controle): the same refusal on a plain form post is the 403 page, and nothing is queued."""
        # A plain form post gets the 403 page rather than JSON it cannot use.
        with mock.patch.object(TaskFormView, 'commands_skip', [ECHO]), \
                mock.patch('commands_async.views.command_exec') as task:
            response = self.client.post(INDEX_URL, payload())

        self.assertEqual(response.status_code, 403)
        task.apply_async.assert_not_called()

    def test_submit_without_the_required_permission_returns_a_field_error(self):
        """AC-8836-15 (controle): a user without the named permission is refused by ajax, and nothing is queued."""
        with mock.patch.object(TaskFormView, 'command_permission_name', 'auth.run_commands'), \
                mock.patch('commands_async.views.command_exec') as task:
            response = self.client.post(INDEX_URL, payload(), **AJAX)

        self.assertEqual(response.status_code, 400)
        self.assertIn('app_command', json.loads(response.content)['form']['errors'])
        task.apply_async.assert_not_called()

    def test_submit_without_the_required_permission_is_forbidden_without_ajax(self):
        """AC-8836-15 (controle): the same refusal on a plain form post is the 403 page, and nothing is queued."""
        with mock.patch.object(TaskFormView, 'command_permission_name', 'auth.run_commands'), \
                mock.patch('commands_async.views.command_exec') as task:
            response = self.client.post(INDEX_URL, payload())

        self.assertEqual(response.status_code, 403)
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
    """The polling endpoint the JS hits until a task is done, as an authorized user sees it.

    The endpoint demands a session since #8836 (see ``TaskFormStatusAccessTest``): these are the
    contract tests it always had, now from a logged-in user. Since #8864 the task read is one the user
    launched through the form, so it is launched here first.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user()

    def setUp(self):
        self.client.force_login(self.user)
        launch(self.client, 'abc')

    @mock.patch('commands_async.views.AsyncResult')
    def test_get_reports_the_output_of_a_finished_task(self, async_result_class):
        """AC-8864-1 (controle): a finished task reports its id, that it is ready, not failed, and its output."""
        async_result_class.return_value = async_result(task_id='abc',
                                                       result='all done')
        response = self.client.get(status_url('abc'))

        self.assertEqual(json.loads(response.content), {
            'task': {'id': 'abc', 'ready': True, 'failed': False, 'output': 'all done'},
        })

    @mock.patch('commands_async.views.AsyncResult')
    def test_get_reports_the_traceback_of_a_failed_task(self, async_result_class):
        """AC-8864-1 (controle): a failed task reports that it failed and its traceback, never an output."""
        async_result_class.return_value = async_result(task_id='abc', failed=True,
                                                       traceback='boom')
        data = json.loads(self.client.get(status_url('abc')).content)['task']

        self.assertTrue(data['failed'])
        self.assertEqual(data['traceback'], 'boom')
        self.assertNotIn('output', data,
                         msg='a failed task must not report an output')

    @mock.patch('commands_async.views.AsyncResult')
    def test_get_reports_a_task_that_is_still_running(self, async_result_class):
        """AC-8864-1 (controle): a task that has not finished reports that it is not ready."""
        async_result_class.return_value = async_result(task_id='abc', ready=False,
                                                       result=None)
        data = json.loads(self.client.get(status_url('abc')).content)['task']

        self.assertFalse(data['ready'])

    @mock.patch('commands_async.views.AsyncResult')
    def test_get_reports_a_null_output_while_the_task_has_not_finished(self, async_result_class):
        """AC-8864-1 (controle): the output is the backend's own answer, null while there is none, never a traceback."""
        async_result_class.return_value = async_result(task_id='abc', ready=False,
                                                       result=None)
        data = json.loads(self.client.get(status_url('abc')).content)['task']

        self.assertEqual(data, {'id': 'abc', 'ready': False, 'failed': False, 'output': None})

    @mock.patch('commands_async.views.celery_app')
    def test_post_revokes_the_task(self, celery_app):
        """AC-8864-2, AC-8864-3 (controle): an authorized revoke asks Celery to terminate the task and says so."""
        response = self.client.post(status_url('abc'))

        celery_app.control.revoke.assert_called_once_with('abc', terminate=True)
        self.assertEqual(json.loads(response.content), {'status': True})

    @mock.patch('commands_async.views.celery_app')
    def test_post_reports_a_revoke_that_could_not_be_delivered(self, celery_app):
        """AC-8879-2: a revoke that Celery cannot take is reported with the fixed phrase, not with its text."""
        celery_app.control.revoke.side_effect = RuntimeError('no broker')
        response = self.client.post(status_url('abc'))
        data = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(data['status'])
        self.assertEqual(data['error'], 'the revocation request could not be sent')
        self.assertNotIn('no broker', response.content.decode('utf-8'), msg='the text of the error must not reach the user')


# #8836: who may read and revoke a task through the status route. A readable result sits behind every id (the
# canary), so a denial that leaks anything shows up as the canary in its body.
OUTPUT = 'OUTPUT-8836-canary'
TRACEBACK = 'TRACEBACK-8836-canary'
DONE_ID = 'done-8836'
FAILED_ID = 'failed-8836'


def readable_results():
    """``AsyncResult`` stand-in: a finished task with an output and a failed one with a traceback."""
    results = {
        DONE_ID: async_result(task_id=DONE_ID, result=OUTPUT),
        FAILED_ID: async_result(task_id=FAILED_ID, failed=True, traceback=TRACEBACK),
    }

    def build(id=None, app=None, **kwargs):
        return results[id]

    return build


class TaskFormStatusAccessTest(TestCase):
    """The status route answers only an authenticated user who holds the permission the form asks for.

    XHR is the request that declares ``X-Requested-With: XMLHttpRequest`` (what the bundled JS sends); every other
    request, a script asking for JSON included, is plain navigation and is redirected to the login page.
    """

    @classmethod
    def setUpTestData(cls):
        cls.plain = create_user('plain')
        cls.runner = create_user('runner')
        cls.permission = grant(cls.runner, 'run_commands')

    def setUp(self):
        results = mock.patch('commands_async.views.AsyncResult', side_effect=readable_results())
        results.start()
        self.addCleanup(results.stop)
        celery = mock.patch('commands_async.views.celery_app')
        self.celery_app = celery.start()
        self.addCleanup(celery.stop)
        self.revoke = self.celery_app.control.revoke
        # These tests measure the login and the permission, not the owner of the task (#8864): with the
        # owner check on, a user who reads a task he did not launch is answered 404 before the result matters.
        ownership = mock.patch.object(app_settings, 'COMMANDS_ASYNC_TASK_OWNERSHIP', False)
        ownership.start()
        self.addCleanup(ownership.stop)

    @staticmethod
    def logged(user):
        client = Client()
        client.force_login(user)
        return client

    @contextmanager
    def permission_required(self):
        """The host names a permission for this view only (``create=True``: the attribute is inherited from the mixin).

        Patched on the view class, never on the mixin, so the gate of the workers view stays as it is.
        """
        with mock.patch.object(TaskFormStatus, 'command_permission_name', self.permission, create=True):
            yield

    def assertNoTaskData(self, response, label):
        body = response.content.decode('utf-8')
        for leak in (OUTPUT, TRACEBACK, '"ready"', '"traceback"', '"output"'):
            self.assertNotIn(leak, body, msg='%s: the denial must not carry %s' % (label, leak))

    def assertReads(self, client, label, **extra):
        """The user reads the canary behind a finished task: what the denials below are measured against."""
        response = client.get(status_url(DONE_ID), **extra)
        self.assertEqual(response.status_code, 200, msg='precondition (%s): the user reads the result' % label)
        self.assertIn(OUTPUT, response.content.decode('utf-8'), msg='precondition (%s): the canary is readable' % label)

    def assertRevokes(self, client, label):
        """The user's revoke reaches Celery, so the silence in the denials below is the gate's."""
        client.post(status_url(DONE_ID))
        self.revoke.assert_called_once_with(DONE_ID, terminate=True)
        self.revoke.reset_mock()

    def test_anonymous_navigation_is_sent_to_the_login_url(self):
        """AC-8836-1: an anonymous query by plain navigation is redirected to the login page, with no task data."""
        self.assertReads(self.logged(self.plain), 'logged in')
        for task_id in (DONE_ID, FAILED_ID):
            for label, extra in (('navigation', {}), ('script asking for JSON', {'HTTP_ACCEPT': 'application/json'})):
                with self.subTest(task=task_id, request=label):
                    response = self.client.get(status_url(task_id), **extra)
                    self.assertRedirects(response, '/login/?next={0}'.format(status_url(task_id)),
                                         fetch_redirect_response=False)
                    self.assertNoTaskData(response, label)

    def test_anonymous_xhr_gets_a_json_403(self):
        """AC-8836-2, AC-8835-12 (controle): an anonymous query by XHR is answered 403 in JSON, with no task data."""
        self.assertReads(self.logged(self.plain), 'logged in', **AJAX)
        for task_id in (DONE_ID, FAILED_ID):
            with self.subTest(task=task_id):
                response = self.client.get(status_url(task_id), **AJAX)
                self.assertEqual(response.status_code, 403, msg='an anonymous XHR must be denied, not redirected')
                self.assertEqual(json.loads(response.content), {'task': None})
                self.assertNoTaskData(response, 'anonymous xhr')

    def test_anonymous_revoke_never_reaches_celery(self):
        """AC-8836-3: an anonymous revoke, by navigation or XHR, is never forwarded to Celery."""
        self.assertRevokes(self.logged(self.plain), 'logged in')
        for label, extra in (('navigation', {}), ('xhr', AJAX)):
            with self.subTest(request=label):
                self.client.post(status_url(DONE_ID), **extra)
                self.revoke.assert_not_called()

    def test_anonymous_revoke_is_denied_like_the_anonymous_query(self):
        """AC-8836-13, AC-8835-12 (controle): an anonymous revoke is redirected to the login page, or answered 403 in JSON by XHR."""
        with self.subTest(request='navigation'):
            response = self.client.post(status_url(DONE_ID))
            self.assertRedirects(response, '/login/?next={0}'.format(status_url(DONE_ID)),
                                 fetch_redirect_response=False)
        with self.subTest(request='xhr'):
            response = self.client.post(status_url(DONE_ID), **AJAX)
            self.assertEqual(response.status_code, 403, msg='an anonymous XHR must be denied, not redirected')
            self.assertEqual(json.loads(response.content), {'task': None})

    def test_anonymous_denial_goes_to_the_login_url_the_package_names(self):
        """AC-8836-1, AC-8836-13: the redirect goes to ``COMMANDS_ASYNC_LOGIN_URL``, not to the project's ``LOGIN_URL``."""
        # The test project's LOGIN_URL is '/login/' and the package setting inherits it (commands_async/settings.py:22),
        # so the tests above cannot tell the two apart. Here the host points the package somewhere else; the view reads
        # the setting at call time (views.py:38), hence the patch on the settings module.
        elsewhere = '/elsewhere/'
        self.assertNotEqual(project_settings.LOGIN_URL, elsewhere, msg='precondition: the two login pages differ')
        with mock.patch.object(app_settings, 'COMMANDS_ASYNC_LOGIN_URL', elsewhere):
            for method in ('get', 'post'):
                with self.subTest(method=method):
                    response = getattr(self.client, method)(status_url(DONE_ID))
                    self.assertRedirects(response, '{0}?next={1}'.format(elsewhere, status_url(DONE_ID)),
                                         fetch_redirect_response=False)
                    self.assertNoTaskData(response, method)

    def test_a_user_without_the_named_permission_is_forbidden_to_query(self):
        """AC-8836-4, AC-8835-12 (controle): when a permission is named, a user without it is answered 403 (JSON by XHR), no task data."""
        with self.permission_required():
            self.assertReads(self.logged(self.runner), 'holds the permission')
            client = self.logged(self.plain)
            for task_id in (DONE_ID, FAILED_ID):
                for label, extra in (('navigation', {}), ('xhr', AJAX)):
                    with self.subTest(task=task_id, request=label):
                        response = client.get(status_url(task_id), **extra)
                        self.assertEqual(response.status_code, 403, msg='%s: must be forbidden' % label)
                        if extra:
                            self.assertEqual(json.loads(response.content), {'task': None})
                        self.assertNoTaskData(response, label)

    def test_a_user_without_the_named_permission_is_forbidden_to_revoke(self):
        """AC-8836-5, AC-8835-12 (controle): when a permission is named, a user without it who revokes is answered 403 (JSON by XHR)."""
        with self.permission_required():
            client = self.logged(self.plain)
            for label, extra in (('navigation', {}), ('xhr', AJAX)):
                with self.subTest(request=label):
                    response = client.post(status_url(DONE_ID), **extra)
                    self.assertEqual(response.status_code, 403, msg='%s: must be forbidden' % label)
                    if extra:
                        self.assertEqual(json.loads(response.content), {'task': None})

    def test_a_user_without_the_named_permission_never_reaches_celery(self):
        """AC-8836-14: when a permission is named, a revoke by a user without it is never forwarded to Celery."""
        with self.permission_required():
            self.assertRevokes(self.logged(self.runner), 'holds the permission')
            client = self.logged(self.plain)
            for label, extra in (('navigation', {}), ('xhr', AJAX)):
                with self.subTest(request=label):
                    client.post(status_url(DONE_ID), **extra)
                    self.revoke.assert_not_called()

    def test_a_user_with_the_named_permission_is_served(self):
        """AC-8864-1, AC-8864-2, AC-8864-3 (controle): the holder of the named permission reads and revokes the task he launched."""
        with self.permission_required(), mock.patch.object(app_settings, 'COMMANDS_ASYNC_TASK_OWNERSHIP', True):
            client = self.logged(self.runner)
            launch(client, DONE_ID)
            data = json.loads(client.get(status_url(DONE_ID)).content)['task']
            self.assertEqual(data, {'id': DONE_ID, 'ready': True, 'failed': False, 'output': OUTPUT})
            response = client.post(status_url(DONE_ID))
            self.revoke.assert_called_once_with(DONE_ID, terminate=True)
            self.assertEqual(json.loads(response.content), {'status': True})

    def test_a_user_without_the_named_permission_is_forbidden_even_on_a_task_he_does_not_own(self):
        """AC-8836-4, AC-8836-5, AC-8835-12: with the owner check on (the default), a user without the named permission is answered 403, never the owner's 404."""
        # O `setUp` desliga o dono para medir só o gate; aqui ele volta ao padrão. A permissão tem de ser conferida ANTES do
        # dono (`CommandGateMixin.dispatch`: `has_permission` em views.py:143, `task_denial` em :146): se a ordem se
        # invertesse, quem não tem a permissão e não é dono receberia o 404 do dono, e o 403 dos critérios deixaria de
        # existir na configuração padrão, sem que nenhum outro teste (todos com o dono desligado) acusasse.
        with mock.patch.object(app_settings, 'COMMANDS_ASYNC_TASK_OWNERSHIP', True):
            holder = self.logged(self.runner)
            launch(holder, DONE_ID)
            client = self.logged(self.plain)

            # pré-condições: o portador é servido e revoga a própria tarefa; e, sem a permissão nomeada, o dono recusa este
            # usuário com o 404 (é essa recusa que o 403 tem de preceder)
            with self.permission_required():
                self.assertReads(holder, 'holds the permission, owner check on')
                self.assertRevokes(holder, 'holds the permission, owner check on')
            self.assertEqual(client.get(status_url(DONE_ID)).status_code, 404,
                             msg='precondition: with no permission named, the owner check refuses a user who did not launch the task')

            with self.permission_required():
                for method in ('get', 'post'):
                    for label, extra in (('navigation', {}), ('xhr', AJAX)):
                        with self.subTest(method=method, request=label):
                            response = getattr(client, method)(status_url(DONE_ID), **extra)
                            self.assertEqual(response.status_code, 403, msg='%s %s: must be forbidden, not not found' % (method, label))
                            if extra:
                                self.assertEqual(json.loads(response.content), {'task': None})
                            self.assertNoTaskData(response, '%s %s' % (method, label))
                self.revoke.assert_not_called()

    def test_every_denial_is_marked_no_store(self):
        """AC-8864-4 (controle): every denial (login or permission, by navigation or XHR) is no-store, even off the URLconf."""
        denials = []
        for method in ('get', 'post'):
            for label, extra in (('navigation', {}), ('xhr', AJAX)):
                denials.append(('anonymous %s %s' % (method, label),
                                getattr(self.client, method)(status_url(DONE_ID), **extra)))
                with self.permission_required():
                    denials.append(('no permission %s %s' % (method, label),
                                    getattr(self.logged(self.plain), method)(status_url(DONE_ID), **extra)))
                # the view mounted straight, without the never_cache of the package URLconf
                for who, user in (('anonymous', AnonymousUser()), ('no permission', self.plain)):
                    request = getattr(RequestFactory(), method)(status_url(DONE_ID), **extra)
                    request.user = user
                    with self.permission_required():
                        response = TaskFormStatus.as_view()(request, task_id=DONE_ID)
                    denials.append(('direct %s %s %s' % (who, method, label), response))
        self.assertEqual(len(denials), 16, msg='precondition: every form of denial is exercised')
        for label, response in denials:
            with self.subTest(denial=label):
                self.assertIn(response.status_code, (302, 403), msg='%s: must be a denial' % label)
                self.assertIn('no-store', response.headers.get('Cache-Control', ''),
                              msg='%s: the denial must be no-store' % label)


class LoginRedirectTest(TestCase):
    """What ``next`` carries when the login page is an absolute address (no criterion: robustness of the helper).

    ``login_redirect`` copies what django's ``login_required`` does (views.py:39-44): ``next`` is just the path when the
    login page is on the same site, and the absolute address of the request when it is on another one (a single
    sign-on page), where a bare path would send the user back to the wrong site.
    """

    def setUp(self):
        patch = mock.patch('commands_async.views.AsyncResult', side_effect=readable_results())
        patch.start()
        self.addCleanup(patch.stop)

    def redirect_to(self, login_url):
        with mock.patch.object(app_settings, 'COMMANDS_ASYNC_LOGIN_URL', login_url):
            return self.client.get(status_url(DONE_ID))

    def test_a_login_page_on_another_host_gets_the_absolute_address_as_next(self):
        # the test client talks to http://testserver
        response = self.redirect_to('https://sso.example.com/login/')

        self.assertRedirects(response,
                             'https://sso.example.com/login/?next={0}'.format(
                                 quote('http://testserver' + status_url(DONE_ID), safe='/')),
                             fetch_redirect_response=False)

    def test_a_login_page_on_the_same_host_gets_only_the_path_as_next(self):
        response = self.redirect_to('http://testserver/login/')

        self.assertRedirects(response, 'http://testserver/login/?next={0}'.format(status_url(DONE_ID)),
                             fetch_redirect_response=False)


class CeleryWorkerStatusTest(TestCase):
    """The worker probe behind the toast on the form page."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user()

    def setUp(self):
        self.client.force_login(self.user)
        # A janela da rota workers é estado do módulo (#8879): cada teste a pede zerada e a devolve zerada.
        reset_workers_window()
        self.addCleanup(reset_workers_window)

    @mock.patch('commands_async.views.celery_app')
    def test_get_lists_the_workers_reported_as_active(self, celery_app):
        """AC-8879-11 (controle): a user without any named permission lists the workers that answered."""
        celery_app.control.inspect.return_value.active.return_value = {
            'worker-1@host': [], 'worker-2@host': [],
        }
        data = json.loads(self.client.get(WORKERS_URL).content)

        self.assertTrue(data['status'])
        self.assertEqual(sorted(data['workers']), ['worker-1@host', 'worker-2@host'])

    @mock.patch('commands_async.views.celery_app')
    def test_get_reports_failure_when_inspection_raises(self, celery_app):
        """AC-8879-1: a failing inspection is answered 200, with the fixed phrase (not its text) and no worker name."""
        celery_app.control.inspect.side_effect = RuntimeError('no broker')
        response = self.client.get(WORKERS_URL)
        data = json.loads(response.content)

        # 200, not an error status: the bundled JS shows the message of a false status from ``.done`` and sends any
        # error status to ``.fail``, which only asks again (static/cmdasync/js/celery.workers.status.js:13-29).
        self.assertEqual(response.status_code, 200)
        self.assertFalse(data['status'])
        self.assertEqual(data['workers'], [])
        self.assertEqual(data['message'], 'could not ask the Celery workers')
        self.assertNotIn('no broker', response.content.decode('utf-8'), msg='the text of the error must not reach the user')

    @mock.patch('commands_async.views.celery_app')
    def test_get_reports_failure_when_no_worker_answers(self, celery_app):
        """AC-8835-6 (controle): when no worker answers the probe fails, answered 200 with no worker name (the text of ``message`` is not promised)."""
        # inspect().active() answers None when nobody is listening (celery/app/control.py:92-103, `_prepare` only
        # returns something `if reply:`); the view reports that as a failed probe rather than an empty worker list.
        celery_app.control.inspect.return_value.active.return_value = None
        response = self.client.get(WORKERS_URL)
        data = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(data['status'])
        self.assertEqual(data['workers'], [])


# #8835: who may ask the workers route, and what the gate hands back. The canary is a worker name: a denial that
# leaks anything shows it in the body. A task running on a worker shows that only names are listed.
WORKER = 'worker-canary-8835@host'
METHODS = ('get', 'post', 'put', 'patch', 'delete', 'head', 'options')
MOUNTINGS = ('urlconf', 'direct')


class CeleryWorkerStatusAccessTest(TestCase):
    """The workers route answers only an authenticated user who holds the permission the form asks for.

    The same two ways of asking as the status route: through the package URLconf (``Client``) and with the view
    mounted straight (``RequestFactory``, no decorator of ``urls.py``), because the gate belongs to the class.
    Unsafe methods go through the default ``Client``, which does not enforce CSRF: the token is "valid".
    """

    @classmethod
    def setUpTestData(cls):
        cls.plain = create_user('plain')
        cls.runner = create_user('runner')
        cls.permission = grant(cls.runner, 'run_commands')

    def setUp(self):
        celery = mock.patch('commands_async.views.celery_app')
        self.celery_app = celery.start()
        self.addCleanup(celery.stop)
        self.inspect = self.celery_app.control.inspect
        self.inspect.return_value.active.return_value = {WORKER: []}
        reset_workers_window()  # a janela da rota workers é estado do módulo (#8879)
        self.addCleanup(reset_workers_window)

    @contextmanager
    def permission_required(self):
        """The host names a permission for the workers view only (the attribute is inherited from the mixin)."""
        with mock.patch.object(CeleryWorkerStatus, 'command_permission_name', self.permission, create=True):
            yield

    @staticmethod
    def asks(who, mounting, method='get', **extra):
        """Asks the route as ``who`` (a user, or ``None`` for the anonymous), through the URLconf or mounted straight."""
        url = WORKERS_URL + '?refresh=1'
        if mounting == 'urlconf':
            client = Client()
            if who is not None:
                client.force_login(who)
            return getattr(client, method)(url, **extra)
        request = getattr(RequestFactory(), method)(url, **extra)
        request.user = AnonymousUser() if who is None else who
        return CeleryWorkerStatus.as_view()(request)

    def assertLists(self, who, mounting, label, **extra):
        """The user lists the canary worker and the inspection runs: what the denials are measured against."""
        reset_workers_window()  # várias chamadas por teste: sem isto a 2ª cairia na janela de 5 s (#8879)
        self.inspect.reset_mock()
        response = self.asks(who, mounting, 'get', **extra)
        self.assertEqual(response.status_code, 200, msg='precondition (%s, %s): the user lists the workers' % (mounting, label))
        self.assertIn(WORKER, response.content.decode('utf-8'),
                      msg='precondition (%s, %s): the canary is listed' % (mounting, label))
        self.inspect.assert_called_once()
        self.inspect.reset_mock()

    def assertNoWorkerData(self, response, label):
        self.assertNotIn(WORKER, response.content.decode('utf-8'), msg='%s: the denial must not carry a worker name' % label)

    def assertJsonDenial(self, response, label, mounting, method):
        """The denial of an XHR is a JSON body with a false status and no worker, whatever the user or the method."""
        if method == 'head' and mounting == 'urlconf':
            return  # the test client takes the body off a HEAD answer (django/test/client.py:139-152); mounted straight, it stays
        self.assertTrue(response.content, msg='%s: the denial must carry a JSON body' % label)
        body = json.loads(response.content)
        self.assertEqual(body['status'], False, msg='%s: status is false' % label)
        self.assertEqual(body['workers'], [], msg='%s: no worker is listed' % label)

    def test_anonymous_navigation_is_sent_to_the_login_url_the_package_names(self):
        """AC-8835-1: an anonymous request by navigation, with any method, goes to the package's login page, with the route to come back to."""
        # The test project's LOGIN_URL is '/login/' and the package setting inherits it (commands_async/settings.py:22),
        # so a test that redirects to '/login/' cannot tell the two apart: the host points the package elsewhere.
        elsewhere = '/elsewhere/'
        self.assertNotEqual(project_settings.LOGIN_URL, elsewhere, msg='precondition: the two login pages differ')
        expected = '{0}?{1}'.format(elsewhere, urlencode({'next': WORKERS_URL + '?refresh=1'}, safe='/'))
        for mounting in MOUNTINGS:
            self.assertLists(self.runner, mounting, 'logged in')
            with mock.patch.object(app_settings, 'COMMANDS_ASYNC_LOGIN_URL', elsewhere):
                for method in METHODS:
                    with self.subTest(mounting=mounting, method=method):
                        response = self.asks(None, mounting, method)
                        self.assertEqual(response.status_code, 302, msg='an anonymous request must be redirected')
                        self.assertEqual(response['Location'], expected)
                        self.assertNoWorkerData(response, method)

    def test_anonymous_xhr_gets_a_json_403(self):
        """AC-8835-2: an anonymous request by XHR, with any method, is answered 403 in JSON, with no worker name."""
        for mounting in MOUNTINGS:
            self.assertLists(self.runner, mounting, 'logged in', **AJAX)
            for method in METHODS:
                with self.subTest(mounting=mounting, method=method):
                    response = self.asks(None, mounting, method, **AJAX)
                    self.assertEqual(response.status_code, 403, msg='an anonymous XHR must be denied, not redirected')
                    self.assertJsonDenial(response, method, mounting, method)
                    self.assertNoWorkerData(response, method)

    def test_a_user_without_the_named_permission_is_forbidden(self):
        """AC-8835-3: when a permission is named, a user without it is answered 403 (JSON by XHR), with no worker name."""
        with self.permission_required():
            for mounting in MOUNTINGS:
                self.assertLists(self.runner, mounting, 'holds the permission')
                for label, extra in (('navigation', {}), ('xhr', AJAX)):
                    for method in METHODS:
                        with self.subTest(mounting=mounting, request=label, method=method):
                            response = self.asks(self.plain, mounting, method, **extra)
                            self.assertEqual(response.status_code, 403, msg='%s: must be forbidden' % label)
                            if extra:
                                self.assertJsonDenial(response, label, mounting, method)
                            self.assertNoWorkerData(response, label)

    def test_a_user_with_the_named_permission_lists_the_workers(self):
        """AC-8879-11 (controle): the holder of the named permission lists the workers, with or without a task on them."""
        self.inspect.return_value.active.return_value = {WORKER: [{'id': 'task-8835', 'name': 'busy'}], 'idle@host': []}
        with self.permission_required():
            for mounting in MOUNTINGS:
                with self.subTest(mounting=mounting):
                    reset_workers_window()  # cada montagem é uma consulta nova, fora da janela (#8879)
                    response = self.asks(self.runner, mounting)
                    data = json.loads(response.content)
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(data['status'])
                    self.assertEqual(sorted(data['workers']), sorted([WORKER, 'idle@host']))
                    self.assertNotIn('task-8835', response.content.decode('utf-8'), msg='only the names are listed')

    def test_every_denial_is_marked_no_store(self):
        """AC-8835-7: every denial (login or permission, by navigation or XHR), through the URLconf or not, is no-store."""
        denials = []
        with self.permission_required():
            for method in ('get', 'post'):
                for label, extra in (('navigation', {}), ('xhr', AJAX)):
                    for mounting in MOUNTINGS:
                        for who_label, who in (('anonymous', None), ('no permission', self.plain)):
                            denials.append(('%s %s %s %s' % (mounting, who_label, method, label),
                                            self.asks(who, mounting, method, **extra)))
        self.assertEqual(len(denials), 16, msg='precondition: every form of denial is exercised')
        for label, response in denials:
            with self.subTest(denial=label):
                self.assertIn(response.status_code, (302, 403), msg='%s: must be a denial' % label)
                self.assertIn('no-store', response.headers.get('Cache-Control', ''),
                              msg='%s: the denial must be no-store' % label)

    def test_a_denied_request_never_reaches_the_workers(self):
        """AC-8835-10: no denied request, by any method, mounting or kind of request, makes the inspection run."""
        with self.permission_required():
            for mounting in MOUNTINGS:
                self.assertLists(self.runner, mounting, 'holds the permission')  # the inspection runs for the holder
            for mounting in MOUNTINGS:
                for who in (None, self.plain):
                    for extra in ({}, AJAX):
                        for method in METHODS:
                            self.asks(who, mounting, method, **extra)
            self.inspect.assert_not_called()

    def test_an_authorized_user_gets_405_for_a_method_other_than_get(self):
        """AC-8835-11 (controle): the holder of the named permission is answered 405 by POST, PUT, PATCH and DELETE."""
        with self.permission_required():
            self.assertLists(self.runner, 'urlconf', 'holds the permission')
            for method in ('post', 'put', 'patch', 'delete'):
                with self.subTest(method=method):
                    self.assertEqual(self.asks(self.runner, 'urlconf', method).status_code, 405)
            self.inspect.assert_not_called()

    def test_the_workers_denial_by_xhr_carries_the_shape_of_the_route(self):
        # The body is a design choice (the spec of #8835): the shape the route already answers on failure, with
        # no `message` and no worker, and not the `{"task": null}` of the status route.
        response = self.asks(None, 'direct', 'get', **AJAX)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content), {'status': False, 'workers': []})


class CommandGateStructureTest(SimpleTestCase):
    """What the project's URLconf and the tests above rely on (no criterion: robustness of the design)."""

    def test_the_workers_view_is_not_a_view_the_project_swaps(self):
        # plus_base/publique/urls_commands_async.py:39-43,52 swaps the callback of every route whose view class is a
        # subclass of TaskFormStatus or TaskFormView: the workers route would be served by the project's view.
        self.assertFalse(issubclass(CeleryWorkerStatus, (TaskFormStatus, TaskFormView)))

    def test_the_status_and_workers_views_share_one_gate(self):
        self.assertIsNot(TaskFormStatus.dispatch, View.dispatch, msg='precondition: the status view has the gate')
        self.assertIs(CeleryWorkerStatus.dispatch, TaskFormStatus.dispatch)


class InspectAnswerTest(SimpleTestCase):
    """What the installed Celery answers, which the workers tests double (no criterion: an assumption of a double)."""

    def test_no_reply_is_none_and_a_reply_is_a_dict_by_node(self):
        # celery/app/control.py:92-103: `_prepare` only returns something `if reply:`.
        inspect = Inspect()

        self.assertIsNone(inspect._prepare([]), msg='no worker answered: no reply, not an empty collection')
        self.assertEqual(inspect._prepare([{WORKER: []}]), {WORKER: []})
