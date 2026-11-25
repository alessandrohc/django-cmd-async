# coding=utf-8
"""A rota status do pacote lê o backend de resultado uma vez por consulta, e a resposta descreve um estado só (#8962).

Execução (container `django`, com o `pyenv` do projeto):
    cd /opt/project/packages/django-cmd-async && /opt/project/pyenv/bin/python runtests.py test_cmdasync.test_status_single_read -v 2

Como no `test_status_backend_failure.py` (a base `BackendFailureTestCase` vem de lá), o `celery_app` do módulo `commands_async.views` é um
app de Celery de verdade, com `result_backend='cache+memory://'` e `broker_url='memory://'`, e as leituras da chave são contadas por um
espião em `backend.get`: a cadeia do Celery acima dela (`AsyncResult` -> `get_task_meta` -> `_get_task_meta_for`) roda de verdade, e o
`AsyncResult` não é dublado. Só o estado pronto vai ao cache do `AsyncResult` (`celery/result.py:433-445`): a tarefa pendente, iniciada,
em andamento ou em nova tentativa custava 3 leituras da chave por consulta, a pronta (SUCCESS, FAILURE, REVOKED), 1.

Os estados são gravados no backend com `store_result` ANTES de a contagem zerar: o `store_result` do backend de chave-valor lê a chave
(`_store_result` consulta o estado atual), e essa leitura não é da rota. A tarefa PENDING é um id que o backend nunca gravou. O estado
próprio da tarefa (PROGRESS) não existe no Celery: é o que uma tarefa grava com `update_state`.

Os estados RETRY e REVOKED têm como resultado uma exceção: a rota a devolve como texto no `output` (200, #8963). A tarefa é gravada com
`store_result`, que é o que o `mark_as_revoked` e o `mark_as_retry` do worker fazem (`celery/backends/base.py:290-307`).
"""
import gc
import json
import sys
from unittest import mock

from celery import Celery, states
from celery.exceptions import Retry, TaskRevokedError
from celery.result import AsyncResult as RealAsyncResult
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponseServerError
from django.test import Client, RequestFactory

from commands_async.views import TaskFormStatus

from test_cmdasync.harness import launch, status_url
from test_cmdasync.test_status_backend_failure import BACKEND_ADDRESS, BackendFailureTestCase, METHODS, OWNED

#: o estado próprio da tarefa (não é um estado do Celery): o que `update_state(state='PROGRESS', meta=...)` grava
PROGRESS = 'PROGRESS'
#: o que o backend guarda, por estado; PENDING não tem linha (o backend não conhece o id)
STORED = {
    'STARTED': {'state': states.STARTED, 'result': {'pid': 4321, 'hostname': 'worker@host'}},
    PROGRESS: {'state': PROGRESS, 'result': {'done': 3, 'of': 9}},
    'RETRY': {'state': states.RETRY, 'result': ValueError('retry me')},
    'SUCCESS': {'state': states.SUCCESS, 'result': 'command output'},
    'FAILURE': {'state': states.FAILURE, 'result': ValueError('boom'), 'traceback': 'Traceback (most recent call last): ...'},
    'REVOKED': {'state': states.REVOKED, 'result': TaskRevokedError('revoked')},
}
#: os 7 estados do AC-8962-1, na ordem do critério
ALL_STATES = ('PENDING', 'STARTED', 'RETRY', 'SUCCESS', 'FAILURE', 'REVOKED', PROGRESS)
#: os estados do corpo do teste do AC-8963-3 e -4; a tarefa revogada e a em nova tentativa são dos AC-8963-1 e -2 (`ExceptionResultTest`)
SERVED_STATES = ('PENDING', 'STARTED', PROGRESS, 'SUCCESS', 'FAILURE')
#: de qual estado para qual a tarefa passa durante a consulta (AC-8962-2): a 1ª leitura vê o primeiro, as seguintes veriam o segundo; a
#: consulta logo depois do Revoke (STARTED -> REVOKED) é o cenário do #8963
TRANSITIONS = (('PENDING', 'FAILURE'), ('PENDING', 'SUCCESS'), ('STARTED', 'FAILURE'), ('STARTED', 'REVOKED'), ('STARTED', 'RETRY'))


class DerivedStatusView(TaskFormStatus):
    """Uma view derivada da do pacote e montada fora da URLconf dele, sem sobrescrever o `get` (a `OwnedTaskStatusView` do `plus_base`, `plus_base/publique/views/commands_async.py:227`, é uma)."""


