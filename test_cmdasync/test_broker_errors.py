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
from commands_async.views import CeleryWorkerStatus
from test_cmdasync.harness import WORKERS_URL, create_user, launch, reset_workers_window, status_url

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
