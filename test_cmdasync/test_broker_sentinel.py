# coding=utf-8
"""A pré-verificação do broker diante de um broker SENTINEL (#8973): as rotas de workers e de revogação e o formulário de execução.

Execução (container `django`, com o `pyenv` do projeto):
    cd /opt/project/packages/django-cmd-async && /opt/project/pyenv/bin/python runtests.py test_cmdasync.test_broker_sentinel -v 2

O `celery_app` é um app REAL de Celery com a configuração do projeto (`plus_base/settings/publique.py:814-836` e
`plus/utils/redis.py:438-443`: a espera de 300 s, as 100 tentativas e as `broker_transport_options` com `sentinels` e
`master_name`), apontado para sentinelas LOCAIS: um `SilentSentinel` (aceita a conexão, grava o que recebe e nunca responde) ou a
porta recusada. Nada vai ao Redis do dev nem à rede.

Os critérios `manual` do mesmo ticket (AC-8973-6 a -9: o sentinela sem rota, o master inalcançável, o Sentinel de pé) não têm
teste aqui: pedem um Sentinel real, e a evidência deles é a medição da T-4 contra a pilha do compose (`evidence/`).
"""
import json
import time
from unittest import mock

from celery import Celery
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError

from commands_async import settings as app_settings
from commands_async import views
from test_cmdasync.harness import AJAX, INDEX_URL, SilentSentinel, reset_workers_window, status_url
from test_cmdasync.test_broker_errors import (COMMAND, FORM_FAILED, TASK, WORKERS_FAILED, BrokerTestCase, FormBrokerTestCase,
                                              Hung, time_limit)

LIMIT = 5.0       # o critério: «em até 5 segundos»
GUARD = 12.0      # o teste não espera mais que isso: hoje a conferência leva de 15 s a 179 s, e o teste tem de falhar, não travar

REFUSED_SENTINEL = ('127.0.0.1', 1)
PASSWORD = 'sekret-8973'
USERNAME = 'ana'


def host_credentials():
    """O `sentinel_kwargs` que um host com Sentinel autenticado define: usuário, senha e nova tentativa do redis-py."""
    return {'sentinel_kwargs': {'username': USERNAME, 'password': PASSWORD,
                                'retry_on_error': [RedisTimeoutError, RedisConnectionError]}}


def sentinel_app(addresses, host_options=None, name='sentinel-8973'):
    """Um app real de Celery com a configuração do projeto e o broker Sentinel nos `addresses`.

    Um endereço: a URL `sentinel://<sentinela>/0` que o `configure_celery` do projeto monta; vários: a forma `;` do kombu (o
    projeto monta só o 1º sentinela, e o kombu ignora a opção `sentinels`: `kombu/transport/redis.py:2342-2351`).
    `host_options` entra nas `broker_transport_options`, como o que um host define (`sentinel_kwargs`).
    """
    url = ';'.join('sentinel://%s:%d/0' % address for address in addresses)
    options = {'priority_steps': [0, 3, 6, 9], 'health_check_interval': 30, 'master_name': 'mymaster',
               'sentinels': list(addresses)}
    options.update(host_options or {})
    app = Celery(name, broker=url, set_as_current=False)
    app.conf.update(broker_connection_timeout=300.0, broker_connection_max_retries=100, broker_connection_retry=True,
                    broker_transport_options=options, task_always_eager=False)
    return app


class SentinelAppMixin(object):
    """Os sentinelas locais e a troca do `celery_app` do pacote por um app real."""

    def silent_sentinels(self, count=1):
        sentinels = [SilentSentinel() for _ in range(count)]
        for sentinel in sentinels:
            self.addCleanup(sentinel.close)
        return sentinels

    def use_broker(self, app):
        self.addCleanup(app.close)
        real = mock.patch('commands_async.views.celery_app', app)
        real.start()
        self.addCleanup(real.stop)
        return app

    def use_sentinels(self, addresses, host_options=None):
        return self.use_broker(sentinel_app(addresses, host_options))

    def state(self, kind, count, host):
        """Sobe o estado do broker e devolve os sentinelas (para o teste ler o que receberam) e a configuração do host."""
        host_options = {'credentials': host_credentials(), 'none': {'sentinel_kwargs': None}, None: None}[host]
        if kind == 'refused':
            sentinels = []
            addresses = [REFUSED_SENTINEL]
        else:
            sentinels = self.silent_sentinels(count)
            addresses = [sentinel.address for sentinel in sentinels]
        self.use_sentinels(addresses, host_options)
        return sentinels