class BrokenBackendApp(Celery):
    """Um app de Celery cujo backend de resultado não pode ser montado (a configuração dele é inválida)."""

    @property
    def backend(self):
        raise ImproperlyConfigured('no result backend can be built for %s' % BACKEND_ADDRESS)


class SingleReadTestCase(BackendFailureTestCase):
    """Base: o app de verdade e o espião da base, mais os estados gravados no backend e a mudança de estado entre as leituras."""

    def setUp(self):
        super().setUp()
        #: o espião da base (registra a leitura em `self.reads` e devolve o que o backend tem)
        self.recorded_get = self.backend.get.side_effect

    def store(self, task_id, name):
        """Grava no backend o estado `name`; PENDING é não gravar nada. A leitura que o `store_result` faz da chave não conta."""
        if name != 'PENDING':
            self.backend.store_result(task_id, **STORED[name])
        self.reads[:] = []

    def raw(self, task_id):
        """O que o backend guarda hoje para a tarefa, como o cache o devolve (`None` para a tarefa que ele não conhece)."""
        value = self.recorded_get(self.backend.get_key_for_task(task_id))
        self.reads[:] = []
        return value

    def first_read_sees(self, task_id, raw):
        """A 1ª leitura da chave da tarefa devolve `raw`; as seguintes, o que o backend tem (a tarefa mudou de estado depois dela)."""
        key = self.backend.get_key_for_task(task_id)
        served = []

        def get(requested):
            value = self.recorded_get(requested)
            if requested == key and not served:
                served.append(requested)
                return raw
            return value

        self.backend.get.side_effect = get

    def body(self, task_id, name):
        """O corpo do 200 que a rota dá para o estado `name` (o que o AC-8963-3 e o AC-8963-4 prometem)."""
        task = {'id': task_id, 'ready': name in ('SUCCESS', 'FAILURE'), 'failed': name == 'FAILURE'}
        if name == 'FAILURE':
            task['traceback'] = STORED[name]['traceback']
        else:
            task['output'] = STORED[name]['result'] if name in STORED else None
        return {'task': task}

    def serve(self, owned, method, label, name):
        """Consulta pela URLconf a tarefa no estado `name` (do dono, ou com a posse desligada): a resposta e o id."""
        task_id = self.task_id(label)
        if owned:
            launch(self.client, task_id)
        self.store(task_id, name)
        return getattr(self.client, method)(status_url(task_id)), task_id

    def derived_response(self, task_id, method):
        """A resposta da view derivada, chamada fora da URLconf. A exceção da serialização (o 500 que o servidor responderia) vira a resposta 500, para o assert do 200 a acusar."""
        request = getattr(RequestFactory(), method)(status_url(task_id))
        request.user = self.user
        try:
            return DerivedStatusView.as_view()(request, task_id=task_id)
        except TypeError:
            return HttpResponseServerError()

    def serve_derived(self, owned, method, label, name):
        """Como `serve`, com a view derivada chamada fora da URLconf."""
        task_id = self.task_id(label)
        if owned:
            launch(self.client, task_id)
        self.store(task_id, name)
        return self.derived_response(task_id, method), task_id


class SingleReadTest(SingleReadTestCase):
    """O estado da tarefa é pedido ao backend de resultado uma vez por consulta, em todo estado (AC-8962-1)."""

    def test_the_state_is_read_once_in_every_state(self):
        """AC-8962-1: when an authorized user queries the status of a task, by GET or HEAD, the route asks the result backend for one read of the state, whatever the state (PENDING, STARTED, RETRY, SUCCESS, FAILURE, REVOKED and an own state of the task)."""
        for owned in OWNED:
            with self.ownership(owned):
                for method in METHODS:
                    for name in ALL_STATES:
                        with self.subTest(owned=owned, method=method, state=name):
                            self.healthy()
                            response, task_id = self.serve(owned, method, 'once', name)
                            # a pré-condição positiva: a rota chegou à leitura e serve a tarefa (a negativa do dono não lê nada, e 0 leituras passaria com qualquer código)
                            self.assertEqual(response.status_code, 200, msg='precondition: a %s task is served' % name)
                            self.assertEqual(len(self.reads), 1,
                                             msg='a query of a %s task must read the result backend once, not once per field of the answer' % name)

    def test_the_state_is_read_once_with_the_view_mounted_off_the_urlconf(self):
        """AC-8962-1: the same single read when the view is mounted off the package URLconf, a view derived from the one of the package that does not override the get (what the plus_base serves)."""
        self.assertIs(DerivedStatusView.get, TaskFormStatus.get, msg='precondition: the derived view inherits the get of the package')
        for owned in OWNED:
            with self.ownership(owned):
                for method in METHODS:
                    for name in ALL_STATES:
                        with self.subTest(owned=owned, method=method, state=name):
                            self.healthy()
                            response, task_id = self.serve_derived(owned, method, 'derived', name)
                            self.assertEqual(response.status_code, 200, msg='precondition: a %s task is served by the derived view' % name)
                            self.assertEqual(len(self.reads), 1,
                                             msg='the derived view must read the result backend once for a %s task' % name)

    def test_the_state_is_read_once_for_the_holder_of_the_named_permission(self):
        """AC-8962-1: with a permission named, the user who holds it gets the same single read for a task that has not finished."""
        # o eixo da permissão do critério (o usuário autorizado é também «autenticado e com a permissão nomeada»)
        plain = Client(raise_request_exception=False)
        plain.force_login(self.plain)
        with self.permission_required():
            self.assertEqual(plain.get(status_url(self.task_id('holder-plain'))).status_code, 403,
                             msg='precondition: the named permission is in force (the user without it is forbidden)')
            for owned in OWNED:
                with self.subTest(owned=owned), self.ownership(owned):
                    self.healthy()
                    response, task_id = self.serve(owned, 'get', 'holder', 'PENDING')
                    self.assertEqual(response.status_code, 200, msg='precondition: the holder of the named permission is served')
                    self.assertEqual(len(self.reads), 1, msg='the holder of the named permission reads the result backend once too')


