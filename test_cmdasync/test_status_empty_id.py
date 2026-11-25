# coding=utf-8
"""A rota status do pacote e o id da tarefa vazio (#8867).

Execução (container `django`, com o `pyenv` do projeto):
    cd /opt/project/packages/django-cmd-async && /opt/project/pyenv/bin/python runtests.py test_cmdasync.test_status_empty_id -v 2

O defeito que estes testes travam só aparece com o backend de resultados de verdade: o `AsyncResult(id='')` levanta `ValueError`
no primeiro `ready()`, porque o `get_key_for_task` do Celery recusa o id vazio (`celery/backends/base.py:986`), e os testes
antigos do pacote dublam o `AsyncResult` (por isso o 500 nunca apareceu). Aqui o `celery_app` do módulo `commands_async.views` é um
app de Celery de verdade, com `result_backend='cache+memory://'` (o `CacheBackend` herda o `get_key_for_task` do
`KeyValueStoreBackend`, como o `RedisBackend`) e `broker_url='memory://'`: nada sai do processo e nenhum teste abre conexão com o
broker do dev. O `AsyncResult` do módulo, a leitura do backend, o `revoke` e as conexões do app são espiões, para os critérios de
«nada ao Celery» olharem o que o app recebe e não a rede.

O estado do `cache+memory://` é global no processo (`celery/backends/cache.py:25`): estes testes só leem dele.

A chave `COMMANDS_ASYNC_TASK_OWNERSHIP` é trocada no módulo `commands_async.settings`, que a view lê na hora do uso (o ponto de
substituição do #8864). O cliente de teste não levanta a exceção da view (`raise_request_exception=False`): o 500 que o servidor
responderia aparece como falha do assert contra o 404 esperado, e não como erro do teste.
"""
import json
from contextlib import contextmanager
from hashlib import sha256
from types import SimpleNamespace
from unittest import mock

from celery import Celery
from celery.result import AsyncResult as RealAsyncResult
from django.conf import settings as project_settings
from django.contrib.auth.models import AnonymousUser
from django.core.cache import caches
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.views.generic import View

from commands_async import settings as app_settings
from commands_async.views import OWNER_KEY_PREFIX, TaskFormStatus, is_task_id, owner_key

from test_cmdasync.harness import AJAX, create_user, grant, launch, status_url

OWNERSHIP = 'COMMANDS_ASYNC_TASK_OWNERSHIP'
EMPTY_URL = status_url('')
NOT_FOUND = {'task': None}
#: todo método HTTP que a view conhece (a lista vem da própria `View`, e não de uma cópia dela aqui)
METHODS = tuple(View.http_method_names)
REQUESTS = (('navigation', {}), ('xhr', AJAX))
#: marca «a view foi chamada sem o `task_id` do endereço» (o `None` não serve: é um id que o chamador poderia passar)
MISSING = object()