# (rótulo, como os sentinelas respondem, quantos na URL, o que o host define em `sentinel_kwargs`)
SILENT_ONE = ('one sentinel that accepts the connection and does not answer', 'silent', 1, None)
SILENT_THREE = ('three sentinels that accept the connection and do not answer', 'silent', 3, None)
SILENT_THREE_WITH_CREDENTIALS = ('three sentinels that do not answer, the host defining credentials and retry_on_error',
                                 'silent', 3, 'credentials')
REFUSED_ONE = ('one sentinel that refuses the connection', 'refused', 1, None)
REFUSED_ONE_WITH_NONE = ('one sentinel that refuses the connection, the host defining sentinel_kwargs as None',
                         'refused', 1, 'none')


class SentinelRoutesUnreachableTest(SentinelAppMixin, BrokerTestCase):
    """AC-8973-1: com o broker Sentinel inalcançável as duas rotas respondem 200 com `status` falso em até 5 s."""

    def answer_within_the_limit(self, label, call):
        started = time.monotonic()
        try:
            with time_limit(GUARD):
                response = call()
        except Hung:
            self.fail('%s: no answer after %.0f s with the Sentinel broker unreachable (the limit is %.0f s)'
                      % (label, GUARD, LIMIT))
        elapsed = time.monotonic() - started
        self.assertLessEqual(elapsed, LIMIT, msg='%s: answered after %.1f s' % (label, elapsed))
        self.assertEqual(response.status_code, 200, msg=label)
        self.assertIs(json.loads(response.content)['status'], False, msg=label)

    def test_the_workers_route_answers_within_the_limit_when_no_sentinel_answers(self):
        """AC-8973-1: the workers route answers 200 with a false status within 5 seconds when the Sentinel broker is unreachable (one or three sentinels, with or without sentinel_kwargs in the host)."""
        for label, kind, count, host in (SILENT_ONE, SILENT_THREE, SILENT_THREE_WITH_CREDENTIALS, REFUSED_ONE,
                                         REFUSED_ONE_WITH_NONE):
            with self.subTest(state=label):
                reset_workers_window()
                self.state(kind, count, host)
                self.answer_within_the_limit(label, lambda: self.ask_workers())

    def test_the_revocation_answers_within_the_limit_when_no_sentinel_answers_and_the_owner_record_is_readable(self):
        """AC-8973-1: the revocation answers 200 with a false status within 5 seconds when the Sentinel broker is unreachable (owner record readable)."""
        client = self.launched_by_owner()
        label, kind, count, host = SILENT_ONE
        self.state(kind, count, host)
        self.answer_within_the_limit(label, lambda: client.post(status_url(TASK)))

    def test_the_revocation_answers_within_the_limit_when_no_sentinel_answers_and_the_owner_is_off(self):
        """AC-8973-1: the revocation answers 200 with a false status within 5 seconds when the Sentinel broker is unreachable (owner off)."""
        client = self.logged(self.owner)
        label, kind, count, host = SILENT_ONE
        self.state(kind, count, host)
        with mock.patch.object(app_settings, 'COMMANDS_ASYNC_TASK_OWNERSHIP', False):
            self.answer_within_the_limit(label, lambda: client.post(status_url(TASK)))