class ChangingStateTest(SingleReadTestCase):
    """A resposta é o estado que o backend tinha na leitura (AC-8962-2) e o corpo por estado segue o mesmo (AC-8963-3, AC-8963-4)."""

    def test_the_answer_is_the_state_of_the_read_when_the_task_changes_state(self):
        """AC-8962-2: when the state of a task changes in the result backend while the query is served, the answer is the state the backend had at the read of this request, with its ready, failed and output or traceback."""
        for owned in OWNED:
            with self.ownership(owned):
                for earlier, later in TRANSITIONS:
                    with self.subTest(owned=owned, earlier=earlier, later=later):
                        self.healthy()
                        task_id = self.task_id('changing')
                        if owned:
                            launch(self.client, task_id)
                        self.store(task_id, earlier)
                        raw_earlier = self.raw(task_id)
                        self.store(task_id, later)
                        self.first_read_sees(task_id, raw_earlier)
                        response = self.client.get(status_url(task_id))
                        self.assertEqual(response.status_code, 200, msg='precondition: the task is served')
                        self.assertEqual(json.loads(response.content), self.body(task_id, earlier),
                                         msg='the answer must describe the state of the read (%s), not a mix with the later one (%s)' % (earlier, later))
                        # o controle positivo: depois da 1ª leitura o backend já está no estado novo (o cenário existiu)
                        self.assertEqual(RealAsyncResult(id=task_id, app=self.app).state, later,
                                         msg='precondition: the task did change state after the first read')

    def test_a_second_query_of_the_same_task_reads_its_new_state(self):
        """AC-8962-2: the state of the answer is the one the backend had at the read of this request: a second query of the same task, after it changed state, answers the new state, with one read each."""
        # reforço do verificador (#8962 v1): os outros casos consultam um id novo uma vez, e um instantâneo que sobrevivesse à requisição
        # (um atributo de classe, ou guardado fora do `get`) passaria neles e deixaria a tela, que consulta de 1 em 1 s, presa no 1º estado
        for owned in OWNED:
            with self.ownership(owned):
                for earlier, later in (('PENDING', 'SUCCESS'), ('STARTED', 'FAILURE')):
                    with self.subTest(owned=owned, earlier=earlier, later=later):
                        self.healthy()
                        task_id = self.task_id('twice')
                        if owned:
                            launch(self.client, task_id)
                        self.store(task_id, earlier)
                        first = self.client.get(status_url(task_id))
                        self.assertEqual(json.loads(first.content), self.body(task_id, earlier), msg='precondition: the first query answers the first state')
                        self.assertEqual(len(self.reads), 1, msg='the first query reads the backend once')
                        self.store(task_id, later)
                        second = self.client.get(status_url(task_id))
                        self.assertEqual(json.loads(second.content), self.body(task_id, later),
                                         msg='the second query must answer the new state (%s), not the one read by the first (%s)' % (later, earlier))
                        self.assertEqual(len(self.reads), 1, msg='the second query reads the backend once too')

    def test_the_body_of_the_answer_in_each_state(self):
        """AC-8963-3, AC-8963-4 (controle): with the result backend answering, the 200 carries the id, whether the task finished, whether it failed and, by the state, the traceback or the output as the backend has it (null while it has none, the meta of the worker while it runs), with the owner check on or off."""
        for owned in OWNED:
            with self.ownership(owned):
                for name in SERVED_STATES:
                    with self.subTest(owned=owned, state=name):
                        self.healthy()
                        response, task_id = self.serve(owned, 'get', 'body', name)
                        self.assertEqual(response.status_code, 200, msg='a %s task is served' % name)
                        self.assertEqual(json.loads(response.content), self.body(task_id, name),
                                         msg='the body of the answer for a %s task' % name)


