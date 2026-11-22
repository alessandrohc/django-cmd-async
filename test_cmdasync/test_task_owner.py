# coding=utf-8
"""O dono da tarefa na rota status e no formulário de execução (#8864).

Execução (container `django`, com o `pyenv` do projeto):
    cd /opt/project/packages/django-cmd-async && /opt/project/pyenv/bin/python runtests.py test_cmdasync.test_task_owner -v 2

O registro do dono mora num cache do Django. Os testes montam os aliases em `OWNER_CACHES`: dois caches da memória
(`owner`, `owner_b`), um que descarta a gravação sem erro (`dropping`) e um que levanta erro (`broken`, `test_cmdasync.caches`).
O broker e o backend de resultados são trocados por dublês (`test_cmdasync.harness`): nada sai do processo, e o `testcover` do
projeto não é usado aqui (o runner é o do próprio pacote).

As settings do dono (`COMMANDS_ASYNC_TASK_OWNERSHIP`, `COMMANDS_ASYNC_OWNER_CACHE_ALIAS`, `COMMANDS_ASYNC_OWNER_TIMEOUT`) são
trocadas no módulo `commands_async.settings`, que as views leem na hora do uso (plan.md do #8864, "Superfície de teste").
"""
import json
import time
from contextlib import contextmanager
from unittest import mock

from django.core import checks
from django.core.cache import caches
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings

from commands_async import settings as app_settings
from commands_async import views
from commands_async.views import TaskFormStatus

from test_cmdasync.harness import AJAX, INDEX_URL, async_result, create_user, launch, status_url

OUTPUT = 'OUTPUT-8864-canary'
NOT_FOUND = {'task': None}
LOCMEM = 'django.core.cache.backends.locmem.LocMemCache'
OWNER_CACHES = {
    'default': {'BACKEND': LOCMEM, 'LOCATION': 'default-8864'},
    'owner': {'BACKEND': LOCMEM, 'LOCATION': 'owner-8864'},
    'owner_b': {'BACKEND': LOCMEM, 'LOCATION': 'owner-b-8864'},
    'dropping': {'BACKEND': 'test_cmdasync.caches.DroppingCache', 'LOCATION': 'dropping-8864'},
    'broken': {'BACKEND': 'test_cmdasync.caches.RaisingCache', 'LOCATION': 'broken-8864'},
}
#: os casos em que o pedido não é de um dono: o que a rota nega com o mesmo 404 (AC-8864-5 … -10, -13)
DENIALS = ('foreign', 'unknown', 'lost', 'dropped', 'unreadable')
OWNER_ALIAS = 'COMMANDS_ASYNC_OWNER_CACHE_ALIAS'
OWNERSHIP = 'COMMANDS_ASYNC_TASK_OWNERSHIP'
OWNER_TIMEOUT = 'COMMANDS_ASYNC_OWNER_TIMEOUT'