class SentinelFormUnreachableTest(SentinelAppMixin, FormBrokerTestCase):
    """AC-8973-4: com o broker Sentinel inalcançável o formulário responde 400 em até 5 s, sem disparar a tarefa."""

    def test_the_form_answers_400_within_the_limit_when_no_sentinel_answers_and_nothing_is_dispatched(self):
        """AC-8973-4: the form answers 400 with the error on the command field, dispatches nothing, within 5 seconds when the Sentinel broker is unreachable (one or three sentinels, with or without sentinel_kwargs in the host)."""
        for label, kind, count, host in (SILENT_ONE, SILENT_THREE_WITH_CREDENTIALS, REFUSED_ONE):
            with self.subTest(state=label):
                self.state(kind, count, host)
                response, elapsed, task = self.submit(label)

                self.assertEqual(response.status_code, 400, msg=label)
                self.assertIn('app_command', json.loads(response.content)['form']['errors'], msg=label)
                task.apply_async.assert_not_called()
                self.assertLessEqual(elapsed, self.LIMIT, msg='%s: answered after %.1f s' % (label, elapsed))

    def test_the_failure_answer_and_the_log_of_the_form_are_the_ones_of_any_broker_failure(self):
        """Robustez, sem critério: a falha do formulário com Sentinel responde a frase fixa, sem o texto do erro nem o endereço, e o detalhe vai ao log do pacote (o `except` do formulário não distingue o broker: `views.py:300-308`)."""
        self.state('refused', 1, None)
        with self.assertLogs(views.logger, level='ERROR') as logs:
            response, _, _ = self.submit('refused sentinel')
        body = response.content.decode('utf-8')
        log = '\n'.join(logs.output)

        self.assertEqual(response.status_code, 400, msg='precondition: the broker failure is answered 400')
        self.assertEqual(json.loads(body), {'form': {'errors': {'app_command': [FORM_FAILED]}}})
        self.assertNotIn('127.0.0.1', body, msg='the address of the sentinel must not reach the user')
        self.assertIn('No master found', log, msg='the detail of the exception must be in the log of the package')


class SentinelCredentialsTest(SentinelAppMixin, BrokerTestCase):
    """AC-8973-2: a pré-verificação apresenta ao sentinela as credenciais que o host define em `sentinel_kwargs`."""

    REACH = 8.0   # o que o sentinela recebe chega no 1º salto; o prazo é do AC-8973-1 e -4, aqui só importa o que chegou

    def reach(self, call):
        """Chama a entrada e devolve a resposta, ou `None` se a conferência ainda esperava depois de `REACH` segundos."""
        try:
            with time_limit(self.REACH):
                return call()
        except Hung:
            return None

    def assert_credentials_reached(self, sentinels, username=USERNAME, password=PASSWORD):
        """O sentinela local recebeu o `AUTH` com o que o host definiu (o que o host não definiu não se confere: o redis-py 8 completa o
        que falta com `default` ou com a senha vazia, e uma versão anterior manda só `AUTH <senha>`)."""
        received = b''.join(sentinel.received for sentinel in sentinels)
        self.assertTrue(any(sentinel.connections for sentinel in sentinels),
                        msg='precondition: the pre-check reached a sentinel')
        self.assertIn(b'AUTH', received, msg='the credentials must be presented to the sentinel')
        if password:
            self.assertIn(password.encode(), received, msg='the password of the host must reach the sentinel')
        if username:
            self.assertIn(username.encode(), received, msg='the username of the host must reach the sentinel')

    def test_the_workers_route_presents_the_credentials_of_the_host_to_the_sentinel(self):
        """AC-8973-2 (controle): the workers route presents the user and the password that the host defines in sentinel_kwargs to the sentinel."""
        sentinels = self.state('silent', 3, 'credentials')
        self.reach(lambda: self.ask_workers())
        self.assert_credentials_reached(sentinels)

    def test_the_revocation_presents_the_credentials_of_the_host_to_the_sentinel(self):
        """AC-8973-2 (controle): the revocation presents the user and the password that the host defines in sentinel_kwargs to the sentinel."""
        client = self.launched_by_owner()
        sentinels = self.state('silent', 3, 'credentials')
        self.reach(lambda: client.post(status_url(TASK)))
        self.assert_credentials_reached(sentinels)

    def test_the_form_presents_the_credentials_of_the_host_to_the_sentinel(self):
        """AC-8973-2 (controle): the execution form presents the user and the password that the host defines in sentinel_kwargs to the sentinel."""
        client = self.logged(self.owner)
        sentinels = self.state('silent', 3, 'credentials')
        with mock.patch('commands_async.views.command_exec'):
            self.reach(lambda: client.post(INDEX_URL, COMMAND, **AJAX))
        self.assert_credentials_reached(sentinels)

    def test_a_host_that_defines_only_a_password_has_it_presented_to_the_sentinel(self):
        """AC-8973-2 (controle): a host that defines only a password in sentinel_kwargs has it presented to the sentinel."""
        sentinels = self.silent_sentinels(1)
        self.use_sentinels([sentinels[0].address], {'sentinel_kwargs': {'password': PASSWORD}})
        self.reach(lambda: self.ask_workers())
        self.assert_credentials_reached(sentinels, username=None)

    def test_a_host_that_defines_only_a_username_has_it_presented_to_the_sentinel(self):
        """AC-8973-2 (controle): a host that defines only a username in sentinel_kwargs has it presented to the sentinel (a mescla não pode descartá-lo por faltar a senha)."""
        sentinels = self.silent_sentinels(1)
        self.use_sentinels([sentinels[0].address], {'sentinel_kwargs': {'username': USERNAME}})
        self.reach(lambda: self.ask_workers())
        self.assert_credentials_reached(sentinels, password=None)