class EmptyIdTestCase(TestCase):
    """Base: um app de Celery de verdade (sem Redis, sem broker) no lugar do `celery_app` da view, com espiões."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user('runner')
        cls.plain = create_user('plain')
        cls.permission = grant(cls.user, 'run_commands_8867')

    def setUp(self):
        caches['default'].clear()
        self.addCleanup(caches['default'].clear)
        self.app = Celery('test-8867', set_as_current=False, broker='memory://', backend='cache+memory://')
        self.addCleanup(self.app.close)
        backend = self.app.backend
        self.app.control  # o `control` é uma propriedade em cache: o espião tem de pegar a instância que a view vai usar
        patches = {
            # o `AsyncResult` do módulo, com o de verdade por baixo: o espião conta a construção e deixa o backend real responder
            'AsyncResult': mock.patch('commands_async.views.AsyncResult', wraps=RealAsyncResult),
            'get_task_meta': mock.patch.object(backend, 'get_task_meta', wraps=backend.get_task_meta),
            # o `revoke` não é de verdade nem no red: o teste não pode transmitir nada, nem a um broker em memória
            'revoke': mock.patch.object(self.app.control, 'revoke'),
            'connection_for_write': mock.patch.object(self.app, 'connection_for_write', wraps=self.app.connection_for_write),
            'connection_for_read': mock.patch.object(self.app, 'connection_for_read', wraps=self.app.connection_for_read),
            'connection': mock.patch.object(self.app, 'connection', wraps=self.app.connection),
        }
        self.spies = {}
        for name, patch in patches.items():
            self.spies[name] = patch.start()
            self.addCleanup(patch.stop)
        celery = mock.patch('commands_async.views.celery_app', self.app)
        celery.start()
        self.addCleanup(celery.stop)

        self.client = self.logged(self.user)

    @staticmethod
    def logged(user):
        client = Client(raise_request_exception=False)
        client.force_login(user)
        return client

    @contextmanager
    def ownership(self, on):
        with mock.patch.object(app_settings, OWNERSHIP, on):
            yield

    @contextmanager
    def permission_required(self):
        """O host nomeia uma permissão só para esta view (o atributo vem herdado do mixin: `create=True`)."""
        with mock.patch.object(TaskFormStatus, 'command_permission_name', self.permission, create=True):
            yield

    def touched(self):
        """O que do Celery o pedido tocou: a construção do `AsyncResult`, a leitura do backend, o `revoke` e as conexões."""
        return sorted(name for name, spy in self.spies.items() if spy.called)

    def reset_spies(self):
        for spy in self.spies.values():
            spy.reset_mock()

    def call(self, client, method, task_id='', **extra):
        return getattr(client, method)(status_url(task_id), **extra)

    def direct(self, method, user, task_id=MISSING, **extra):
        """A view chamada sem a URLconf (o `never_cache` de `urls.py:22-23` marcaria a resposta e esconderia a falta do no-store)."""
        request = getattr(RequestFactory(), method)(status_url('' if task_id is MISSING else task_id), **extra)
        request.user = user
        kwargs = {} if task_id is MISSING else {'task_id': task_id}
        try:
            return TaskFormStatus.as_view()(request, **kwargs)
        except Exception as error:
            self.fail('%s: the view raised %r (the server would answer 500)' % (method, error))

    def assertNotFound(self, response, method, label):
        self.assertEqual(response.status_code, 404, msg='%s: an empty id must be answered not found' % label)
        if method != 'head':  # o cliente de teste descarta o corpo da resposta ao HEAD
            self.assertEqual(json.loads(response.content), NOT_FOUND, msg='%s: the body of the answer' % label)

    def assertNoStore(self, response, label):
        self.assertIn('no-store', response.headers.get('Cache-Control', ''), msg='%s: the answer must be no-store' % label)

    def assertServedFromTheRealBackend(self, response, task_id, label):
        """O controle positivo: com um id comum a rota é servida pelo backend de verdade (e então o `AsyncResult` é construído)."""
        self.assertEqual(response.status_code, 200, msg='%s: a common id must be served' % label)
        self.assertEqual(json.loads(response.content)['task']['id'], task_id, msg='%s: the id the client sent' % label)


class RealBackendTest(EmptyIdTestCase):
    """O que estes testes supõem do Celery, provado contra o código de verdade (e não contra um dublê)."""

    def test_the_real_backend_refuses_an_empty_id_and_answers_a_common_one(self):
        """(sem ID) o `get_key_for_task` recusa o id vazio no primeiro `ready()` e atende um id comum: a suposição dos testes abaixo."""
        with self.assertRaises(ValueError, msg='the real backend must refuse an empty task id'):
            RealAsyncResult(id='', app=self.app).ready()
        self.assertEqual(RealAsyncResult(id='task-8867', app=self.app).state, 'PENDING')


class EmptyIdOwnershipOffTest(EmptyIdTestCase):
    """Com a posse desligada o id vazio não chega ao Celery: 404, no-store, nada pedido (AC-8867-1, -2, -3, -7)."""

    def test_an_empty_id_is_answered_not_found_with_any_method(self):
        """AC-8867-1: with the owner check off, an empty task id is answered 404 (JSON with `task` null, but for HEAD), with any method."""
        with self.ownership(False):
            self.assertServedFromTheRealBackend(self.client.get(status_url('task-1')), 'task-1', 'precondition')
            self.assertTrue(METHODS, msg='precondition: the view knows its methods')
            for method in METHODS:
                for label, extra in REQUESTS:
                    with self.subTest(method=method, request=label):
                        response = self.call(self.client, method, **extra)
                        self.assertNotFound(response, method, '%s %s' % (method, label))

    def test_an_empty_id_asks_nothing_of_celery(self):
        """AC-8867-2: with the owner check off, an empty task id never reads the result backend, never revokes, never opens a broker connection."""
        with self.ownership(False):
            # controle positivo: com um id comum os espiões registram o que a rota pede ao Celery
            self.assertServedFromTheRealBackend(self.client.get(status_url('task-2')), 'task-2', 'precondition (query)')
            self.assertTrue({'AsyncResult', 'get_task_meta'} <= set(self.touched()),
                            msg='precondition: a query of a common id reads the backend, and the spies see it')
            self.reset_spies()
            self.client.post(status_url('task-2'))
            self.assertTrue({'revoke', 'connection_for_write'} <= set(self.touched()),
                            msg='precondition: a revoke of a common id opens the broker connection and revokes, and the spies see it')

            for method in METHODS:
                for label, extra in REQUESTS:
                    self.reset_spies()
                    with self.subTest(method=method, request=label):
                        self.call(self.client, method, **extra)
                        self.assertEqual(self.touched(), [], msg='%s %s: nothing of Celery may be asked' % (method, label))

    def test_the_answer_to_an_empty_id_is_no_store_even_off_the_urlconf(self):
        """AC-8867-3: with the owner check off, the answer to an empty task id is no-store, also with the view mounted off the package URLconf."""
        with self.ownership(False):
            for method in METHODS:
                for label, extra in REQUESTS:
                    with self.subTest(route='urlconf', method=method, request=label):
                        self.assertNoStore(self.call(self.client, method, **extra), '%s %s through the URLconf' % (method, label))
                    with self.subTest(route='direct', method=method, request=label):
                        self.assertNoStore(self.direct(method, self.user, '', **extra), '%s %s without the URLconf' % (method, label))

    def test_a_view_mounted_without_a_task_id_is_answered_not_found(self):
        """AC-8867-7: with the owner check off, the view mounted off the package URLconf, called without a task id, answers 404 with `task` null."""
        with self.ownership(False):
            served = self.direct('get', self.user, 'task-7')
            self.assertServedFromTheRealBackend(served, 'task-7', 'precondition: with a task id the same view is served')
            for method in METHODS:
                for label, extra in REQUESTS:
                    self.reset_spies()
                    with self.subTest(method=method, request=label):
                        response = self.direct(method, self.user, **extra)
                        self.assertNotFound(response, method, '%s %s without a task id' % (method, label))
                        self.assertEqual(self.touched(), [], msg='%s %s: nothing of Celery may be asked' % (method, label))

    def test_a_holder_of_the_named_permission_is_answered_alike(self):
        """AC-8867-1, AC-8867-2, AC-8867-3: with a permission named and the owner check off, the user who holds it is answered 404 for an empty id, asks nothing of Celery and gets a no-store answer, as the user without a named permission does."""
        # reforço do verificador (#8867 v2): os outros testes desta classe não nomeiam permissão, e o critério define o usuário
        # autorizado também como «autenticado e com a permissão nomeada»
        with self.ownership(False), self.permission_required():
            self.assertServedFromTheRealBackend(self.client.get(status_url('task-perm')), 'task-perm',
                                                'precondition: the holder of the named permission is served')
            self.assertEqual(self.logged(self.plain).get(status_url('task-perm')).status_code, 403,
                             msg='precondition: the named permission is in force (the user without it is forbidden)')

            for method in METHODS:
                for label, extra in REQUESTS:
                    self.reset_spies()
                    with self.subTest(method=method, request=label):
                        response = self.call(self.client, method, **extra)
                        self.assertNotFound(response, method, '%s %s' % (method, label))
                        self.assertEqual(self.touched(), [], msg='%s %s: nothing of Celery may be asked' % (method, label))
                        self.assertNoStore(self.direct(method, self.user, '', **extra), '%s %s without the URLconf' % (method, label))


class EmptyIdKeptTest(EmptyIdTestCase):
    """O que a rota já fazia com o id vazio, e continua a fazer (AC-8867-5, -6, -8)."""

    def test_with_the_owner_check_on_an_empty_id_is_still_answered_not_found(self):
        """AC-8867-5, AC-8864-4, AC-8864-6, AC-8864-7 (controle): with the owner check on, an empty id is answered 404, no-store, and asks nothing of Celery."""
        with self.ownership(True):
            launch(self.client, 'task-5')
            self.assertServedFromTheRealBackend(self.client.get(status_url('task-5')), 'task-5', 'precondition: the owner is served')
            self.assertTrue({'AsyncResult', 'get_task_meta'} <= set(self.touched()),
                            msg='precondition: the owner\'s query reads the backend, and the spies see it')

            for method in METHODS:
                for label, extra in REQUESTS:
                    self.reset_spies()
                    with self.subTest(method=method, request=label):
                        response = self.call(self.client, method, **extra)
                        self.assertNotFound(response, method, '%s %s' % (method, label))
                        self.assertNoStore(response, '%s %s' % (method, label))
                        self.assertEqual(self.touched(), [], msg='%s %s: nothing of Celery may be asked' % (method, label))

    def test_the_gate_denies_an_empty_id_as_it_denies_any_id(self):
        """AC-8928-2 (controle): the login and the permission come before the empty id: anonymous and no-permission users are denied as with any id."""
        anonymous = Client(raise_request_exception=False)
        plain = self.logged(self.plain)
        with self.permission_required():
            with self.ownership(True):
                holder = self.client.get(EMPTY_URL)
                self.assertEqual(holder.status_code, 404, msg='precondition: the holder of the permission goes through the gate')
            self.assertEqual(plain.get(status_url('task-6')).status_code, 403, msg='precondition: the user without it is forbidden')

            for owned in (True, False):
                with self.ownership(owned):
                    for method in METHODS:
                        with self.subTest(ownership=owned, method=method):
                            response = self.call(anonymous, method)
                            self.assertRedirects(response, '/login/?next=%s' % EMPTY_URL, fetch_redirect_response=False,
                                                 msg_prefix='anonymous navigation')
                            response = self.call(anonymous, method, **AJAX)
                            self.assertEqual(response.status_code, 403, msg='an anonymous XHR must be denied, not redirected')
                            if method != 'head':
                                self.assertEqual(json.loads(response.content), NOT_FOUND)
                            response = self.call(plain, method)
                            self.assertEqual(response.status_code, 403, msg='no permission, navigation: forbidden')
                            response = self.call(plain, method, **AJAX)
                            self.assertEqual(response.status_code, 403, msg='no permission, XHR: forbidden')
                            if method != 'head':
                                self.assertEqual(json.loads(response.content), NOT_FOUND)

    def test_the_empty_id_sends_the_anonymous_to_the_login_url_the_package_names(self):
        """AC-8928-2 (controle): the anonymous navigation to the empty id goes to ``COMMANDS_ASYNC_LOGIN_URL``, not to the project's ``LOGIN_URL``."""
        # O LOGIN_URL do projeto de teste é '/login/' e a configuração do pacote o herda, então o teste do id vazio acima não
        # distingue os dois (test_views.py:403-405). Aqui o host aponta o pacote para outro lugar; a view lê a configuração na chamada.
        elsewhere = '/elsewhere/'
        self.assertNotEqual(project_settings.LOGIN_URL, elsewhere, msg='precondition: the two login pages differ')
        anonymous = Client(raise_request_exception=False)
        with mock.patch.object(app_settings, 'COMMANDS_ASYNC_LOGIN_URL', elsewhere):
            for owned in (True, False):
                with self.ownership(owned):
                    for method in METHODS:
                        with self.subTest(ownership=owned, method=method):
                            response = self.call(anonymous, method)
                            self.assertRedirects(response, '%s?next=%s' % (elsewhere, EMPTY_URL), fetch_redirect_response=False)

    def test_with_the_owner_check_on_a_view_mounted_without_a_task_id_is_answered_not_found(self):
        """AC-8867-8 (controle): with the owner check on, the view mounted off the package URLconf, called without a task id, answers 404 with `task` null."""
        with self.ownership(True):
            for method in METHODS:
                for label, extra in REQUESTS:
                    self.reset_spies()
                    with self.subTest(method=method, request=label):
                        response = self.direct(method, self.user, **extra)
                        self.assertNotFound(response, method, '%s %s without a task id' % (method, label))


