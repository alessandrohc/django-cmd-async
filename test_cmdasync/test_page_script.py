# coding=utf-8
"""A tela de comandos em node: os scripts REAIS (`cmdhandler.js` e `cmdasync-page.js`) rodam com um dublê de jQuery, de DOM e de
relógio (#8866).

Execução (container `django`, com o `pyenv` do projeto; precisa do `node`, que vem no container):
    cd /opt/project/packages/django-cmd-async && /opt/project/pyenv/bin/python runtests.py test_cmdasync.test_page_script -v 2

O harness de node (`js_harness/world.js` e `js_harness/scenarios.js`) roda UMA vez por processo e devolve, em JSON, o que a tela
mostra em cada cenário (resposta do servidor, clique do usuário, relógio); os métodos abaixo afirmam sobre esse JSON, um por
critério, com o ID na 1ª linha da docstring. Os controles (comportamento que já vale hoje e tem de continuar valendo) levam
"(controle)" depois do ID.

Sem `node` o teste FALHA, não pula: um pulo daria verde sem provar nada (o mesmo risco do `.mo` velho sem o `msgfmt`,
`test_locale.py`). O dublê repete as regras do jQuery 3 medidas no Chrome (ver o cabeçalho do `world.js`); os testes da última
classe conferem, contra o código real, as suposições que ele fixa.
"""
import functools
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from test_cmdasync.harness import INDEX_URL, create_user

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = Path(__file__).resolve().parent / 'js_harness' / 'scenarios.js'
CMDHANDLER = ROOT / 'commands_async' / 'static' / 'cmdasync' / 'js' / 'cmdhandler.js'

REVOKE_KINDS = ('http403', 'http404', 'http500', 'html200', 'falseWithError', 'falseNoError')
REVOKE_PHASES = ('tracking', 'window', 'late')


@functools.lru_cache(maxsize=1)
def load_scenarios():
    node = shutil.which('node')
    if node is None:
        raise AssertionError('node is not installed: the page script tests need it, and they do not skip without it')
    completed = subprocess.run([node, str(SCENARIOS)], capture_output=True, text=True, timeout=120)
    if completed.returncode != 0:
        raise AssertionError('the node harness crashed: {0}'.format(completed.stderr))
    return json.loads(completed.stdout.strip().splitlines()[-1])


