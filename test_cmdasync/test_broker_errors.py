# coding=utf-8
"""As rotas workers e de revogação diante do erro e da lentidão do broker (#8879).

Execução (container `django`, com o `pyenv` do projeto):
    cd /opt/project/packages/django-cmd-async && /opt/project/pyenv/bin/python runtests.py test_cmdasync.test_broker_errors -v 2

O `celery_app` é trocado por um dublê, salvo em `BrokerUnreachableTest` (AC-8879-7), que usa um app de Celery REAL com a
configuração do projeto (`plus_base/settings/publique.py:814-818`) apontado para uma porta local recusada: nada vai ao broker
do dev. A janela da rota workers é estado do módulo `commands_async.views` (`workers_window`): cada teste a zera, e o relógio
dela (`views.clock`) é trocado por um relógio que o teste avança.
"""
import json
import signal
import threading
import time
from contextlib import contextmanager
from unittest import mock

from celery import Celery
from django.core.cache import caches
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import translation
from kombu.exceptions import OperationalError

from commands_async import settings as app_settings
from commands_async import views
from commands_async.views import CeleryWorkerStatus, TaskFormView
from test_cmdasync.harness import (AJAX, INDEX_URL, WORKERS_URL, create_user, launch, queued_task, reset_workers_window,
                                   status_url)

# O erro de teste leva um canário de host e porta, como o texto do kombu (medido: "Error 111 connecting to <host>:<porta>.").
CANARY = 'broker-canary-8879.internal:6379'
ERRORS = (('RuntimeError', RuntimeError), ('OSError', OSError), ('TimeoutError', TimeoutError),
          ('OperationalError', OperationalError))

# As frases que vão em `_()` (as dos critérios).
WORKERS_FAILED = 'could not ask the Celery workers'
NO_WORKER = 'no Celery worker answered'
REVOKE_FAILED = 'the revocation request could not be sent'

LOCMEM = 'django.core.cache.backends.locmem.LocMemCache'
TASK = 'task-8879'
WORKER = 'worker-8879@host'


def failure(exc_type):
    return exc_type('Error 111 connecting to {0}. Connection refused.'.format(CANARY))