class SentinelPasswordTest(SentinelAppMixin, BrokerTestCase):
    """AC-8973-3: a falha da conferência não põe a senha do `sentinel_kwargs` na resposta nem no log."""

    def setUp(self):
        super().setUp()
        # a porta recusada falha igual no código de hoje e no novo: o que se confere é o texto da falha, não o prazo
        self.state('refused', 1, 'credentials')

    def test_the_password_stays_out_of_the_answer_and_the_log_of_the_workers_route(self):
        """AC-8973-3 (controle): a failed check of the Sentinel broker leaves the password of the host out of the answer of the workers route and out of the log."""
        with self.assertLogs(views.logger, level='ERROR') as logs:
            response = self.ask_workers()
        body = response.content.decode('utf-8')
        log = '\n'.join(logs.output)

        self.assertEqual(json.loads(body)['message'], WORKERS_FAILED, msg='precondition: the check failed')
        self.assertIn('No master found', log, msg='precondition: the exception of the sentinel client is in the log')
        self.assertNotIn(PASSWORD, body)
        self.assertNotIn(PASSWORD, log)

    def test_the_password_stays_out_of_the_answer_and_the_log_of_the_revocation_with_the_detail_on(self):
        """AC-8973-3 (controle): with the detail of the revocation error on (the only answer that carries the text of the error), the password of the host stays out of the answer and out of the log."""
        client = self.launched_by_owner()
        with mock.patch.object(app_settings, 'COMMANDS_ASYNC_REVOKE_ERROR_DETAIL', True), \
                self.assertLogs(views.logger, level='ERROR') as logs:
            response = client.post(status_url(TASK))
        body = response.content.decode('utf-8')
        log = '\n'.join(logs.output)
        data = json.loads(body)

        self.assertIs(data['status'], False, msg='precondition: the check failed')
        self.assertIn('No master found', data['error'], msg='precondition: the text of the error is in the answer')
        self.assertNotIn(PASSWORD, body)
        self.assertNotIn(PASSWORD, log)

    def test_the_password_stays_out_of_the_answer_and_the_log_of_the_form(self):
        """AC-8973-3 (controle): a failed check of the Sentinel broker leaves the password of the host out of the answer of the form and out of the log."""
        client = self.logged(self.owner)
        with mock.patch('commands_async.views.command_exec'), self.assertLogs(views.logger, level='ERROR') as logs:
            response = client.post(INDEX_URL, COMMAND, **AJAX)
        body = response.content.decode('utf-8')
        log = '\n'.join(logs.output)

        self.assertEqual(response.status_code, 400, msg='precondition: the check failed')
        self.assertIn('No master found', log, msg='precondition: the exception of the sentinel client is in the log')
        self.assertNotIn(PASSWORD, body)
        self.assertNotIn(PASSWORD, log)