class ScenarioTestCase(SimpleTestCase):
    """Base: carrega o JSON dos cenários uma vez e dá os asserts de estado da tela."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.output = load_scenarios()
        cls.constants = cls.output['constants']
        cls.texts = cls.constants['texts']
        cls.login_target = '{0}?{1}={2}'.format(
            cls.constants['page']['loginUrl'], cls.constants['page']['redirectField'], cls.constants['page']['indexUrl'])

    def scenario(self, name):
        found = self.output['scenarios'][name]
        self.assertNotIn('harnessError', found, msg='the harness could not run {0}: {1}'.format(name, found.get('harnessError')))
        return found

    def assertAtRest(self, state, msg=''):
        """A tela liberada: Execute habilitado, Cancel e Revoke desabilitados e nenhum indicador de progresso."""
        for button, disabled in (('execute', False), ('cancel', True), ('revoke', True)):
            self.assertEqual(state[button]['disabled'], disabled, msg='{0} button disabled? {1}'.format(button, msg))
            self.assertFalse(state[button]['progressVisible'], msg='{0} button still shows progress. {1}'.format(button, msg))

    def assertAlertShows(self, state, text, msg=''):
        self.assertIsNotNone(state['alert'], msg='the page has no alert space. {0}'.format(msg))
        self.assertFalse(state['alert']['hidden'], msg='the alert space is hidden. {0}'.format(msg))
        self.assertEqual(state['alert']['text'], text, msg=msg)

    def assertAlertEmpty(self, state, msg=''):
        """Nenhum texto à mostra: sem o espaço, ou com o espaço oculto e vazio."""
        if state['alert'] is None:
            return
        self.assertTrue(state['alert']['hidden'], msg='the alert space is visible. {0}'.format(msg))
        self.assertEqual(state['alert']['text'], '', msg=msg)

    def expected_revoke_text(self, kind):
        return self.constants['serverErrorText'] if kind == 'falseWithError' else self.texts['revokeFailedText']


class RevocationFailureTest(ScenarioTestCase):
    """O pedido de revogação que falha (AC-8866-1, -2 e -17)."""

    def test_a_failed_revocation_shows_its_reason_on_the_page(self):
        """AC-8866-1: a revogação que falha mostra o motivo na tela, e não num alert() do navegador."""
        for kind in REVOKE_KINDS:
            for phase in REVOKE_PHASES:
                name = 'revoke:{0}:full:{1}'.format(kind, phase)
                with self.subTest(scenario=name):
                    after = self.scenario(name)['after']
                    self.assertAlertShows(after, self.expected_revoke_text(kind), msg=name)
                    self.assertEqual(after['browserAlerts'], [], msg='a browser alert() window was used')
                    self.assertEqual(after['uncaught'], [])

    def test_a_failed_revocation_gives_the_revoke_button_back_while_tracking(self):
        """AC-8866-2: com o Execute desabilitado, a falha deixa o Revoke habilitado e sem indicador; com o Execute habilitado, desabilitado."""
        for kind in REVOKE_KINDS:
            for phase in ('tracking', 'window'):
                name = 'revoke:{0}:full:{1}'.format(kind, phase)
                with self.subTest(scenario=name):
                    found = self.scenario(name)
                    # o botão esperava o servidor desabilitado, com o indicador: é o que a falha desfaz
                    self.assertTrue(found['clicked']['revoke']['disabled'])
                    self.assertTrue(found['clicked']['revoke']['progressVisible'])
                    self.assertTrue(found['after']['execute']['disabled'], msg='precondition: the screen is still tracking')
                    self.assertFalse(found['after']['revoke']['disabled'], msg='the revoke button stayed disabled')
                    self.assertFalse(found['after']['revoke']['progressVisible'], msg='the revoke button kept its progress indicator')
            name = 'revoke:{0}:full:late'.format(kind)
            with self.subTest(scenario=name):
                found = self.scenario(name)
                self.assertAtRest(found['released'], msg='precondition: the screen was released before the failure arrived')
                self.assertTrue(found['after']['revoke']['disabled'], msg='a late failure re-enabled revoke on a released screen')
                self.assertFalse(found['after']['revoke']['progressVisible'])
                self.assertFalse(found['after']['execute']['disabled'])

    def test_an_accepted_revocation_leaves_the_screen_alone(self):
        """AC-8866-17 (controle): a revogação aceita não mostra aviso e o Revoke segue desabilitado, com indicador, até a tela ser liberada."""
        found = self.scenario('revoke:accepted')
        accepted = found['afterAccepted']
        self.assertAlertEmpty(accepted)
        self.assertEqual(accepted['browserAlerts'], [])
        self.assertTrue(accepted['revoke']['disabled'])
        self.assertTrue(accepted['revoke']['progressVisible'])
        released = found['afterRelease']
        self.assertTrue(released['revoke']['disabled'])
        self.assertFalse(released['revoke']['progressVisible'])
        self.assertAtRest(released)


class StatusFailureTest(ScenarioTestCase):
    """A consulta de estado que não traz a tarefa (AC-8866-3, -4 e -5)."""

    def test_the_login_page_sends_the_user_to_the_login(self):
        """AC-8866-3: a consulta de estado que recebe 200 com o HTML do login leva o usuário ao login, com o retorno."""
        after = self.scenario('status:html200:full')['after']
        self.assertEqual(after['uncaught'], [], msg='the status callback raised on the login page')
        self.assertEqual(after['locations'], [self.login_target])
        self.assertAtRest(after, msg='the screen was left stuck: update-finish never fired')

    def test_the_login_page_in_the_middle_of_the_follow_up_sends_the_user_to_the_login(self):
        """AC-8866-3: a sessão que expira no meio do acompanhamento leva ao login (a consulta que sai do timer recebe o HTML do login)."""
        found = self.scenario('status:html200:midway')
        self.assertEqual(found['after']['uncaught'], [], msg='the status callback raised on the login page')
        self.assertEqual(found['after']['locations'], [self.login_target])
        self.assertAtRest(found['after'], msg='the screen was left stuck: update-finish never fired')
        self.assertEqual(found['later']['liveTimers'], 0, msg='a new query stayed scheduled')

    def test_an_error_page_with_an_http_error_status_is_not_the_login(self):
        """AC-8866-3 (controle): um erro HTTP com HTML (502) não leva ao login."""
        after = self.scenario('status:html502:full')['after']
        self.assertEqual(after['locations'], [])
        self.assertEqual(after['uncaught'], [])
        self.assertAtRest(after)

    def test_a_refused_status_query_tells_why(self):
        """AC-8866-4: o 403 e o 404 da consulta de estado mostram o motivo de cada um, e só o dele."""
        forbidden = self.scenario('status:http403:full')['after']
        self.assertAlertShows(forbidden, self.texts['forbiddenText'], msg='403')
        not_found = self.scenario('status:http404:full')['after']
        self.assertAlertShows(not_found, self.texts['notFoundText'], msg='404')
        self.assertNotEqual(forbidden['alert']['text'], not_found['alert']['text'])
        self.assertEqual(forbidden['browserAlerts'] + not_found['browserAlerts'], [])

    def test_a_failed_status_query_stops_and_releases_the_screen(self):
        """AC-8866-5 (controle): 403, 404, 429, 500 e a falha de rede param de consultar e liberam a tela."""
        for kind in ('http403', 'http404', 'http429', 'http500', 'network'):
            with self.subTest(failure=kind):
                found = self.scenario('status:{0}:full'.format(kind))
                self.assertAtRest(found['after'])
                self.assertEqual(found['after']['liveTimers'], 0, msg='a new query stayed scheduled')
                self.assertEqual(found['later']['statusRequests'], 1, msg='a new query went out after the failure')
                self.assertEqual(found['later']['liveTimers'], 0)


class CallbackFailureTest(ScenarioTestCase):
    """Um callback de outro código que lança não pode prender a tela (robustez, sem ID)."""

    def test_a_callback_that_raises_on_a_refused_query_does_not_keep_the_screen_stuck(self):
        # O update-finish sai num finally: a mesma classe de tela presa que o ticket corrige.
        after = self.scenario('status:callback-throws')['after']
        self.assertAtRest(after, msg='update-finish never fired after a callback raised')
        self.assertEqual([failure['message'] for failure in after['uncaught']], ['Error: host callback raised'],
                         msg='the callback error must still surface')
        self.assertAlertShows(after, self.texts['forbiddenText'], msg='the page handler runs before the host callback')


class AbortTest(ScenarioTestCase):
    """Interromper o acompanhamento: o Cancel e a troca de comando na lista (AC-8866-6 e -16)."""

    def test_an_interrupted_follow_up_ends_whole(self):
        """AC-8866-6: com uma consulta agendada, o Cancel e a troca de comando encerram o acompanhamento por inteiro."""
        for trigger in ('cancel', 'switch'):
            for waits in (1, 2, 3):
                name = 'abort:{0}:{1}:timer'.format(trigger, waits)
                with self.subTest(scenario=name):
                    found = self.scenario(name)
                    self.assertEqual(found['before']['liveTimers'], 1, msg='precondition: a new query is scheduled')
                    after = found['afterAbort']
                    self.assertEqual(after['liveTimers'], 0, msg='the scheduled query was not cancelled')
                    self.assertAtRest(after, msg='the screen was not released at once')
                    self.assertEqual(found['afterAdvance']['statusRequests'], after['statusRequests'],
                                     msg='a status query went out after the follow-up was interrupted')
                    second = found['second']
                    self.assertEqual(second['output'], 'SECOND OUTPUT', msg='the next command was not followed to its end')
                    self.assertAtRest(second)

    def test_switching_the_command_hides_the_copy_button(self):
        # Robustez (sem ID): a troca limpa o painel antes de liberar a tela, e o Copy acompanha o que o painel guarda.
        for waits in (1, 2, 3):
            with self.subTest(waits=waits):
                after = self.scenario('abort:switch:{0}:timer'.format(waits))['afterAbort']
                self.assertEqual(after['output'], '', msg='precondition: the panel was cleared')
                self.assertTrue(after['copyHidden'], msg='the copy button is visible over an empty panel')

    def test_without_the_abort_the_scheduled_query_goes_out(self):
        """AC-8866-6 (controle): sem interromper, a consulta agendada sai."""
        found = self.scenario('abort:none:2')
        self.assertEqual(found['before']['liveTimers'], 1)
        self.assertEqual(found['after']['statusRequests'], found['before']['statusRequests'] + 1)

    def test_an_abort_without_a_scheduled_query_does_not_release_the_screen(self):
        # Robustez (sem ID): o Cancel durante o envio do formulário ou da 1ª consulta em voo é ignorado (F3).
        for phase in ('first-inflight', 'submitting'):
            with self.subTest(phase=phase):
                self.assertTrue(self.scenario('abort:cancel:{0}'.format(phase))['after']['execute']['disabled'])

    def test_an_interrupted_follow_up_releases_the_screen_when_the_query_in_flight_answers(self):
        """AC-8866-16 (controle): com a consulta em voo (da 2ª em diante), a tela é liberada quando a resposta chega, sem a saída da tarefa."""
        for trigger in ('cancel', 'switch'):
            for waits in (2, 3):
                name = 'abort:{0}:{1}:flight'.format(trigger, waits)
                with self.subTest(scenario=name):
                    found = self.scenario(name)
                    self.assertEqual(found['afterAbort']['pendingStatus'], 1, msg='precondition: the query is in flight')
                    answered = found['afterAnswer']
                    self.assertAtRest(answered)
                    self.assertNotIn('TASK OUTPUT', answered['output'], msg='the task output was shown after the interruption')


class AlertClearingTest(ScenarioTestCase):
    """O aviso de falha some na ação seguinte do usuário (AC-8866-12)."""

    def test_the_next_user_action_hides_the_previous_alert(self):
        """AC-8866-12: enviar, interromper, trocar de comando na lista ou pedir de novo a revogação esconde o aviso anterior."""
        for trigger in ('submit', 'cancel', 'switch', 'revoke'):
            with self.subTest(trigger=trigger):
                found = self.scenario('clear:{0}'.format(trigger))
                self.assertIsNotNone(found['before']['alert'], msg='precondition: the page has an alert space')
                self.assertFalse(found['before']['alert']['hidden'], msg='precondition: an alert is on screen')
                self.assertNotEqual(found['before']['alert']['text'], '')
                self.assertTrue(found['after']['alert']['hidden'], msg='the alert stayed on screen')
                self.assertEqual(found['after']['alert']['text'], '')
        with self.subTest(trigger='none'):
            # controle: sem a ação do usuário o aviso fica
            found = self.scenario('clear:none')
            self.assertIsNotNone(found['before']['alert'])
            self.assertFalse(found['before']['alert']['hidden'])
            self.assertEqual(found['after']['alert'], found['before']['alert'])


class TaskOutputTest(ScenarioTestCase):
    """O fim da tarefa e o login do formulário, que continuam como estão (AC-8866-13 e -14)."""

    def test_a_finished_task_shows_its_output_as_text(self):
        """AC-8866-13 (controle): a tarefa que termina mostra a saída (ou o traceback) como texto e libera a tela."""
        for kind, expected in (('output', '<b>bold</b> output'), ('traceback', 'Traceback <i>x</i>')):
            with self.subTest(kind=kind):
                after = self.scenario('done:{0}'.format(kind))['after']
                self.assertEqual(after['output'], expected)
                self.assertEqual(after['outputNodes'], ['text'], msg='the output must reach the DOM as a text node only')
                self.assertFalse(after['copyHidden'])
                self.assertAtRest(after)

    def test_the_form_answered_with_the_login_page_goes_to_the_login(self):
        """AC-8866-14 (controle): o formulário respondido com a tela de login leva ao login, com o retorno."""
        after = self.scenario('form:login200')['after']
        self.assertEqual(after['locations'], [self.login_target])
        self.assertAtRest(after)


class FormLoginDataTypeTest(ScenarioTestCase):
    """O formulário pede JSON: é o que faz o HTML do login cair no error dele (AC-8866-14)."""

    def test_the_form_asks_for_json_so_the_login_page_reaches_its_error_handler(self):
        """AC-8866-14 (controle): o formulário pede dataType json; sem ele o HTML do login chegaria ao success e não ao error."""
        found = self.scenario('form:login200')
        self.assertEqual(found['dataType'], 'json', msg='the form no longer asks for json: the login page would reach success')


class ScriptsLoadTest(ScenarioTestCase):
    """Os scripts carregam e a página inicializa (AC-8866-10)."""

    def test_the_page_initialises_without_a_script_error(self):
        """AC-8866-10 (controle): os scripts executam com a página montada, com a lista de comandos ligada e desligada, e o ready completa."""
        for name, items in (('boot:list', 2), ('boot:text', 0)):
            with self.subTest(scenario=name):
                found = self.scenario(name)
                self.assertEqual(found['state']['uncaught'], [])
                self.assertEqual(found['state']['consoleErrors'], [])
                booted = found['boot']
                self.assertTrue(booted['ajaxFormBound'], msg='the ready function stopped before binding the form')
                self.assertTrue(booted['cancelBound'])
                self.assertTrue(booted['revokeBound'])
                self.assertGreaterEqual(booted['updateFinishCallbacks'], 1)
                self.assertEqual(booted['itemsBound'], items)


class HostTemplateTest(ScenarioTestCase):
    """Um host com template próprio, sem o espaço do aviso ou sem os textos (AC-8866-15)."""

    def test_a_host_template_without_the_alert_parts_is_tolerated(self):
        """AC-8866-15: sem o espaço do aviso ou sem os textos, nenhum erro de script; o servidor ainda manda no que aparece."""
        for template in ('nospace', 'spaceonly'):
            for kind in ('http403', 'http404'):
                name = 'status:{0}:{1}'.format(kind, template)
                with self.subTest(scenario=name):
                    after = self.scenario(name)['after']
                    self.assertEqual(after['uncaught'], [])
                    self.assertAtRest(after)
                    self.assertAlertEmpty(after, msg=name)
            for kind in REVOKE_KINDS:
                name = 'revoke:{0}:{1}:tracking'.format(kind, template)
                with self.subTest(scenario=name):
                    after = self.scenario(name)['after']
                    self.assertEqual(after['uncaught'], [])
                    # a revogação que falha segue o AC-8866-2: o Revoke volta habilitado com o Execute desabilitado
                    self.assertTrue(after['execute']['disabled'])
                    self.assertFalse(after['revoke']['disabled'], msg='the revoke button stayed disabled')
                    if template == 'nospace' or kind != 'falseWithError':
                        self.assertAlertEmpty(after, msg=name)
                    else:
                        self.assertAlertShows(after, self.constants['serverErrorText'], msg=name)

    def test_a_host_template_without_the_alert_parts_does_not_raise(self):
        # Robustez (sem ID): o que já vale hoje; nada lança e a consulta recusada libera a tela.
        for template in ('nospace', 'spaceonly'):
            for kind in ('http403', 'http404'):
                with self.subTest(template=template, failure=kind):
                    after = self.scenario('status:{0}:{1}'.format(kind, template))['after']
                    self.assertEqual(after['uncaught'], [])
                    self.assertAtRest(after)
            for kind in REVOKE_KINDS:
                with self.subTest(template=template, revoke=kind):
                    self.assertEqual(self.scenario('revoke:{0}:{1}:tracking'.format(kind, template))['after']['uncaught'], [])


def balanced_call(source, start):
    """Devolve o texto de `source` do `(` em `start` até o `)` que o fecha."""
    depth = 0
    for index in range(start, len(source)):
        if source[index] == '(':
            depth += 1
        elif source[index] == ')':
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError('unbalanced parentheses in cmdhandler.js')


class ButtonCollector(HTMLParser):
    """Coleta, por id, os atributos das tags da página."""

    def __init__(self):
        super().__init__()
        self.by_id = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get('id'):
            self.by_id[attributes['id']] = attributes


class DoubleAssumptionsTest(TestCase):
    """As suposições que o dublê de node fixa, conferidas contra o código e o template reais."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user()

    def test_the_status_query_and_the_revocation_ask_for_json(self):
        # O dublê manda o HTML do login ao .fail (parsererror) só quando o $.ajax pede dataType json: se um dos dois
        # deixar de pedir, o dublê passaria a provar o que o navegador não faz.
        source = CMDHANDLER.read_text(encoding='utf-8')
        # os comentários saem antes: uma menção a `$.ajax(` num comentário não é uma chamada
        source = re.sub(r'/\*.*?\*/', '', source, flags=re.S)
        source = re.sub(r'//[^\n]*', '', source)
        calls = [balanced_call(source, match.end() - 1) for match in re.finditer(r'\$\.ajax\(', source)]
        self.assertEqual(len(calls), 2, msg='expected the status query and the revocation')
        for call in calls:
            with self.subTest(call=call[:60]):
                self.assertRegex(call, r'dataType\s*:\s*[\'"]json[\'"]', msg='this $.ajax does not ask for dataType json')

    def test_the_double_starts_from_the_template_state_of_the_buttons(self):
        # O dublê assume Execute habilitado e Cancel e Revoke desabilitados; é o que o template entrega.
        self.client.force_login(self.user)
        collector = ButtonCollector()
        collector.feed(self.client.get(INDEX_URL).content.decode('utf-8'))
        initial = load_scenarios()['constants']['initialButtons']
        for button, key in (('btn-task-submit', 'execute'), ('btn-task-cancel', 'cancel'), ('btn-task-revoke', 'revoke')):
            with self.subTest(button=button):
                self.assertIn(button, collector.by_id)
                self.assertEqual('disabled' in collector.by_id[button], initial[key])