#: o texto que a rota deve dar como `output` quando ele é o do `str()` da exceção que o backend devolve (o `Retry` do cancelamento perde a
#: mensagem na ida e volta por JSON, #8963): o teste o lê do backend em vez de fixá-lo
LEARNED = object()
#: o que o worker grava em REVOKED (`mark_as_revoked`) e o que a rota deve dar como `output`
REVOKED_CASES = (
    ('the reason of a revoke', lambda: TaskRevokedError('revoked'), 'revoked'),
    ('the reason of a terminate', lambda: TaskRevokedError('terminated'), 'terminated'),
    ('an exception with no text', lambda: TaskRevokedError(), ''),
)
#: o que o worker grava em RETRY (`mark_as_retry`): a exceção que motivou a tentativa, a sem texto, o `Retry` do cancelamento por perda de
#: conexão (o que o projeto alcança) e o `self.retry()` sem exceção, que grava `None`
RETRY_CASES = (
    ('the original exception', lambda: ValueError('flaky: will retry'), 'flaky: will retry'),
    ('an exception with no text', lambda: ValueError(), ''),
    ('the retry of a cancelled task', lambda: Retry(message='cancelled by Celery'), LEARNED),
    ('a retry with no exception', lambda: None, None),
)
ROUTES = ('urlconf', 'derived')


class ExceptionResultTest(SingleReadTestCase):
    """A tarefa revogada e a em nova tentativa são respondidas 200, com o texto da exceção do resultado como saída (AC-8963-1, AC-8963-2)."""

    def answer(self, owned, method, route, state, exception):
        """Consulta a tarefa que o worker deixou em `state` (REVOKED ou RETRY) com `exception` como resultado: a resposta e o id."""
        task_id = self.task_id('exception')
        if owned:
            launch(self.client, task_id)
        self.backend.store_result(task_id, exception, getattr(states, state))
        self.reads[:] = []
        if route == 'derived':
            return self.derived_response(task_id, method), task_id
        return getattr(self.client, method)(status_url(task_id)), task_id

    def assertAnsweredWithTheText(self, response, task_id, state, expected):
        """O corpo inteiro do GET: o id, `ready` (verdadeiro só em REVOKED), `failed` falso e o texto como `output`."""
        if expected is LEARNED:
            stored = RealAsyncResult(id=task_id, app=self.app).result
            self.assertIsInstance(stored, BaseException, msg='precondition: the backend returns the exception it stored')
            expected = str(stored)
            self.assertTrue(expected, msg='precondition: the text of the exception the backend returns is not empty')
        self.assertEqual(json.loads(response.content),
                         {'task': {'id': task_id, 'ready': state == 'REVOKED', 'failed': False, 'output': expected}},
                         msg='the body of the answer for a %s task: the output is the text of the exception of its result' % state)

    def check(self, state, cases):
        for owned in OWNED:
            with self.ownership(owned):
                for route in ROUTES:
                    for method in METHODS:
                        for label, make_exception, expected in cases:
                            with self.subTest(owned=owned, route=route, method=method, case=label):
                                self.healthy()
                                response, task_id = self.answer(owned, method, route, state, make_exception())
                                self.assertEqual(response.status_code, 200,
                                                 msg='a %s task must be answered 200: the exception of its result is not JSON, and the route answered 500' % state)
                                self.assertIn('no-store', response.headers.get('Cache-Control', ''),
                                              msg='the answer for a %s task is no-store, as every answer of the route' % state)
                                if method == 'get':
                                    self.assertAnsweredWithTheText(response, task_id, state, expected)

    def test_a_revoked_task_is_answered_200_with_the_text_of_its_exception(self):
        """AC-8963-1, AC-8864-4: when an authorized user queries, by GET or HEAD, a task the backend gives as revoked, the route answers 200 and, on GET, the id, ready true, failed false and the text of the exception stored for it (empty, not null, when it has no text); no-store, also off the URLconf."""
        self.check('REVOKED', REVOKED_CASES)

    def test_a_task_being_retried_is_answered_200_with_the_text_of_its_exception(self):
        """AC-8963-2, AC-8864-4: when an authorized user queries, by GET or HEAD, a task the backend gives as being retried, the route answers 200 and, on GET, the id, ready false, failed false and the text of the exception stored for it (empty when it has no text, null when the backend stored none); no-store, also off the URLconf."""
        self.check('RETRY', RETRY_CASES)

    def test_the_holder_of_the_named_permission_is_answered_alike(self):
        """AC-8963-1, AC-8963-2: with a permission named, the user who holds it gets the same 200 for a revoked task and for a task being retried."""
        # o eixo da permissão dos critérios (o usuário autorizado é também «autenticado e com a permissão nomeada»)
        plain = Client(raise_request_exception=False)
        plain.force_login(self.plain)
        with self.permission_required():
            self.assertEqual(plain.get(status_url(self.task_id('holder-plain'))).status_code, 403,
                             msg='precondition: the named permission is in force (the user without it is forbidden)')
            for owned in OWNED:
                with self.ownership(owned):
                    for state, make_exception, expected in (('REVOKED', lambda: TaskRevokedError('terminated'), 'terminated'),
                                                            ('RETRY', lambda: ValueError('flaky: will retry'), 'flaky: will retry')):
                        with self.subTest(owned=owned, state=state):
                            self.healthy()
                            response, task_id = self.answer(owned, 'get', 'urlconf', state, make_exception())
                            self.assertEqual(response.status_code, 200, msg='the holder of the named permission is answered 200 for a %s task' % state)
                            self.assertAnsweredWithTheText(response, task_id, state, expected)