class SingleHostSilentBrokerTest(SentinelAppMixin, BrokerTestCase):
    """AC-8973-5: o `redis://` de um host só que aceita a conexão e não responde (o «não responder» do critério)."""

    def test_the_workers_route_answers_within_the_limit_when_the_single_host_accepts_and_does_not_answer(self):
        """AC-8973-5 (controle): the workers route answers 200 with a false status within 5 seconds when the Redis broker of a single host accepts the connection and does not answer."""
        silent = self.silent_sentinels(1)[0]
        app = Celery('single-silent-8973', broker='redis://%s:%d/0' % silent.address, set_as_current=False)
        app.conf.update(broker_connection_timeout=300.0, broker_connection_max_retries=100, broker_connection_retry=True,
                        broker_transport_options={'priority_steps': [0, 3, 6, 9], 'health_check_interval': 30},
                        task_always_eager=False)
        self.use_broker(app)

        started = time.monotonic()
        try:
            with time_limit(GUARD):
                response = self.ask_workers()
        except Hung:
            self.fail('no answer after %.0f s with the single-host broker accepting and not answering' % GUARD)
        elapsed = time.monotonic() - started

        self.assertTrue(silent.connections, msg='precondition: the pre-check reached the address')
        self.assertEqual(response.status_code, 200)
        self.assertIs(json.loads(response.content)['status'], False)
        self.assertLessEqual(elapsed, LIMIT, msg='answered after %.1f s' % elapsed)


class PreCheckRequestTest(SentinelAppMixin, BrokerTestCase):
    """Robustez, sem critério: o pedido de conexão da conferência leva os limites do salto do sentinela."""

    def test_the_connection_request_carries_the_limits_of_the_sentinel_hop_and_the_credentials_of_the_host(self):
        """Robustez, sem critério: the request of the pre-check carries the socket limits and no new attempt for the sentinel hop, over the credentials of the host and without its retry keys (o efeito do `socket_connect_timeout` só aparece num endereço sem rota, que um teste não cria: a medição da T-4 do sentinela sem rota; sem este teste o mutante que o tira passa)."""
        host = host_credentials()
        host['sentinel_kwargs']['retry'] = object()
        host['sentinel_kwargs']['retry_on_timeout'] = True
        app = self.use_broker(sentinel_app([REFUSED_SENTINEL], host))
        # o pedido ao `connection_for_write` é o que o teste lê (celery/app/base.py:1058-1060: a mescla das opções é do Celery)
        with mock.patch.object(app, 'connection_for_write') as request:
            views.broker_precheck()
        options = request.call_args.kwargs['transport_options']
        sentinel_kwargs = options['sentinel_kwargs']

        self.assertEqual(options['socket_connect_timeout'], views.BROKER_PRECHECK_SOCKET_TIMEOUT, msg='precondition: the master hop is limited')
        self.assertEqual(sentinel_kwargs['socket_connect_timeout'], views.BROKER_PRECHECK_SOCKET_TIMEOUT)
        self.assertEqual(sentinel_kwargs['socket_timeout'], views.BROKER_PRECHECK_SOCKET_TIMEOUT)
        self.assertIsNone(sentinel_kwargs['retry'], msg='the pre-check must not retry a call to the sentinel')
        self.assertNotIn('retry_on_timeout', sentinel_kwargs)
        self.assertNotIn('retry_on_error', sentinel_kwargs)
        self.assertEqual((sentinel_kwargs['username'], sentinel_kwargs['password']), (USERNAME, PASSWORD),
                         msg='the credentials of the host stay in the request')


class HostSentinelKwargsOutsideSentinelTest(SentinelAppMixin, BrokerTestCase):
    """Robustez, sem critério: o `sentinel_kwargs` do host (ausente, `None` ou com credenciais) não derruba a conferência de um broker alcançável."""

    def test_a_reachable_broker_passes_the_check_whatever_the_host_defines_in_sentinel_kwargs(self):
        """Robustez, sem critério: the pre-check passes against a reachable broker when the host defines sentinel_kwargs as None, with credentials, or does not define it (the option is read only by the Sentinel transport: kombu/transport/redis.py:2301-2304)."""
        for label, host in (('absent', {}), ('None', {'sentinel_kwargs': None}), ('credentials', host_credentials())):
            with self.subTest(sentinel_kwargs=label):
                options = {'priority_steps': [0, 3, 6, 9], 'health_check_interval': 30}
                options.update(host)
                app = Celery('memory-8973', broker='memory://', set_as_current=False)
                app.conf.update(broker_transport_options=options, task_always_eager=False)
                self.use_broker(app)

                views.broker_precheck()   # não levanta: o broker de memória aceita a conexão
