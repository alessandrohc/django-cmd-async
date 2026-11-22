# coding=utf-8
"""A rota status do pacote e a falha da leitura do backend de resultado do Celery (#8945).

Execução (container `django`, com o `pyenv` do projeto):
    cd /opt/project/packages/django-cmd-async && /opt/project/pyenv/bin/python runtests.py test_cmdasync.test_status_backend_failure -v 2

O defeito só aparece com o backend de resultado de verdade: os testes antigos do pacote dublam o `AsyncResult` e nunca veem a leitura
falhar. Aqui o `celery_app` do módulo `commands_async.views` é um app de Celery de verdade, com `result_backend='cache+memory://'` e
`broker_url='memory://'` (como no `test_status_empty_id.py`), e a falha é **simulada na leitura da chave** do backend (`backend.get`,
o que o `redis-py` levantaria): a cadeia do Celery acima dela (`AsyncResult` -> `get_task_meta` -> `_get_task_meta_for`) roda de
verdade, e o `AsyncResult` não é dublado. O pacote não depende do `redis` (`pyproject.toml`: `celery>=5.5`): nenhum teste importa
`redis` nem abre conexão, e a falha é de tipos do Python que um backend levanta (conexão recusada, tempo esgotado, exceção qualquer).

A consulta lê o estado da tarefa uma vez só (#8962; antes eram até 3 leituras da chave por consulta, `ready()`, `failed()` e `result`, porque
o Celery só guarda em cache o resultado pronto, `celery/result.py:433-445`): a leitura que falha é a 1ª e a única. O número de leituras
por estado é do `test_status_single_read.py`. Os testes usam ids que nunca foram gravados (PENDING). O estado do `cache+memory://` é
global no processo (`celery/backends/cache.py:25`): cada teste usa ids próprios e eles só leem dele.

A posse (`COMMANDS_ASYNC_TASK_OWNERSHIP`) é trocada no módulo `commands_async.settings`, que a view lê na hora do uso (o ponto de
substituição do #8864). O cliente de teste não levanta a exceção da view (`raise_request_exception=False`): o 500 que o servidor
responderia aparece como resposta e como falha do assert contra o 503 esperado, e não como erro do teste.
"""
import json
import logging
from contextlib import contextmanager
from unittest import mock

from celery import Celery
from celery.result import AsyncResult as RealAsyncResult
from django.core.cache import caches
from django.test import Client, RequestFactory, TestCase
from django.views.generic import View

from commands_async import settings as app_settings
from commands_async import tasks, views
from commands_async.views import TaskFormStatus

from test_cmdasync.harness import AJAX, create_user, grant, launch, status_url

OWNERSHIP = 'COMMANDS_ASYNC_TASK_OWNERSHIP'
#: o que o erro do backend traria ao usuário se a rota o repassasse: o texto e o endereço do Redis (o do kombu e o do redis-py os trazem)
BACKEND_ADDRESS = 'redis-secret.internal:6379'
ERRORS = (
    ('connection refused', lambda: ConnectionRefusedError('Error 111 connecting to %s. Connection refused.' % BACKEND_ADDRESS)),
    ('timeout', lambda: TimeoutError('Timeout reading from %s' % BACKEND_ADDRESS)),
    ('any other error', lambda: RuntimeError('backend exploded at %s' % BACKEND_ADDRESS)),
)
#: a leitura que falha: a consulta faz uma só (#8962); com as 3 de antes, a 2ª e a 3ª também eram testadas
READS = (1,)
METHODS = ('get', 'head')
OWNED = (True, False)


class RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class BackendFailureTestCase(TestCase):
    """Base: um app de Celery de verdade (sem Redis, sem broker) no lugar do `celery_app` da view, e a leitura da chave espiada."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user('runner')
        # o usuário que tem a permissão nomeada e o que não a tem: só o teste do eixo da permissão os usa (reforço do verificador)
        cls.plain = create_user('plain')
        cls.permission = grant(cls.user, 'run_commands_8945')

    def setUp(self):
        caches['default'].clear()
        self.addCleanup(caches['default'].clear)
        self.app = Celery('test-8945', set_as_current=False, broker='memory://', backend='cache+memory://')
        self.addCleanup(self.app.close)
        self.backend = self.app.backend
        #: as chaves que o backend foi chamado a ler, na ordem
        self.reads = []
        #: a leitura (1-based) que levanta, e o que ela levanta; `None`: o backend responde
        self.failing_read = None
        self.error = None
        real_get = self.backend.get

        def get(key):
            self.reads.append(key)
            if self.failing_read == len(self.reads):
                raise self.error
            return real_get(key)

        for patch in (mock.patch.object(self.backend, 'get', side_effect=get), mock.patch('commands_async.views.celery_app', self.app)):
            patch.start()
            self.addCleanup(patch.stop)
        self.counter = 0
        self.client = Client(raise_request_exception=False)
        self.client.force_login(self.user)

    def task_id(self, name):
        """Um id de tarefa que o backend nunca gravou (PENDING), próprio de cada chamada."""
        self.counter += 1
        return 'task-8945-%s-%d' % (name, self.counter)

    @contextmanager
    def ownership(self, on):
        with mock.patch.object(app_settings, OWNERSHIP, on):
            yield

    @contextmanager
    def permission_required(self):
        """O host nomeia uma permissão só para esta view (o atributo vem herdado do mixin: `create=True`), como no `test_status_empty_id.py`."""
        with mock.patch.object(TaskFormStatus, 'command_permission_name', self.permission, create=True):
            yield

    def fails_on(self, read, make_error):
        self.failing_read = read
        self.error = make_error()
        self.reads[:] = []

    def healthy(self):
        self.failing_read = None
        self.error = None
        self.reads[:] = []

    def query(self, owned, method, name, **extra):
        """Consulta pela URLconf, do dono (a tarefa foi disparada pelo formulário) ou com a posse desligada: a resposta e o id."""
        task_id = self.task_id(name)
        if owned:
            launch(self.client, task_id)
        self.reads[:] = []
        return getattr(self.client, method)(status_url(task_id), **extra), task_id

    def direct(self, owned, name, **extra):
        """A view chamada sem a URLconf (o `never_cache` de `urls.py` marcaria a resposta e esconderia a falta do cabeçalho)."""
        task_id = self.task_id(name)
        if owned:
            launch(self.client, task_id)
        self.reads[:] = []
        request = RequestFactory().get(status_url(task_id), **extra)
        request.user = self.user
        try:
            return TaskFormStatus.as_view()(request, task_id=task_id)
        except Exception as error:
            self.fail('the view raised %r (the server would answer 500)' % (error,))

    @contextmanager
    def package_log(self):
        """Os registros do log do pacote durante o bloco, sem deixá-los chegar ao stderr do runner."""
        # a view usa o `logger` de `commands_async.tasks` (`views.py:17`): o mesmo objeto, conferido aqui
        self.assertIs(views.logger, tasks.logger, msg='precondition: the view logs in the logger of the package')
        handler = RecordingHandler()
        propagate = tasks.logger.propagate
        tasks.logger.addHandler(handler)
        tasks.logger.propagate = False
        try:
            yield handler.records
        finally:
            tasks.logger.removeHandler(handler)
            tasks.logger.propagate = propagate

    def assertServedPending(self, response, task_id, label):
        """O controle positivo: com o backend respondendo, a tarefa em PENDING é servida (o número de leituras é do `test_status_single_read.py`)."""
        self.assertEqual(response.status_code, 200, msg='%s: a healthy backend must be served' % label)
        self.assertEqual(json.loads(response.content),
                         {'task': {'id': task_id, 'ready': False, 'failed': False, 'output': None}}, msg=label)


class BackendFailureAnswerTest(BackendFailureTestCase):
    """O que a rota responde, registra e deixa de ler quando a leitura do backend de resultado levanta (AC-8945-1 … -4)."""

    def test_a_failing_read_is_answered_503(self):
        """AC-8945-1: when a read of the result backend raises, the status route answers 503 (JSON with `task` null on GET), without the error text or the backend address."""
        with self.package_log():
            for owned in OWNED:
                with self.ownership(owned):
                    self.healthy()
                    response, task_id = self.query(owned, 'get', 'precondition')
                    self.assertServedPending(response, task_id, 'precondition (owned=%s)' % owned)
                    for method in METHODS:
                        for read in READS:
                            for kind, make_error in ERRORS:
                                with self.subTest(owned=owned, method=method, read=read, error=kind):
                                    self.fails_on(read, make_error)
                                    response, _ = self.query(owned, method, 'failing')
                                    self.assertEqual(response.status_code, 503,
                                                     msg='a failing read of the backend must be answered 503, not left to the server error')
                                    text = response.content.decode('utf-8')
                                    if method == 'get':
                                        self.assertEqual(json.loads(response.content), {'task': None},
                                                         msg='the body of the 503 is the contract of the route with `task` null')
                                    for secret in (BACKEND_ADDRESS, str(self.error)):
                                        self.assertNotIn(secret, text, msg='the answer must not carry the error text or the backend address')

    def test_a_failing_read_is_logged_without_the_task_id(self):
        """AC-8945-2: when a read of the result backend raises, the error is logged in the package log, with the exception, and the message does not carry the task id."""
        for owned in OWNED:
            with self.ownership(owned):
                for method in METHODS:
                    for read in READS:
                        for kind, make_error in ERRORS:
                            with self.subTest(owned=owned, method=method, read=read, error=kind), self.package_log() as records:
                                self.fails_on(read, make_error)
                                task_id = self.task_id('logged')
                                if owned:
                                    launch(self.client, task_id)
                                self.reads[:] = []
                                records[:] = []
                                getattr(self.client, method)(status_url(task_id))
                                errors = [record for record in records if record.levelno >= logging.ERROR]
                                self.assertEqual(len(errors), 1, msg='the failure must be logged once, as an error')
                                record = errors[0]
                                self.assertIsNotNone(record.exc_info, msg='the log carries the exception (its traceback)')
                                self.assertIs(type(record.exc_info[1]), type(self.error), msg='the exception logged is the one the backend raised')
                                self.assertNotIn(task_id, record.getMessage(),
                                                 msg='the message of the record must not carry the task id (client text with the owner check off)')

    def test_nothing_is_read_after_the_read_that_failed(self):
        """AC-8945-3 (controle): when a read of the result backend raises, the route asks the backend for no read after the one that failed, nor repeats it."""
        for owned in OWNED:
            with self.ownership(owned), self.package_log():
                self.healthy()
                response, task_id = self.query(owned, 'get', 'precondition')
                self.assertServedPending(response, task_id, 'precondition (owned=%s)' % owned)
                for method in METHODS:
                    for read in READS:
                        for kind, make_error in ERRORS:
                            with self.subTest(owned=owned, method=method, read=read, error=kind):
                                self.fails_on(read, make_error)
                                self.query(owned, method, 'counted')
                                self.assertEqual(len(self.reads), read,
                                                 msg='the reads the backend received must stop at the one that failed (no read after it, no repeat)')

    def test_the_503_is_no_store_even_off_the_urlconf(self):
        """AC-8945-4: the 503 for a failing read of the result backend is no-store, with or without XHR, also with the view mounted off the package URLconf."""
        with self.package_log():
            for owned in OWNED:
                with self.ownership(owned):
                    self.healthy()
                    served = self.direct(owned, 'precondition')
                    self.assertEqual(served.status_code, 200, msg='precondition: the same view is served with a healthy backend')
                    for label, extra in (('navigation', {}), ('xhr', AJAX)):
                        for read in READS:
                            with self.subTest(owned=owned, request=label, read=read):
                                self.fails_on(read, ERRORS[0][1])
                                response = self.direct(owned, 'failing', **extra)
                                self.assertEqual(response.status_code, 503, msg='precondition: the failing read is answered 503')
                                self.assertIn('no-store', response.headers.get('Cache-Control', ''),
                                              msg='the 503 must be no-store, off the URLconf (the `never_cache` of urls.py is not there)')


    def test_a_holder_of_the_named_permission_is_answered_alike(self):
        """AC-8945-1, AC-8945-2, AC-8945-3, AC-8945-4: with a permission named, the user who holds it gets the same 503 (JSON with `task` null, no error text or backend address), the one error record without the task id, no read after the failed one, and no-store off the URLconf."""
        # reforço do verificador (#8945 v1): os outros testes desta classe não nomeiam permissão, e o critério define o usuário
        # autorizado também como «autenticado e com a permissão nomeada»
        plain = Client(raise_request_exception=False)
        plain.force_login(self.plain)
        with self.package_log() as records, self.permission_required():
            for owned in OWNED:
                with self.ownership(owned):
                    self.healthy()
                    response, task_id = self.query(owned, 'get', 'holder-precondition')
                    self.assertServedPending(response, task_id, 'precondition: the holder of the named permission is served (owned=%s)' % owned)
                    self.assertEqual(plain.get(status_url(self.task_id('holder-plain'))).status_code, 403,
                                     msg='precondition: the named permission is in force (the user without it is forbidden)')
                    # a leitura falha (a única da consulta): nenhuma leitura pode sair depois dela
                    kind, make_error = ERRORS[1]
                    with self.subTest(owned=owned, read=1, error=kind):
                        self.fails_on(1, make_error)
                        records[:] = []
                        response, task_id = self.query(owned, 'get', 'holder-failing')
                        self.assertEqual(response.status_code, 503, msg='a failing read must be answered 503 for the holder of the permission too')
                        self.assertEqual(json.loads(response.content), {'task': None})
                        for secret in (BACKEND_ADDRESS, str(self.error)):
                            self.assertNotIn(secret, response.content.decode('utf-8'), msg='the answer must not carry the error text or the backend address')
                        errors = [record for record in records if record.levelno >= logging.ERROR]
                        self.assertEqual(len(errors), 1, msg='the failure must be logged once, as an error')
                        self.assertNotIn(task_id, errors[0].getMessage(), msg='the message of the record must not carry the task id')
                        self.assertEqual(len(self.reads), 1, msg='the reads must stop at the one that failed')
                    with self.subTest(owned=owned, read=1, error=kind, route='direct'):
                        self.fails_on(1, make_error)
                        response = self.direct(owned, 'holder-direct')
                        self.assertEqual(response.status_code, 503, msg='precondition: the failing read is answered 503')
                        self.assertIn('no-store', response.headers.get('Cache-Control', ''), msg='the 503 must be no-store, off the URLconf')


class BackendRespondingTest(BackendFailureTestCase):
    """O que continua igual com o backend respondendo, provado com o backend de resultado de verdade."""

    def test_a_healthy_backend_serves_the_task(self):
        """AC-8962-3, AC-8945-6 (controle): with the result backend answering, the query of a pending task is served 200 with its id, state and output, with the owner check on or off."""
        for owned in OWNED:
            with self.subTest(owned=owned), self.ownership(owned):
                self.healthy()
                response, task_id = self.query(owned, 'get', 'healthy')
                self.assertServedPending(response, task_id, 'owned=%s' % owned)
                self.assertIn('no-store', response.headers.get('Cache-Control', ''), msg='a served answer is no-store')


class AssumptionsTest(BackendFailureTestCase):
    """O que os testes acima supõem do Celery e do Django, provado contra o código de verdade (e não contra um dublê)."""

    def test_a_failing_read_of_the_key_reaches_the_async_result_unwrapped(self):
        """(sem ID) a leitura da chave que levanta sobe pela cadeia do Celery até `ready()` como levantada: não vira PENDING nem é embrulhada."""
        self.fails_on(1, ERRORS[0][1])
        with self.assertRaises(ConnectionRefusedError, msg='the failure of the read must reach the caller as raised'):
            RealAsyncResult(id=self.task_id('chain'), app=self.app).ready()
        self.healthy()
        self.assertEqual(RealAsyncResult(id=self.task_id('chain'), app=self.app).state, 'PENDING',
                         msg='precondition: with a healthy backend an unknown id is PENDING')

    def test_head_runs_the_get_of_the_view(self):
        """(sem ID) a view não tem `head` próprio: o Django põe o `get` no lugar (`django/views/generic/base.py:128-129`), então o critério vale para os dois métodos."""
        self.assertFalse(hasattr(TaskFormStatus, 'head'), msg='the view has no head of its own')
        self.assertIn('head', View.http_method_names)
        request = RequestFactory().head(status_url('task'))
        view = TaskFormStatus()
        view.setup(request, task_id='task')
        self.assertEqual(view.head, view.get, msg='HEAD must run the get of the view')