@override_settings(CACHES=OWNER_CACHES)
class OwnerTestCase(TestCase):
    """Base: dois usuários autorizados, o broker e o backend trocados e os caches do registro limpos entre os testes.

    O cache local da memória é global no processo e os testes repetem ids e pks (o SQLite em memória reinicia o pk a cada
    classe): sem a limpeza, o registro de um teste mudaria o resultado do seguinte conforme a ordem.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = create_user('owner')
        cls.other = create_user('other')

    def setUp(self):
        self.clear_caches()
        self.addCleanup(self.clear_caches)
        results = mock.patch('commands_async.views.AsyncResult', side_effect=self.read_result)
        self.async_result_class = results.start()
        self.addCleanup(results.stop)
        celery = mock.patch('commands_async.views.celery_app')
        self.celery_app = celery.start()
        self.addCleanup(celery.stop)
        self.revoke = self.celery_app.control.revoke

    @staticmethod
    def clear_caches():
        for alias in ('default', 'owner', 'owner_b'):
            caches[alias].clear()

    @staticmethod
    def read_result(id=None, app=None, **kwargs):
        """Qualquer id tem um resultado legível (a "canária"): a negativa que vaza algo mostra a canária no corpo."""
        return async_result(task_id=id, result=OUTPUT)

    @staticmethod
    def logged(user):
        client = Client()
        client.force_login(user)
        return client

    @contextmanager
    def owner_setting(self, name, value):
        with mock.patch.object(app_settings, name, value):
            yield

    def assertServed(self, response, task_id, label):
        self.assertEqual(response.status_code, 200, msg='%s: must be served' % label)
        data = json.loads(response.content)['task']
        self.assertEqual(data['id'], task_id, msg='%s: the id the client sent' % label)
        self.assertEqual(data['output'], OUTPUT, msg='%s: the output of the task' % label)

    def assertDenied(self, response, label):
        self.assertEqual(response.status_code, 404, msg='%s: must be answered not found' % label)
        self.assertEqual(json.loads(response.content), NOT_FOUND, msg='%s: the body of the denial' % label)
        self.assertNotIn(OUTPUT, response.content.decode('utf-8'), msg='%s: the denial must not leak the output' % label)

    def launched_by_owner(self, task_id):
        """O dono dispara a tarefa e é servido nela: o controle positivo contra o qual as negativas se medem."""
        client = self.logged(self.owner)
        launch(client, task_id)
        self.assertServed(client.get(status_url(task_id)), task_id, 'precondition: the owner is served his task')
        self.async_result_class.reset_mock()
        self.celery_app.reset_mock()
        return client

    @contextmanager
    def denial(self, case):
        """(client, task_id) de um pedido que o dono da tarefa deve negar, no estado que cada caso monta."""
        owner = self.logged(self.owner)
        if case == 'foreign':
            launch(owner, 'task-foreign')
            yield self.logged(self.other), 'task-foreign'
        elif case == 'unknown':
            yield owner, 'task-never-launched'
        elif case == 'lost':
            launch(owner, 'task-lost')
            caches['default'].clear()
            yield owner, 'task-lost'
        elif case == 'dropped':
            # a gravação some sem erro (o `IGNORE_EXCEPTIONS` do Redis do projeto): o disparo responde o id e não há registro
            with self.owner_setting(OWNER_ALIAS, 'dropping'):
                launch(owner, 'task-dropped')
                yield owner, 'task-dropped'
        elif case == 'unreadable':
            with self.owner_setting(OWNER_ALIAS, 'owner'):
                launch(owner, 'task-unreadable')
            with self.owner_setting(OWNER_ALIAS, 'broken'):
                yield owner, 'task-unreadable'
        else:
            raise ValueError(case)


class OwnerCheckTest(OwnerTestCase):
    """O que a rota status nega a quem não é dono da tarefa, e o que serve a quem é."""

    def test_another_user_is_answered_not_found_with_any_method(self):
        """AC-8864-5: a task launched by another user is answered 404 with `task` null, with any HTTP method, and no data."""
        owner = self.launched_by_owner('task-5')
        other = self.logged(self.other)

        for method in ('get', 'post', 'put', 'patch', 'delete', 'options'):
            with self.subTest(method=method):
                self.assertDenied(getattr(other, method)(status_url('task-5')), method)
        self.assertServed(owner.get(status_url('task-5')), 'task-5', 'the owner is still served')

    def test_a_revoke_denied_by_the_owner_check_never_reaches_celery(self):
        """AC-8864-6: a revoke refused because the task is not the user's (another user, unknown, lost, dropped or unreadable) is never sent."""
        owner = self.launched_by_owner('task-own')
        owner.post(status_url('task-own'))
        self.revoke.assert_called_once_with('task-own', terminate=True)  # precondition: the owner's revoke is sent
        self.celery_app.reset_mock()

        for case in DENIALS:
            with self.subTest(case=case), self.denial(case) as (client, task_id):
                client.post(status_url(task_id))
                self.revoke.assert_not_called()

    def test_a_denied_request_never_reads_the_task_from_the_backend(self):
        """AC-8864-7: a query or revoke refused by the owner check reads nothing of the task from the Celery backend."""
        owner = self.logged(self.owner)
        launch(owner, 'task-own')
        self.assertServed(owner.get(status_url('task-own')), 'task-own', 'precondition: the owner is served')
        self.assertTrue(self.async_result_class.called, msg='precondition: the owner\'s read reaches the backend')

        for case in DENIALS:
            for method in ('get', 'post'):
                self.async_result_class.reset_mock()
                self.celery_app.reset_mock()
                with self.subTest(case=case, method=method), self.denial(case) as (client, task_id):
                    getattr(client, method)(status_url(task_id))
                    self.async_result_class.assert_not_called()
                    self.assertEqual(self.celery_app.mock_calls, [], msg='nothing of the Celery app may be touched')

    def test_every_denial_is_answered_alike(self):
        """AC-8864-8: an id with no record of the user (never launched, record lost or dropped) is answered as another user's task."""
        for method in ('get', 'post'):
            with self.denial('foreign') as (client, task_id):
                reference = getattr(client, method)(status_url(task_id))
            self.assertDenied(reference, 'another user\'s task (%s)' % method)

            for case in ('unknown', 'lost', 'dropped', 'unreadable'):
                with self.subTest(method=method, case=case), self.denial(case) as (client, task_id):
                    response = getattr(client, method)(status_url(task_id))
                    self.assertDenied(response, case)
                    self.assertEqual(
                        (response.status_code, response.content, response['Content-Type']),
                        (reference.status_code, reference.content, reference['Content-Type']),
                        msg='%s: the same status and body as another user\'s task' % case)

    def test_the_same_user_in_another_login_session_is_served(self):
        """AC-8864-9 (controle): the user who launched the task reads and revokes it from another login session."""
        first = self.logged(self.owner)
        launch(first, 'task-9')
        second = self.logged(self.owner)
        self.assertNotEqual(first.session.session_key, second.session.session_key,
                            msg='precondition: two login sessions of the same user')

        self.assertServed(second.get(status_url('task-9')), 'task-9', 'the other login session')
        second.post(status_url('task-9'))
        self.revoke.assert_called_once_with('task-9', terminate=True)

    def test_an_unreadable_registry_is_not_the_owner(self):
        """AC-8864-10: a registry that cannot be read (raises, or answers empty) makes the task not the user's: 404, query and revoke."""
        owner = self.logged(self.owner)
        with self.owner_setting(OWNER_ALIAS, 'owner'):
            launch(owner, 'task-10')
            self.assertServed(owner.get(status_url('task-10')), 'task-10', 'precondition: the registry is readable')
        self.celery_app.reset_mock()

        for alias in ('broken', 'dropping'):
            for method in ('get', 'post'):
                with self.subTest(alias=alias, method=method), self.owner_setting(OWNER_ALIAS, alias):
                    self.assertDenied(getattr(owner, method)(status_url('task-10')), '%s / %s' % (alias, method))
        self.revoke.assert_not_called()

    def test_with_the_owner_check_off_any_authorized_user_is_served(self):
        """AC-8945-6, AC-8945-7 (controle): with the switch off, another authorized user reads and revokes a task with an id that is not empty, and the form records no owner."""
        owner = self.logged(self.owner)
        other = self.logged(self.other)
        with self.owner_setting(OWNERSHIP, False):
            launch(owner, 'task-12')
            self.assertServed(other.get(status_url('task-12')), 'task-12', 'another user, switch off')
            # o critério promete o id, o estado e a saída (ou o traceback): o dict inteiro, e não só o id e a saída
            data = json.loads(other.get(status_url('task-12')).content)['task']
            self.assertEqual(data, {'id': 'task-12', 'ready': True, 'failed': False, 'output': OUTPUT})
            response = other.post(status_url('task-12'))
            self.revoke.assert_called_once_with('task-12', terminate=True)
            self.assertEqual(json.loads(response.content), {'status': True})

            # um id com barra (o regex da rota o entrega inteiro) é servido e revogado como qualquer outro
            self.revoke.reset_mock()
            launch(owner, 'task-12/with-slash')
            self.assertServed(other.get(status_url('task-12/with-slash')), 'task-12/with-slash', 'an id with a slash')
            response = other.post(status_url('task-12/with-slash'))
            self.revoke.assert_called_once_with('task-12/with-slash', terminate=True)
            self.assertEqual(json.loads(response.content), {'status': True})

            # uma tarefa que falhou: o traceback no lugar da saída
            self.async_result_class.side_effect = lambda id=None, app=None, **kwargs: async_result(
                task_id=id, failed=True, traceback='Traceback (most recent call last): ... (8867)')
            data = json.loads(other.get(status_url('task-12-failed')).content)['task']
            self.assertEqual(data, {'id': 'task-12-failed', 'ready': True, 'failed': True,
                                    'traceback': 'Traceback (most recent call last): ... (8867)'})
            self.async_result_class.side_effect = self.read_result

        # com a chave desligada o formulário não gravou nada: ligada de novo, nem o próprio usuário acha o registro
        self.assertDenied(owner.get(status_url('task-12')), 'no record was written while the switch was off')

    def test_the_record_lasts_until_the_timeout(self):
        """AC-8864-13: 24 hours after the launch (the default timeout) the user who launched the task is answered as another user."""
        owner = self.logged(self.owner)
        launch(owner, 'task-13')
        launched_at = time.time()

        with mock.patch('time.time', return_value=launched_at + 86400 - 60):
            self.assertServed(owner.get(status_url('task-13')), 'task-13', 'a minute before the 24 hours')
        with mock.patch('time.time', return_value=launched_at + 86400 + 60):
            for method in ('get', 'post'):
                with self.subTest(method=method):
                    self.assertDenied(getattr(owner, method)(status_url('task-13')), 'a minute after the 24 hours (%s)' % method)
        self.revoke.assert_not_called()

    def test_a_shorter_configured_timeout_is_honoured(self):
        """AC-8864-13: a timeout configured shorter than 24 hours ends the record sooner, for the query and for the revoke."""
        owner = self.logged(self.owner)
        with self.owner_setting(OWNER_TIMEOUT, 120):
            launch(owner, 'task-13-short')
        launched_at = time.time()

        with mock.patch('time.time', return_value=launched_at + 60):
            self.assertServed(owner.get(status_url('task-13-short')), 'task-13-short', 'inside the configured timeout')
        with mock.patch('time.time', return_value=launched_at + 180):
            for method in ('get', 'post'):
                with self.subTest(method=method):
                    self.assertDenied(getattr(owner, method)(status_url('task-13-short')),
                                      'after the configured timeout (%s)' % method)
        self.revoke.assert_not_called()

    def test_the_owner_is_answered_405_for_the_methods_the_view_does_not_handle(self):
        """AC-8864-18 (controle): the user who launched the task is still answered 405 for PUT, PATCH and DELETE."""
        owner = self.launched_by_owner('task-18')

        for method in ('put', 'patch', 'delete'):
            with self.subTest(method=method):
                self.assertEqual(getattr(owner, method)(status_url('task-18')).status_code, 405)

    def test_the_owner_is_recorded_and_read_in_the_named_alias(self):
        """AC-8864-19: the record is written and read in the alias named by the setting, and in no other."""
        owner = self.logged(self.owner)
        with self.owner_setting(OWNER_ALIAS, 'owner'):
            launch(owner, 'task-19')
            self.assertServed(owner.get(status_url('task-19')), 'task-19', 'the named alias')
        with self.owner_setting(OWNER_ALIAS, 'owner_b'):
            self.assertDenied(owner.get(status_url('task-19')), 'another alias holds no record')
        self.assertDenied(owner.get(status_url('task-19')), 'the default alias holds no record')