class FakeClock(object):
    """O relógio da janela (`views.clock`): o teste o avança."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@override_settings(CACHES={'default': {'BACKEND': LOCMEM, 'LOCATION': 'broker-errors-8879'}})
class BrokerTestCase(TestCase):
    """Base: dois usuários autorizados, o `celery_app` trocado, a janela zerada e o cache do dono limpo entre os testes.

    O cache do registro do dono é o `default` (padrão do pacote, #8864): LocMem do processo, global, com ids repetidos entre
    os testes; sem a limpeza o registro de um teste mudaria o resultado do seguinte.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = create_user('owner')
        cls.other = create_user('other')

    def setUp(self):
        caches['default'].clear()
        self.addCleanup(caches['default'].clear)
        reset_workers_window()
        self.addCleanup(reset_workers_window)
        translation.activate('en')  # as frases dos critérios são os msgids em inglês, qualquer que seja o idioma do teste
        self.addCleanup(translation.deactivate)
        celery = mock.patch('commands_async.views.celery_app')
        self.celery_app = celery.start()
        self.addCleanup(celery.stop)
        self.inspect = self.celery_app.control.inspect
        self.revoke = self.celery_app.control.revoke
        self.inspect.return_value.active.return_value = {WORKER: []}

    @staticmethod
    def logged(user):
        client = Client()
        client.force_login(user)
        return client

    def ask_workers(self, user=None):
        """Consulta a rota workers como `user` (padrão: o dono), sem zerar a janela: o que o teste mede."""
        return (self.logged(user or self.owner)).get(WORKERS_URL)

    def launched_by_owner(self):
        client = self.logged(self.owner)
        launch(client, TASK)
        return client


class WorkersErrorTest(BrokerTestCase):
    """O erro da consulta aos workers: a frase fixa, o detalhe no log e o `None` dito claramente."""

    def test_an_error_is_answered_with_the_fixed_phrase_and_never_its_text(self):
        """AC-8879-1: a failing query is answered 200, status false, no worker name and the fixed phrase, never the text of the error."""
        self.assertTrue(json.loads(self.ask_workers().content)['status'], msg='precondition: a healthy inspection lists the workers')
        for label, exc_type in ERRORS:
            with self.subTest(error=label):
                reset_workers_window()
                self.inspect.side_effect = failure(exc_type)
                response = self.ask_workers()

                self.assertEqual(response.status_code, 200)
                data = json.loads(response.content)
                self.assertIs(data['status'], False)
                self.assertEqual(data['workers'], [])
                self.assertEqual(data['message'], WORKERS_FAILED)
                body = response.content.decode('utf-8')
                self.assertNotIn(CANARY, body, msg='%s: the address of the broker must not reach the user' % label)
                self.assertNotIn('Connection refused', body, msg='%s: the text of the error must not reach the user' % label)

    def test_a_failing_query_leaves_the_error_in_the_log(self):
        """AC-8879-3: a failing query to the workers is logged with the detail of the exception, and the answer does not carry it."""
        self.inspect.side_effect = failure(OperationalError)
        with self.assertLogs(views.logger, level='ERROR') as logs:
            response = self.ask_workers()

        self.assertIn(CANARY, '\n'.join(logs.output), msg='the detail of the exception must be in the log of the package')
        self.assertNotIn(CANARY, response.content.decode('utf-8'))

    def ask_workers_as_a_response(self):
        """Consulta a rota como o dono e devolve a resposta mesmo quando a view deixa o erro sair.

        O `Client` comum relevanta a exceção da view e o teste daria ERROR; sem relevantar, a view que não trata o erro é um
        500 que o assert de status pega como FAIL, o que a prova por mutação do reforço do #8879 mostra.
        """
        client = Client(raise_request_exception=False)
        client.force_login(self.owner)
        return client.get(WORKERS_URL)

    def test_an_error_that_comes_out_of_the_workers_answer_is_answered_with_the_fixed_phrase(self):
        """AC-8879-1: an error raised while the workers answer (``active()``, where the broker fails), not while the inspector is built, is answered 200 with the fixed phrase and never its text."""
        # No Celery real, `control.inspect()` só monta o `Inspect` (celery/app/control.py:80, `__init__` guarda os argumentos) e é
        # `.active()` (:136) que fala com o broker, por `_request` (:105) e `broadcast`; os testes acima injetam o erro no `inspect()`.
        self.assertTrue(json.loads(self.ask_workers().content)['status'], msg='precondition: a healthy inspection lists the workers')
        for label, exc_type in ERRORS:
            with self.subTest(error=label):
                reset_workers_window()
                self.inspect.return_value.active.side_effect = failure(exc_type)
                response = self.ask_workers_as_a_response()

                self.assertEqual(response.status_code, 200)
                data = json.loads(response.content)
                self.assertIs(data['status'], False)
                self.assertEqual(data['workers'], [])
                self.assertEqual(data['message'], WORKERS_FAILED)
                body = response.content.decode('utf-8')
                self.assertNotIn(CANARY, body, msg='%s: the address of the broker must not reach the user' % label)
                self.assertNotIn('Connection refused', body, msg='%s: the text of the error must not reach the user' % label)

    def test_an_error_that_comes_out_of_the_workers_answer_is_in_the_log(self):
        """AC-8879-3: an error raised while the workers answer (``active()``) is logged with the detail of the exception, and the answer does not carry it."""
        self.inspect.return_value.active.side_effect = failure(OperationalError)
        with self.assertLogs(views.logger, level='ERROR') as logs:
            response = self.ask_workers_as_a_response()

        self.assertEqual(json.loads(response.content)['message'], WORKERS_FAILED, msg='precondition: the failure is answered')
        self.assertIn(CANARY, '\n'.join(logs.output), msg='the detail of the exception must be in the log of the package')
        self.assertNotIn(CANARY, response.content.decode('utf-8'))

    def test_no_worker_answering_is_said_so_and_not_as_a_python_error(self):
        """AC-8879-4: when no worker answers, the message says that no worker answered."""
        self.inspect.return_value.active.return_value = None
        response = self.ask_workers()
        data = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data['message'], NO_WORKER)
        self.assertNotIn('NoneType', response.content.decode('utf-8'))
        self.assertNotEqual(NO_WORKER, WORKERS_FAILED, msg='precondition: the two phrases are different')