class UnbuildableBackendTest(SingleReadTestCase):
    """O backend de resultado que não pode ser montado continua dando 503 (AC-8962-4)."""

    def unbuildable(self, owned, method, label, **extra):
        """Consulta a tarefa com o app cujo backend não monta; devolve a resposta e o que o `__del__` do `AsyncResult` reportou ao interpretador."""
        task_id = self.task_id(label)
        if owned:
            launch(self.client, task_id)
        reported = []
        broken = BrokenBackendApp('test-8962-broken', set_as_current=False, broker='memory://')
        with mock.patch('commands_async.views.celery_app', broken), \
                mock.patch.object(sys, 'unraisablehook', reported.append):
            response = getattr(self.client, method)(status_url(task_id), **extra)
            gc.collect()
        return response, reported

    def test_a_backend_that_cannot_be_built_is_answered_503(self):
        """AC-8962-4 (controle): when the result backend cannot be built, the route answers 503 (JSON with `task` null on GET), without the error text or the backend address, and no error is reported by the AsyncResult being collected."""
        # regressão (`SHALL CONTINUE TO`): o código de hoje já cumpre, então passa no red por desenho; os mutantes m-e e m-f do plano o provam
        # depois do green (o `get` que lê o backend antes do `try`, e o instantâneo sem o `remove_pending_result` próprio)
        # o app e o backend de verdade respondem: a rota serve a tarefa, então o 503 de baixo é do backend que não monta
        response, _ = self.serve(True, 'get', 'healthy', 'PENDING')
        self.assertEqual(response.status_code, 200, msg='precondition: the same route is served with a result backend that builds')
        for owned in OWNED:
            with self.ownership(owned):
                for method in METHODS:
                    with self.subTest(owned=owned, method=method):
                        response, reported = self.unbuildable(owned, method, 'broken')
                        self.assertEqual(response.status_code, 503, msg='a result backend that cannot be built must be answered 503, not left to the server error')
                        if method == 'get':
                            self.assertEqual(json.loads(response.content), {'task': None}, msg='the body of the 503 is the contract of the route')
                        self.assertNotIn(BACKEND_ADDRESS, response.content.decode('utf-8'), msg='the answer must not carry the error text')
                        self.assertEqual(reported, [],
                                         msg='collecting the AsyncResult must not report an ignored error each query (its __del__ must not open the backend again)')

    def test_a_backend_that_cannot_be_built_is_answered_503_to_the_holder_of_the_named_permission(self):
        """AC-8962-4 (controle): with a permission named, the user who holds it gets the same 503 for a result backend that cannot be built."""
        with self.permission_required():
            for owned in OWNED:
                with self.subTest(owned=owned), self.ownership(owned):
                    response, reported = self.unbuildable(owned, 'get', 'holder-broken')
                    self.assertEqual(response.status_code, 503, msg='the holder of the named permission is answered 503 too')
                    self.assertEqual(json.loads(response.content), {'task': None})
                    self.assertEqual(reported, [], msg='no ignored error is reported by the AsyncResult being collected')