class OwnerNoStoreTest(OwnerTestCase):
    """O `no-store` da resposta da rota status, com a view montada fora da URLconf do pacote."""

    def direct(self, method, user, task_id, **extra):
        request = getattr(RequestFactory(), method)(status_url(task_id), **extra)
        request.user = user
        return TaskFormStatus.as_view()(request, task_id=task_id)

    def test_every_response_of_the_status_view_is_no_store(self):
        """AC-8864-4: the answer to a query or revoke (served, refused by the owner check, or 405) is no-store, even off the URLconf."""
        self.launched_by_owner('task-4')

        answers = []
        for label, extra in (('navigation', {}), ('xhr', AJAX)):
            answers.append(('served get %s' % label, self.direct('get', self.owner, 'task-4', **extra), 200))
            answers.append(('served post %s' % label, self.direct('post', self.owner, 'task-4', **extra), 200))
            answers.append(('not found get %s' % label, self.direct('get', self.other, 'task-4', **extra), 404))
            answers.append(('not found post %s' % label, self.direct('post', self.other, 'task-4', **extra), 404))
            answers.append(('405 %s' % label, self.direct('put', self.owner, 'task-4', **extra), 405))
        self.assertEqual(len(answers), 10, msg='precondition: every form of answer is exercised')

        for label, response, status_code in answers:
            with self.subTest(answer=label):
                self.assertEqual(response.status_code, status_code, msg='%s: the status of the answer' % label)
                self.assertIn('no-store', response.headers.get('Cache-Control', ''),
                              msg='%s: the answer must be no-store' % label)