class RevokeErrorTest(BrokerTestCase):
    """O erro do envio da revogação: a frase fixa por padrão, o texto só com a setting ligada, e o detalhe no log."""

    def revoke_as_owner(self):
        client = self.launched_by_owner()
        return client.post(status_url(TASK))

    def test_the_detail_setting_is_off_by_default(self):
        """AC-8879-2 (controle): the detail of the revocation error is off when the host does not configure it."""
        from django.conf import settings as project_settings
        self.assertFalse(hasattr(project_settings, 'COMMANDS_ASYNC_REVOKE_ERROR_DETAIL'),
                         msg='precondition: the test project does not configure the setting')
        self.assertIs(app_settings.COMMANDS_ASYNC_REVOKE_ERROR_DETAIL, False)

    def test_a_failed_revocation_is_answered_with_the_fixed_phrase_and_never_its_text(self):
        """AC-8879-2: with the detail off (the default), a failed send is answered 200 with the fixed phrase, never the text of the error."""
        client = self.launched_by_owner()
        self.revoke.assert_not_called()
        client.post(status_url(TASK))
        self.revoke.assert_called_once_with(TASK, terminate=True)  # precondition: the owner's revoke reaches Celery
        self.revoke.reset_mock()
        for label, exc_type in ERRORS:
            with self.subTest(error=label):
                self.revoke.side_effect = failure(exc_type)
                response = client.post(status_url(TASK))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(response.content), {'status': False, 'error': REVOKE_FAILED})
                self.assertNotIn(CANARY, response.content.decode('utf-8'), msg='%s: the address must not reach the user' % label)

    def test_a_failed_revocation_leaves_the_error_in_the_log(self):
        """AC-8879-3: a failed send of the revocation is logged with the detail of the exception, and the answer does not carry it."""
        client = self.launched_by_owner()
        self.revoke.side_effect = failure(OperationalError)
        with self.assertLogs(views.logger, level='ERROR') as logs:
            response = client.post(status_url(TASK))

        self.assertIn(CANARY, '\n'.join(logs.output), msg='the detail of the exception must be in the log of the package')
        self.assertNotIn(CANARY, response.content.decode('utf-8'))

    def test_with_the_detail_on_a_failed_revocation_carries_the_text_of_the_error(self):
        """AC-8879-8 (controle): with the detail on, a failed send is answered 200 with the text of the error as the reason."""
        client = self.launched_by_owner()
        self.revoke.side_effect = failure(OperationalError)
        with mock.patch.object(app_settings, 'COMMANDS_ASYNC_REVOKE_ERROR_DETAIL', True):
            response = client.post(status_url(TASK))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content),
                         {'status': False, 'error': 'Error 111 connecting to {0}. Connection refused.'.format(CANARY)})


class Hung(BaseException):
    """O teste desistiu de esperar a rota: BaseException para não ser engolida por um `except Exception` do Celery ou do kombu."""