class TaskIdPredicateTest(SimpleTestCase):
    """O predicado «é um id de tarefa» é um só, e o `owner_key` não muda de resposta ao usá-lo (revisor de design v1, ajuste 4)."""

    IDS = (None, '', ' ', 0, b'a', 'a', 'a/b')

    def test_is_task_id_is_text_that_is_not_empty_and_owner_key_keeps_its_answers(self):
        """(sem ID) `is_task_id` é «texto não vazio»; `owner_key` devolve a mesma chave de antes, ou `None` para o que não é de ninguém."""
        member = SimpleNamespace(pk=7)
        task_ids = [task_id for task_id in self.IDS if isinstance(task_id, str) and task_id]
        self.assertEqual(len(task_ids), 3, msg='precondition: some of the ids are task ids')
        for task_id in self.IDS:
            with self.subTest(task_id=repr(task_id)):
                expected = isinstance(task_id, str) and bool(task_id)
                self.assertIs(is_task_id(task_id), expected)
                key = OWNER_KEY_PREFIX + sha256(('%s\n%s' % (7, task_id)).encode('utf-8')).hexdigest() if expected else None
                self.assertEqual(owner_key(member, task_id), key)
                self.assertIsNone(owner_key(AnonymousUser(), task_id), msg='a user without a pk is nobody\'s owner')