class OwnerFormTest(OwnerTestCase):
    """O formulário de execução e o registro do dono."""

    def test_a_registry_that_was_not_kept_leaves_an_error_with_the_task_id(self):
        """AC-8864-11, AC-8864-14: a record that is not kept (cache raises, drops the write, or is no alias) is logged with the id, and the launch still answers."""
        owner = self.logged(self.owner)

        for alias in ('broken', 'dropping', 'not-in-caches'):
            task_id = 'task-11-%s' % alias
            with self.subTest(alias=alias):
                with self.owner_setting(OWNER_ALIAS, alias), self.assertLogs(views.logger, level='ERROR') as logs:
                    launch(owner, task_id)  # o disparo responde o id (AC-8864-14) mesmo sem registro
                self.assertIn(task_id, '\n'.join(logs.output), msg='%s: the error must name the task' % alias)

    def test_a_refused_task_records_no_owner(self):
        """AC-8944-6 (controle): a broker that refuses the task is answered 400 and leaves nothing in the registry."""
        owner = self.logged(self.owner)
        with mock.patch('commands_async.views.precheck_before_launch'), \
                mock.patch('commands_async.views.command_exec') as task:
            task.apply_async.side_effect = RuntimeError('broker is down')
            response = owner.post(INDEX_URL, {'app_command': 'test_cmdasync.cmdasync_echo', 'args': '', 'kwargs': ''}, **AJAX)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(caches['default']._cache), 0, msg='no owner may be recorded for a task that was never queued')