@contextmanager
def time_limit(seconds):
    def on_alarm(signum, frame):
        raise Hung()

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class BrokerUnreachableTest(BrokerTestCase):
    """O broker Redis que recusa a conexão: as duas rotas respondem em até 5 s, com o app REAL de Celery.

    O endereço que não responde (rede) não tem teste: a linha de evidência é `evidence/T-2.md` (plan.md, "Como provar").
    """

    LIMIT = 5.0       # o critério: "em até 5 segundos"
    GUARD = 12.0      # o teste não espera mais que isso: no red a rota esperaria minutos, e o teste tem de falhar, não travar

    def setUp(self):
        super().setUp()
        # A configuração real do projeto (plus_base/settings/publique.py:814-818): espera de 300 s e 100 tentativas, e as
        # `broker_transport_options` sem timeout de socket. `set_as_current=False`: este app não vira o `current_app`.
        app = Celery('broker-unreachable-8879', broker='redis://127.0.0.1:1/0', set_as_current=False)
        app.conf.update(broker_connection_timeout=300.0, broker_connection_max_retries=100, broker_connection_retry=True,
                        broker_transport_options={'priority_steps': [0, 3, 6, 9], 'health_check_interval': 30})
        self.addCleanup(app.close)
        real = mock.patch('commands_async.views.celery_app', app)
        real.start()
        self.addCleanup(real.stop)

    def answer_within_the_limit(self, label, call):
        started = time.monotonic()
        try:
            with time_limit(self.GUARD):
                response = call()
        except Hung:
            self.fail('%s: no answer after %.0f s with the broker refusing the connection (the limit is %.0f s)'
                      % (label, self.GUARD, self.LIMIT))
        elapsed = time.monotonic() - started
        self.assertLessEqual(elapsed, self.LIMIT, msg='%s: answered after %.1f s' % (label, elapsed))
        self.assertEqual(response.status_code, 200, msg=label)
        self.assertIs(json.loads(response.content)['status'], False, msg=label)

    def test_the_workers_route_answers_within_the_limit_when_the_connection_is_refused(self):
        """AC-8879-7: the workers route answers 200 with a false status within 5 seconds when the Redis broker refuses the connection."""
        self.answer_within_the_limit('workers', lambda: self.ask_workers())

    def test_the_revocation_answers_within_the_limit_when_the_connection_is_refused_and_the_owner_is_off(self):
        """AC-8879-7: the revocation answers 200 with a false status within 5 seconds when the Redis broker refuses the connection (owner off)."""
        client = self.logged(self.owner)
        with mock.patch.object(app_settings, 'COMMANDS_ASYNC_TASK_OWNERSHIP', False):
            self.answer_within_the_limit('revocation, owner off', lambda: client.post(status_url(TASK)))

    def test_the_revocation_answers_within_the_limit_when_the_connection_is_refused_and_the_owner_record_is_readable(self):
        """AC-8879-7: the same, with the owner check on and the record of the owner readable (the default configuration)."""
        client = self.launched_by_owner()
        self.answer_within_the_limit('revocation, owner record readable', lambda: client.post(status_url(TASK)))

    # O endereço do broker do app real de `setUp` (`redis://127.0.0.1:1/0`): o texto do erro REAL do kombu o traz
    # ("Error 111 connecting to 127.0.0.1:1. Connection refused."), e é por onde uma queda de verdade chega à view.
    BROKER_ADDRESS = '127.0.0.1'

    def answer_and_log_with_the_broker_down(self, label, call):
        """Chama a rota com o broker recusando a conexão e devolve a resposta e o texto do log `ERROR` do pacote."""
        try:
            with time_limit(self.GUARD), self.assertLogs(views.logger, level='ERROR') as logs:
                response = call()
        except Hung:
            self.fail('%s: no answer after %.0f s with the broker refusing the connection' % (label, self.GUARD))
        return response, '\n'.join(logs.output)

    def test_the_workers_route_answers_the_fixed_phrase_for_the_real_error_of_the_broker_check(self):
        """AC-8879-1, AC-8879-3: the real error of a dead broker (raised by the pre-check) is answered with the fixed phrase, never its text or the address of the broker, and the detail goes to the log."""
        response, log = self.answer_and_log_with_the_broker_down('workers', lambda: self.ask_workers())
        body = response.content.decode('utf-8')
        data = json.loads(body)

        self.assertEqual(response.status_code, 200)
        self.assertIs(data['status'], False)
        self.assertEqual(data['workers'], [])
        self.assertEqual(data['message'], WORKERS_FAILED, msg='precondition: the failure is answered with the fixed phrase')
        self.assertNotIn(self.BROKER_ADDRESS, body, msg='the address of the broker must not reach the user')
        self.assertNotIn('Connection refused', body, msg='the text of the error must not reach the user')
        self.assertIn(self.BROKER_ADDRESS, log, msg='the detail of the exception must be in the log of the package')

    def test_the_revocation_answers_the_fixed_phrase_for_the_real_error_of_the_broker_check(self):
        """AC-8879-2, AC-8879-3: the real error of a dead broker (raised by the pre-check) is answered with the fixed phrase, never its text or the address of the broker, and the detail goes to the log."""
        client = self.launched_by_owner()
        response, log = self.answer_and_log_with_the_broker_down('revocation', lambda: client.post(status_url(TASK)))
        body = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(body), {'status': False, 'error': REVOKE_FAILED},
                         msg='precondition: the failure is answered with the fixed phrase')
        self.assertNotIn(self.BROKER_ADDRESS, body, msg='the address of the broker must not reach the user')
        self.assertNotIn('Connection refused', body, msg='the text of the error must not reach the user')
        self.assertIn(self.BROKER_ADDRESS, log, msg='the detail of the exception must be in the log of the package')