@override_settings(CACHES={'default': {'BACKEND': LOCMEM, 'LOCATION': 'check-default-8864'},
                           'owner': {'BACKEND': LOCMEM, 'LOCATION': 'check-owner-8864'},
                           'dummy': {'BACKEND': 'django.core.cache.backends.dummy.DummyCache'}})
class OwnerCacheCheckTest(SimpleTestCase):
    """A verificação do Django que avisa o alias do registro que não guarda nada."""

    @staticmethod
    def owner_messages():
        """Os avisos do pacote sobre o alias do dono: os que nomeiam a configuração do alias."""
        return [m for m in checks.run_checks()
                if OWNER_ALIAS in '%s %s' % (m.msg, m.hint or '')]

    def test_an_alias_that_stores_nothing_is_warned(self):
        """AC-8864-20: with the owner check on, an alias missing from CACHES or a DummyCache is warned, naming the alias and the setting."""
        for alias in ('not-in-caches', 'dummy'):
            with self.subTest(alias=alias), mock.patch.object(app_settings, OWNER_ALIAS, alias), \
                    mock.patch.object(app_settings, OWNERSHIP, True):
                messages = self.owner_messages()
                self.assertEqual(len(messages), 1, msg='%s: one warning is expected' % alias)
                self.assertEqual(messages[0].level, checks.WARNING)
                self.assertIn(alias, '%s %s' % (messages[0].msg, messages[0].hint or ''),
                              msg='%s: the warning must name the alias' % alias)

    def test_a_working_alias_or_the_owner_check_off_is_not_warned(self):
        """AC-8864-20 (controle): a cache that stores, or the owner check off, raises no warning."""
        with mock.patch.object(app_settings, OWNERSHIP, True), mock.patch.object(app_settings, OWNER_ALIAS, 'not-in-caches'):
            self.assertTrue(self.owner_messages(), msg='precondition: the same registry with a missing alias is warned')
        with mock.patch.object(app_settings, OWNERSHIP, True), mock.patch.object(app_settings, OWNER_ALIAS, 'owner'):
            self.assertEqual(self.owner_messages(), [])
        with mock.patch.object(app_settings, OWNERSHIP, False), mock.patch.object(app_settings, OWNER_ALIAS, 'dummy'):
            self.assertEqual(self.owner_messages(), [])