class WorkersWindowTest(BrokerTestCase):
    """A janela de 5 s por processo da rota workers: a consulta que o processo já respondeu vale por 5 s."""

    def setUp(self):
        super().setUp()
        self.clock = FakeClock(1000.0)
        clock = mock.patch.object(views, 'clock', self.clock)
        clock.start()
        self.addCleanup(clock.stop)

    def first_answer(self, outcome):
        """A primeira consulta, de um dos três resultados que a janela guarda: devolve a resposta e o corpo."""
        if outcome == 'list':
            self.inspect.return_value.active.return_value = {WORKER: []}
        elif outcome == 'nobody':
            self.inspect.return_value.active.return_value = None
        else:
            self.inspect.side_effect = failure(OperationalError)
        response = self.ask_workers(self.owner)
        self.assertEqual(response.status_code, 200, msg='precondition: the first query is answered')
        self.assertEqual(self.inspect.call_count, 1, msg='precondition: the first query reaches the workers')
        return response

    def change_the_workers(self):
        """Muda o que os workers responderiam: uma consulta que chegue a eles daria outro resultado."""
        self.inspect.side_effect = None
        self.inspect.return_value.active.return_value = {'other-worker@host': []}

    def test_a_query_inside_the_window_is_answered_with_the_previous_result_for_each_outcome(self):
        """AC-8879-5, AC-8879-11: under 5 seconds after an answered query (of another user), the previous result is served and the workers are not asked again."""
        for outcome in ('list', 'nobody', 'failure'):
            with self.subTest(outcome=outcome):
                reset_workers_window()
                self.inspect.reset_mock()
                self.clock.now = 1000.0
                first = self.first_answer(outcome)
                self.change_the_workers()

                self.clock.now = 1004.9
                second = self.ask_workers(self.other)

                self.assertEqual(self.inspect.call_count, 1, msg='%s: the workers must not be asked again inside the window' % outcome)
                self.assertEqual(second.status_code, 200)
                self.assertEqual(json.loads(second.content), json.loads(first.content),
                                 msg='%s: the answer is the one of the previous query' % outcome)
                if outcome == 'list':
                    self.assertEqual(json.loads(second.content)['workers'], [WORKER], msg='the names of the previous query')

    def test_a_query_five_seconds_after_the_previous_one_asks_the_workers_again(self):
        """AC-8879-6 (controle): 5 seconds or more after the previous answered query, the workers are asked again and the new result is served."""
        for outcome in ('list', 'nobody', 'failure'):
            with self.subTest(outcome=outcome):
                reset_workers_window()
                self.inspect.reset_mock()
                self.clock.now = 1000.0
                self.first_answer(outcome)
                self.change_the_workers()

                self.clock.now = 1005.0
                second = self.ask_workers(self.other)

                self.assertEqual(self.inspect.call_count, 2, msg='%s: the workers must be asked again at 5 seconds' % outcome)
                data = json.loads(second.content)
                self.assertIs(data['status'], True)
                self.assertEqual(data['workers'], ['other-worker@host'], msg='%s: the new result is served' % outcome)

    def test_simultaneous_queries_ask_the_workers_once_and_get_the_same_result(self):
        """AC-8879-10: queries that reach the process at the same time, with no answered query in the window, ask the workers once and all get the same result."""
        count = 6
        answers = []
        errors = []
        barrier = threading.Barrier(count)

        def slow_active():
            time.sleep(0.4)  # o broadcast demora: as outras requisições chegam enquanto ele roda
            return {WORKER: []}

        self.inspect.return_value.active.side_effect = slow_active

        def ask():
            try:
                request = RequestFactory().get(WORKERS_URL)
                request.user = self.owner
                barrier.wait(timeout=5)
                response = CeleryWorkerStatus.as_view()(request)
                answers.append((response.status_code, response.content))
            except BaseException as exc:  # o fio não pode morrer calado: o teste confere a lista de erros
                errors.append(exc)

        threads = [threading.Thread(target=ask) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual(errors, [], msg='no thread may fail')
        self.assertEqual(len(answers), count, msg='precondition: every query was answered')
        self.assertEqual({code for code, _ in answers}, {200})
        self.assertEqual(len({body for _, body in answers}), 1, msg='every query gets the same result')
        self.assertEqual(json.loads(answers[0][1])['workers'], [WORKER])
        self.assertEqual(self.inspect.call_count, 1, msg='the workers must be asked once for %d simultaneous queries' % count)


# O formulário de execução (#8944). O comando é o `cmdasync_echo` do app de teste; o endereço do app real é uma porta local recusada.
ECHO = 'cmdasync_echo'
COMMAND = {'app_command': 'test_cmdasync.cmdasync_echo', 'args': '', 'kwargs': ''}
FORM_TASK = 'form-task-8944'
FORM_FAILED = 'command cmdasync_echo failed to execute'
FORM_FAILED_PT_BR = 'falha ao executar o comando cmdasync_echo'
NOT_ALLOWED = 'not allowed to run this command'
REFUSED_BROKER = 'redis://127.0.0.1:1/0'
REFUSED_ADDRESS = '127.0.0.1'


class FormBrokerTestCase(BrokerTestCase):
    """O formulário de execução diante do broker (#8944): o `celery_app` é um app REAL de Celery (ou um dublê, nos testes de
    recusa), o `command_exec` é trocado por um dublê que devolve a tarefa e a `precheck_before_launch` é a função REAL (quem a
    troca são o `launch` do harness e os testes de `test_views` e `test_task_owner`, que não medem o broker)."""

    LIMIT = 5.0       # o critério: «em até 5 segundos»
    GUARD = 12.0      # o teste não espera mais que isso: sem a pré-verificação a resposta não vem, e o teste tem de falhar, não travar

    def use_app(self, broker, eager=False):
        """Troca o `celery_app` do pacote por um app real com a configuração do projeto (`plus_base/settings/publique.py:814-818`:
        espera de 300 s, 100 tentativas e as `broker_transport_options` sem timeout de socket)."""
        app = Celery('form-broker-8944', broker=broker, set_as_current=False)
        app.conf.update(broker_connection_timeout=300.0, broker_connection_max_retries=100, broker_connection_retry=True,
                        broker_transport_options={'priority_steps': [0, 3, 6, 9], 'health_check_interval': 30},
                        task_always_eager=eager)
        self.addCleanup(app.close)
        real = mock.patch('commands_async.views.celery_app', app)
        real.start()
        self.addCleanup(real.stop)
        return app

    def submit(self, label):
        """Submete o formulário por XHR, como o dono, com o `command_exec` trocado; devolve a resposta, o tempo e o dublê."""
        client = self.logged(self.owner)
        with mock.patch('commands_async.views.command_exec') as task:
            task.apply_async.return_value = queued_task(FORM_TASK)
            started = time.monotonic()
            try:
                with time_limit(self.GUARD):
                    response = client.post(INDEX_URL, COMMAND, **AJAX)
            except Hung:
                self.fail('%s: no answer after %.0f s with the broker down (the limit is %.0f s)'
                          % (label, self.GUARD, self.LIMIT))
        return response, time.monotonic() - started, task


class FormBrokerDownTest(FormBrokerTestCase):
    """O broker que recusa a conexão: o formulário responde a falha em até 5 s, sem disparar a tarefa."""

    def test_a_refused_connection_is_answered_400_within_the_limit_and_nothing_is_dispatched(self):
        """AC-8944-1: with the Redis broker refusing the connection, the form answers 400 with the error on the command field, dispatches nothing, within 5 seconds."""
        self.use_app(REFUSED_BROKER)
        response, elapsed, task = self.submit('refused connection')

        self.assertEqual(response.status_code, 400)
        self.assertIn('app_command', json.loads(response.content)['form']['errors'])
        task.apply_async.assert_not_called()
        self.assertLessEqual(elapsed, self.LIMIT, msg='answered after %.1f s' % elapsed)

    def test_a_broker_that_accepts_the_connection_still_gets_the_task(self):
        """AC-8944-1 (controle): the real pre-check passes against a broker that accepts the connection, and the form answers the task."""
        self.use_app('memory://')
        response, elapsed, task = self.submit('accepting broker')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)['task']['id'], FORM_TASK)
        task.apply_async.assert_called_once()

    def test_the_failure_answer_is_the_fixed_phrase_without_the_task_the_error_or_the_address(self):
        """AC-8944-2: the broker failure is answered with only the fixed phrase (in English, and translated when there is a translation), with no task id, no error text and no broker address."""
        self.use_app(REFUSED_BROKER)
        for label, language, phrase in (('english', 'en', FORM_FAILED), ('pt_BR', 'pt-br', FORM_FAILED_PT_BR)):
            with self.subTest(language=label):
                translation.activate(language)
                response, _, _ = self.submit(label)
                body = response.content.decode('utf-8')

                self.assertEqual(response.status_code, 400, msg='precondition: the broker failure is answered 400')
                self.assertEqual(json.loads(body), {'form': {'errors': {'app_command': [phrase]}}})
                self.assertNotIn('"task"', body, msg='the answer of a failure must not carry a task id')
                self.assertNotIn(REFUSED_ADDRESS, body, msg='the address of the broker must not reach the user')
                self.assertNotIn('Connection refused', body, msg='the text of the error must not reach the user')

    def test_the_failure_leaves_the_error_in_the_log_with_the_detail_of_the_exception(self):
        """AC-8944-7: the broker failure of the form is logged by the package with the detail of the exception."""
        self.use_app(REFUSED_BROKER)
        with self.assertLogs(views.logger, level='ERROR') as logs:
            response, _, _ = self.submit('log')
        text = '\n'.join(logs.output)

        self.assertEqual(response.status_code, 400, msg='precondition: the broker failure is answered 400')
        self.assertIn('failed to execute ' + ECHO, text)
        self.assertIn(REFUSED_ADDRESS, text, msg='the detail of the exception must be in the log of the package')
        self.assertIn('OperationalError', text)

    def test_the_synchronous_mode_dispatches_without_opening_a_connection_to_the_broker(self):
        """AC-8944-3 (controle): with Celery in synchronous mode the form dispatches the command although the broker is down."""
        self.use_app(REFUSED_BROKER, eager=True)
        response, elapsed, task = self.submit('synchronous mode')

        self.assertEqual(response.status_code, 200,
                         msg='the broker must not be checked in synchronous mode (the dead broker would answer 400)')
        self.assertEqual(json.loads(response.content)['task']['id'], FORM_TASK)
        task.apply_async.assert_called_once()
        self.assertLessEqual(elapsed, self.LIMIT, msg='answered after %.1f s' % elapsed)


class FormRefusalTest(FormBrokerTestCase):
    """As recusas do formulário (comando ignorado e usuário sem a permissão) vêm antes de qualquer conexão com o broker."""

    def setUp(self):
        super().setUp()
        # O dublê do app fica FORA do modo síncrono: a função real lê a flag e chamaria a pré-verificação; com o `MagicMock`
        # «verdadeiro» ela a dispensaria e o teste ficaria cego à ordem (a pré-verificação antes da recusa passaria).
        self.celery_app.conf.task_always_eager = False
        precheck = mock.patch('commands_async.views.broker_precheck')
        self.precheck = precheck.start()
        self.addCleanup(precheck.stop)

    @staticmethod
    def refusals():
        return (('a command on the ignore list', lambda: mock.patch.object(TaskFormView, 'commands_skip', [ECHO])),
                ('a user without the permission',
                 lambda: mock.patch.object(TaskFormView, 'command_permission_name', 'auth.run_commands')))

    def test_a_refusal_opens_no_connection_to_the_broker(self):
        """AC-8944-4: a command on the ignore list, or a user without the named permission, is refused without opening a connection to the broker, by XHR and without XHR."""
        client = self.logged(self.owner)
        with mock.patch('commands_async.views.command_exec') as task:
            task.apply_async.return_value = queued_task(FORM_TASK)
            allowed = client.post(INDEX_URL, COMMAND, **AJAX)
        self.assertEqual(allowed.status_code, 200, msg='precondition: a permitted submit is dispatched')
        self.precheck.assert_called_once_with()   # precondição: com o modo síncrono desligado, a pré-verificação do disparo roda
        self.precheck.reset_mock()
        self.celery_app.reset_mock()

        for label, patcher in self.refusals():
            for by_xhr in (True, False):
                with self.subTest(refusal=label, xhr=by_xhr):
                    with patcher(), mock.patch('commands_async.views.command_exec') as task:
                        response = client.post(INDEX_URL, COMMAND, **(AJAX if by_xhr else {}))

                    self.precheck.assert_not_called()
                    self.celery_app.connection_for_write.assert_not_called()
                    # nenhuma outra API do app (`connection_for_read`, `control`…) pode ser usada para abrir conexão
                    self.assertEqual(self.celery_app.mock_calls, [], msg='a refusal must not touch the Celery app')
                    task.apply_async.assert_not_called()
                    if by_xhr:
                        self.assertEqual(response.status_code, 400)
                        self.assertEqual(json.loads(response.content)['form']['errors']['app_command'], [NOT_ALLOWED])
                    else:
                        self.assertEqual(response.status_code, 403)
