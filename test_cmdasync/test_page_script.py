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
JQUERY_FORM = ROOT / 'commands_async' / 'static' / 'cmdasync' / 'js' / 'jquery.form.v4.4.2.min.js'
STATIC_CMDASYNC = ROOT / 'commands_async' / 'static' / 'cmdasync'

REVOKE_KINDS = ('http403', 'http404', 'http500', 'html200', 'falseWithError', 'falseNoError')
REVOKE_PHASES = ('tracking', 'late')
# Os tipos de falha da consulta de estado que chegam ao `.fail` do handler (o `FAILURE_KINDS` do scenarios.js).
STATUS_FAILURE_KINDS = ('html200', 'http403', 'http404', 'http500', 'network')


def interrupted_answer_scenarios():
    """Os cenários em que a consulta de estado em voo TERMINA depois da interrupção, por resposta ou por falha (AC-8967-1).

    Devolve (nome, o que é o fim): 'answer' para a resposta (pronta ou não) e 'failure' para a falha.
    """
    found = []
    for trigger in ('cancel', 'switch'):
        for answer in ('notready', 'ready'):
            found.append(('first:{0}:{1}'.format(trigger, answer), 'answer'))
        for waits in (2, 3):
            found.append(('abort:{0}:{1}:flight'.format(trigger, waits), 'answer'))
            found.append(('abort:{0}:{1}:flight:notready'.format(trigger, waits), 'answer'))
            for kind in STATUS_FAILURE_KINDS:
                found.append(('abort:{0}:{1}:flight:fail:{2}'.format(trigger, waits, kind), 'failure'))
    found.append(('abort:cancel:2:flight:fail:callback-throws', 'failure'))
    return found


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
            name = 'revoke:{0}:full:tracking'.format(kind)
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
    """Interromper o acompanhamento: o Cancel e a troca de comando na lista (AC-8866-6 e -16, AC-8955-1 e -2, AC-8967-1 a -6)."""

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
        """AC-8967-6 (controle): a troca de comando com a consulta agendada libera a tela na hora, com o painel vazio e o Copy escondido."""
        # A troca limpa o painel antes de liberar a tela (a página), e o Copy acompanha o que o painel guarda.
        for waits in (1, 2, 3):
            with self.subTest(waits=waits):
                after = self.scenario('abort:switch:{0}:timer'.format(waits))['afterAbort']
                self.assertAtRest(after, msg='the screen was not released at once')
                self.assertEqual(after['output'], '', msg='precondition: the panel was cleared')
                self.assertTrue(after['copyHidden'], msg='the copy button is visible over an empty panel')

    def test_cancelling_with_a_scheduled_query_leaves_the_panel_empty(self):
        """AC-8967-2: o Cancel com uma consulta agendada libera a tela na hora, com o painel de saída vazio e o Copy escondido."""
        for waits in (1, 2, 3):
            name = 'abort:cancel:{0}:timer'.format(waits)
            with self.subTest(scenario=name):
                found = self.scenario(name)
                self.assertEqual(found['before']['liveTimers'], 1, msg='precondition: a new query is scheduled')
                self.assertEqual(found['before']['output'], self.texts['runningText'],
                                 msg='precondition: the panel holds the text of the previous query')
                after = found['afterAbort']
                self.assertAtRest(after, msg='the screen was not released at once')
                self.assertEqual(after['output'], '', msg='the panel kept the running text after the Cancel')
                self.assertTrue(after['copyHidden'], msg='the copy button is visible over the running text')

    def test_an_interrupted_query_that_ends_leaves_the_panel_empty(self):
        """AC-8967-1: depois do Cancel ou da troca, a consulta em voo que termina (resposta ou falha) libera a tela com o painel vazio e o Copy escondido."""
        scenarios = interrupted_answer_scenarios()
        self.assertEqual(len(scenarios), 33, msg='the scenario list changed size')
        for name, ending in scenarios:
            with self.subTest(scenario=name, ending=ending):
                found = self.scenario(name)
                self.assertEqual(found['afterAbort']['pendingStatus'], 1, msg='precondition: the query is in flight')
                answered = found['afterAnswer']
                self.assertEqual(answered['pendingStatus'], 0, msg='precondition: the query ended')
                self.assertAtRest(answered, msg='the screen was not released when the query ended')
                self.assertEqual(answered['output'], '', msg='the panel is not empty after the query ended')
                self.assertTrue(answered['copyHidden'], msg='the copy button is visible after the query ended')

    def test_a_failed_first_query_after_the_interruption_leaves_the_panel_empty(self):
        """AC-8967-1 (controle): a 1ª consulta que falha depois da interrupção libera a tela com o painel vazio (ele nunca guardou o texto)."""
        for trigger in ('cancel', 'switch'):
            for kind in STATUS_FAILURE_KINDS:
                name = 'first:{0}:fail:{1}'.format(trigger, kind)
                with self.subTest(scenario=name):
                    found = self.scenario(name)
                    self.assertEqual(found['afterAbort']['pendingStatus'], 1, msg='precondition: the first query is in flight')
                    answered = found['afterAnswer']
                    self.assertAtRest(answered, msg='the screen was not released when the query failed')
                    self.assertEqual(answered['output'], '')
                    self.assertTrue(answered['copyHidden'])

    def test_a_host_callback_that_raises_on_update_failed_does_not_leave_the_running_text(self):
        """AC-8967-1: o ouvinte de host de `update-failed` que lança não impede o painel vazio (o `empty()` vem antes dele)."""
        found = self.scenario('abort:cancel:2:flight:fail:callback-throws')
        self.assertEqual([failure['message'] for failure in found['afterAnswer']['uncaught']], ['Error: host callback raised'],
                         msg='precondition: the host callback raised')
        self.assertEqual(found['afterAbort']['output'], self.texts['runningText'], msg='precondition: the panel held the running text')
        answered = found['afterAnswer']
        self.assertAtRest(answered, msg='the screen was not released')
        self.assertEqual(answered['output'], '', msg='the panel kept the running text')
        self.assertTrue(answered['copyHidden'])

    def test_the_running_text_shows_while_the_follow_up_goes_on(self):
        """AC-8967-3 (controle): com o acompanhamento ativo, a resposta «não pronta» deixa o texto de execução no painel."""
        for name in ('abort:none:1', 'abort:none:2'):
            with self.subTest(scenario=name):
                found = self.scenario(name)
                self.assertEqual(found['before']['output'], self.texts['runningText'])
                self.assertEqual(found['after']['output'], self.texts['runningText'], msg='the text went away while following')
                self.assertEqual(found['after']['pendingStatus'], 1, msg='precondition: the follow-up goes on')

    def test_the_running_text_is_the_one_the_template_publishes(self):
        """AC-8967-3: o texto de execução no painel é o que o template publica (traduzido quando há tradução), e não o literal em inglês."""
        found = self.scenario('abort:none:1:translated')
        translated = 'Executando comando...'
        self.assertNotEqual(translated, self.texts['runningText'], msg='precondition: the published text is not the English one')
        self.assertEqual(found['before']['output'], translated, msg='the page wrote a text the template did not publish')
        self.assertEqual(found['after']['output'], translated, msg='the text went away while following')
        self.assertEqual(found['after']['pendingStatus'], 1, msg='precondition: the follow-up goes on')

    def test_the_updating_event_is_notified_once_per_answer_while_following(self):
        """AC-8967-4 (controle): com o acompanhamento ativo, cada resposta (pronta ou não) avisa o `updating` uma vez."""
        for name, answers in (('abort:none:1', 1), ('abort:none:2', 2)):
            with self.subTest(scenario=name):
                found = self.scenario(name)
                self.assertEqual(found['before']['updatingCalls'], answers)
                # a consulta que saiu depois ainda não foi respondida: nenhum aviso a mais
                self.assertEqual(found['after']['updatingCalls'], answers)
        with self.subTest(scenario='follow:ready'):
            found = self.scenario('follow:ready')
            self.assertEqual(found['afterNotReady']['updatingCalls'], 1)
            self.assertEqual(found['afterReady']['updatingCalls'], 2, msg='the ready answer did not notify updating')
            self.assertEqual(found['afterReady']['output'], 'TASK OUTPUT', msg='precondition: the ready answer ended the follow-up')

    def test_the_updating_event_is_not_notified_after_an_interruption(self):
        """AC-8967-5: depois do Cancel ou da troca, a resposta da consulta em voo (pronta ou não) não avisa o `updating`."""
        cases = []
        for trigger in ('cancel', 'switch'):
            for answer in ('notready', 'ready'):
                cases.append(('first:{0}:{1}'.format(trigger, answer), 0))
            for waits in (2, 3):
                cases.append(('abort:{0}:{1}:flight'.format(trigger, waits), waits))
                cases.append(('abort:{0}:{1}:flight:notready'.format(trigger, waits), waits))
        for name, before_interruption in cases:
            with self.subTest(scenario=name):
                found = self.scenario(name)
                self.assertEqual(found['afterAbort']['updatingCalls'], before_interruption,
                                 msg='precondition: one notification per answer before the interruption')
                self.assertEqual(found['afterAnswer']['pendingStatus'], 0, msg='precondition: the query was answered')
                self.assertEqual(found['afterAnswer']['updatingCalls'], before_interruption,
                                 msg='updating was notified for the answer of an interrupted follow-up')

    def test_a_failure_without_interruption_keeps_the_screen_as_it_is(self):
        # Controle sem ID: a falha da consulta SEM interrupção segue como está neste ticket (o texto de execução
        # e o Copy ficam); mudar isso é o follow-up F1 do #8967, que troca este teste.
        after = self.scenario('status:html200:midway')['after']
        self.assertAtRest(after, msg='the screen was not released')
        self.assertEqual(after['output'], self.texts['runningText'])
        self.assertFalse(after['copyHidden'])

    def test_without_the_abort_the_scheduled_query_goes_out(self):
        """AC-8866-6 (controle): sem interromper, a consulta agendada sai."""
        found = self.scenario('abort:none:2')
        self.assertEqual(found['before']['liveTimers'], 1)
        self.assertEqual(found['after']['statusRequests'], found['before']['statusRequests'] + 1)

    def test_an_interruption_during_the_submit_ends_the_follow_up_whole(self):
        """AC-8955-1: o Cancel e a troca de comando durante o envio do formulário liberam a tela quando a resposta chega, sem consulta nem saída, e o comando seguinte é acompanhado até o fim."""
        for trigger in ('cancel', 'switch'):
            with self.subTest(trigger=trigger):
                found = self.scenario('submit:{0}'.format(trigger))
                self.assertEqual(found['before']['statusRequests'], 0, msg='precondition: the form is still in flight')
                answered = found['afterAnswer']
                self.assertEqual(answered['statusRequests'], 0, msg='a status query went out after the interruption')
                self.assertEqual(answered['pendingStatus'], 0)
                self.assertAtRest(answered, msg='the screen was not released when the answer of the form arrived')
                self.assertEqual(answered['output'], '', msg='an output was shown after the interruption')
                self.assertEqual(found['afterAdvance']['statusRequests'], 0, msg='a status query went out later')
                second = found['second']
                self.assertEqual(second['output'], 'SECOND OUTPUT', msg='the next command was not followed to its end')
                self.assertAtRest(second)
        with self.subTest(trigger='none'):
            # controle: sem a interrupção o mesmo envio consulta o estado
            found = self.scenario('submit:none')
            self.assertEqual(found['afterAnswer']['statusRequests'], 1)
            self.assertEqual(found['afterAnswer']['pendingStatus'], 1)

    def test_an_interruption_with_the_first_query_in_flight_releases_the_screen_when_it_answers(self):
        """AC-8955-2: o Cancel e a troca de comando com a 1ª consulta em voo liberam a tela quando a resposta chega, sem a saída da tarefa nem outra consulta, e o comando seguinte é acompanhado até o fim."""
        for trigger in ('cancel', 'switch'):
            for answer in ('notready', 'ready'):
                name = 'first:{0}:{1}'.format(trigger, answer)
                with self.subTest(scenario=name):
                    found = self.scenario(name)
                    self.assertEqual(found['afterAbort']['pendingStatus'], 1, msg='precondition: the first query is in flight')
                    self.assertEqual(found['afterAbort']['statusRequests'], 1)
                    answered = found['afterAnswer']
                    self.assertAtRest(answered, msg='the screen was not released when the first query answered')
                    self.assertNotIn('TASK OUTPUT', answered['output'], msg='the task output was shown after the interruption')
                    self.assertEqual(answered['liveTimers'], 0, msg='a new query was scheduled')
                    self.assertEqual(found['afterAdvance']['statusRequests'], 1, msg='another query went out')
                    second = found['second']
                    self.assertEqual(second['output'], 'SECOND OUTPUT', msg='the next command was not followed to its end')
                    self.assertAtRest(second)
        with self.subTest(case='control'):
            # controle: sem a interrupção, a resposta «não pronta» agenda a 2ª consulta e ela sai
            found = self.scenario('abort:none:1')
            self.assertEqual(found['before']['liveTimers'], 1)
            self.assertEqual(found['after']['statusRequests'], found['before']['statusRequests'] + 1)

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


class RevokeWindowTest(ScenarioTestCase):
    """O Revoke entre o envio do formulário e a resposta do servidor (AC-8955-3, -4 e -5)."""

    def test_the_revoke_button_waits_for_the_server_to_answer_the_form(self):
        """AC-8955-3: do envio até a resposta com a tarefa o Revoke está desabilitado (mesmo se um pedido por script falhar); com a resposta, habilitado."""
        for run in ('first', 'second'):
            with self.subTest(run=run):
                found = self.scenario('revoke-window:{0}'.format(run))
                window = found['inWindow']
                self.assertTrue(window['execute']['disabled'], msg='precondition: the form is in flight')
                self.assertTrue(window['revoke']['disabled'], msg='the revoke button is enabled before the server answered the form')
                self.assertFalse(found['clicked'], msg='a click reached the disabled revoke button')
                self.assertEqual(found['afterClick']['revokeRequests'], 0)
                # um pedido de revogação por script na janela (a guarda e o aviso) deixa o botão como está
                scripted = found['afterScript']
                self.assertTrue(scripted['revoke']['disabled'], msg='a script request enabled the revoke button before the answer')
                self.assertFalse(scripted['revoke']['progressVisible'])
                answered = found['afterAnswer']
                self.assertTrue(answered['execute']['disabled'], msg='precondition: the follow-up goes on')
                self.assertFalse(answered['revoke']['disabled'], msg='the revoke button stayed disabled after the server answered the form')
                self.assertFalse(answered['revoke']['progressVisible'])

    def test_a_late_failure_of_the_previous_revocation_does_not_enable_the_button_in_the_submit_window(self):
        """AC-8955-3: a falha tardia da revogação de uma execução anterior, na janela do envio seguinte, mostra o aviso e deixa o Revoke desabilitado."""
        found = self.scenario('revoke-window:late-failure')
        self.assertAtRest(found['released'], msg='precondition: the screen was released by the conclusion')
        window = found['inWindow']
        self.assertTrue(window['execute']['disabled'], msg='precondition: a new form is in flight')
        self.assertTrue(window['revoke']['disabled'], msg='precondition: no task was accepted yet, the revoke button is disabled')
        after = found['afterFailure']
        self.assertAlertShows(after, self.texts['revokeFailedText'])
        self.assertTrue(after['revoke']['disabled'], msg='the late failure enabled the revoke button in the submit window')
        self.assertFalse(after['revoke']['progressVisible'])

    def test_the_revoke_click_after_the_answer_asks_for_the_task_of_that_execution(self):
        """AC-8955-4 (controle): o clique no Revoke depois da resposta pede a revogação da tarefa dessa execução, e não a de uma anterior."""
        found = self.scenario('revoke:after-answer')
        self.assertFalse(found['afterAnswer']['revoke']['disabled'], msg='precondition: the revoke button is enabled after the answer')
        self.assertTrue(found['clicked'])
        self.assertEqual(found['revokeUrls'], ['status/t2'])

    def test_a_script_revocation_without_an_accepted_task_is_a_failed_revocation(self):
        """AC-8955-5: a revogação pedida por script sem tarefa aceita na execução em curso não envia pedido nenhum, não lança erro e mostra o aviso de falha."""
        for kind in ('before-any-submit', 'first-window', 'second-window', 'refused'):
            name = 'script-revoke:{0}:full'.format(kind)
            with self.subTest(scenario=name):
                found = self.scenario(name)
                self.assertIsNone(found['threw'], msg='the script request raised an error')
                after = found['after']
                self.assertEqual(found['revokeUrls'], [], msg='a revocation request was sent')
                self.assertEqual(after['revokeRequests'], 0)
                self.assertEqual(after['uncaught'], [])
                self.assertAlertShows(after, self.texts['revokeFailedText'], msg=name)
                self.assertEqual(after['browserAlerts'], [], msg='a browser alert() window was used')
        with self.subTest(scenario='script-revoke:first-window:nospace'):
            # um host sem o espaço do aviso (AC-8866-15): nenhum erro e nenhum texto
            found = self.scenario('script-revoke:first-window:nospace')
            self.assertIsNone(found['threw'], msg='the script request raised an error')
            self.assertEqual(found['revokeUrls'], [])
            self.assertEqual(found['after']['uncaught'], [])
            self.assertAlertEmpty(found['after'])


class FollowUpRobustnessTest(ScenarioTestCase):
    """Robustez (sem ID): os caminhos do desenho que os critérios não provam sozinhos."""

    def test_a_duplicate_submit_the_page_refuses_leaves_the_follow_up_alone(self):
        # Robustez (sem ID): o beforeSubmit que o ajax() entrega ao ajaxForm repassa a recusa da página e não reinicia o estado.
        found = self.scenario('robust:duplicate:after-cancel')
        self.assertIs(found['duplicateProceeded'], False, msg='the page no longer refuses a second submit, or the wrapper swallowed the refusal')
        self.assertAtRest(found['afterAnswer'], msg='the second submit undid the interruption already asked')
        self.assertEqual(found['afterAnswer']['statusRequests'], 0)
        found = self.scenario('robust:duplicate:following')
        self.assertIs(found['duplicateProceeded'], False)
        self.assertEqual(found['revokeUrls'], ['status/t1'], msg='the second submit reset the task of the follow-up in course')

    def test_a_refused_form_after_an_interruption_leaves_the_next_command_followed(self):
        # Robustez (sem ID): o envio recusado (400) depois de uma interrupção deixa o estado do acompanhamento sujo até o próximo envio.
        found = self.scenario('robust:refused-after-interrupt')
        self.assertAtRest(found['afterRefusal'], msg='precondition: the refused form released the screen')
        second = found['second']
        self.assertEqual(second['output'], 'SECOND OUTPUT', msg='the command after the refused form was not followed to its end')
        self.assertAtRest(second)

    def test_a_host_that_fires_form_valid_without_the_handler_submit_is_followed_as_before(self):
        # Robustez (sem ID): sem o begin() (running falso), o form-valid chama o request(), que reinicia o update.
        found = self.scenario('robust:host-without-begin')
        self.assertAtRest(found['released'], msg='precondition: the interruption in the submit released the screen')
        self.assertEqual(found['released']['statusRequests'], 0, msg='precondition: no query went out')
        self.assertEqual(found['afterDirect']['statusRequests'], 1, msg='the first query did not go out')
        answered = found['afterAnswer']
        self.assertEqual(answered['output'], 'HOST OUTPUT', msg='the answer of the first query was not shown')
        self.assertAtRest(answered)

    def test_the_screen_released_by_an_interruption_still_revokes_the_last_task(self):
        # Robustez (sem ID): a fronteira do AC-8955-5: a tela liberada (não o envio em curso) pede a última tarefa, sem aviso.
        found = self.scenario('robust:released-boundary')
        self.assertAtRest(found['released'], msg='precondition: the interruption released the screen')
        self.assertIsNone(found['threw'])
        self.assertEqual(found['revokeUrls'], ['status/t1'], msg='the last task was not asked to be revoked')
        self.assertAlertEmpty(found['after'])

    def test_ajax_without_a_before_submit_of_the_host_still_begins_the_follow_up(self):
        # Robustez (sem ID): o embrulho do beforeSubmit não pode chamar uma página que não existe.
        found = self.scenario('robust:ajax-without-before-submit')
        self.assertEqual(found['beforeSubmitType'], 'function', msg='ajax() did not give the form a beforeSubmit')
        self.assertNotIn('threw', found, msg='the beforeSubmit raised: {0}'.format(found.get('threw')))
        self.assertEqual(found['returnedType'], 'undefined')
        # o envio começou (begin): a interrupção pedida antes da resposta encerra sem consulta
        self.assertEqual(found['afterFormValid']['statusRequests'], 0, msg='the interruption did not stop the first query')


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

    def test_the_page_initialises_with_the_workers_probe_on(self):
        """AC-8954-9 (controle): os scripts, com o de workers e o setting ligados, executam com a página montada e o ready completa."""
        found = self.scenario('boot:workers')
        self.assertEqual(found['state']['uncaught'], [])
        self.assertEqual(found['state']['consoleErrors'], [])
        self.assertEqual(found['state']['workersRequests'], 1, msg='the page did not start the workers probe')
        booted = found['boot']
        self.assertTrue(booted['ajaxFormBound'], msg='the ready function stopped before binding the form')
        self.assertTrue(booted['cancelBound'])
        self.assertTrue(booted['revokeBound'])


class WorkersProbeTest(ScenarioTestCase):
    """A consulta de workers da tela de comandos (`celery.workers.status.js`, com o setting ligado): a recusa que o tempo não conserta
    para a consulta, a que o tempo conserta a refaz (AC-8954-1 a -3 e -5 a -8)."""

    def test_a_forbidden_probe_stops_asking(self):
        """AC-8954-1: o 403 da consulta de workers, na 1ª consulta ou depois de consultas que falharam ou não acharam worker, para a consulta."""
        for prior in ('first', 'network', 'nobody', 'nobody+network'):
            name = 'workers:forbidden:{0}'.format(prior)
            with self.subTest(scenario=name):
                found = self.scenario(name)
                before = found['beforeForbidden']
                self.assertEqual(before['pendingWorkers'], 1, msg='precondition: the query is in flight')
                self.assertEqual(found['afterForbidden']['liveTimers'], 0, msg='a new query was scheduled after the refusal')
                self.assertEqual(found['later']['workersRequests'], before['workersRequests'], msg='a query went out after the refusal')
        with self.subTest(case='control'):
            # controle: com a falha de rede no lugar do 403, a consulta sai de novo
            found = self.scenario('workers:retry:network')
            self.assertEqual(found['at5000']['workersRequests'], found['afterAnswer']['workersRequests'] + 1)

    def test_a_forbidden_probe_tells_why_in_the_notice(self):
        """AC-8954-2: o 403 mostra no aviso dos workers a frase de acesso negado, como texto e no lugar do que o aviso mostrava."""
        phrase = self.texts['workersForbiddenText']
        for prior in ('first', 'nobody'):
            name = 'workers:forbidden:{0}'.format(prior)
            with self.subTest(scenario=name):
                found = self.scenario(name)
                toast = found['afterForbidden']['toast']
                self.assertEqual(toast['body'], [phrase], msg='the notice does not carry only the phrase of the refusal')
                self.assertEqual(toast['shown'], found['beforeForbidden']['toast']['shown'] + 1,
                                 msg='the refusal did not open the notice')

    def test_the_login_page_on_the_probe_sends_the_user_to_the_login(self):
        """AC-8954-3: o HTML do login na consulta de workers, na 1ª consulta ou depois de falhas, leva ao login com o retorno e para a consulta."""
        for prior in ('first', 'network', 'nobody+network'):
            name = 'workers:login:{0}'.format(prior)
            with self.subTest(scenario=name):
                found = self.scenario(name)
                before = found['beforeLogin']
                self.assertEqual(before['pendingWorkers'], 1, msg='precondition: the query is in flight')
                after = found['afterLogin']
                self.assertEqual(after['uncaught'], [])
                self.assertEqual(after['locations'], [self.login_target], msg='the user was not sent to the login')
                self.assertEqual(after['liveTimers'], 0, msg='a new query was scheduled after the login page')
                self.assertEqual(found['later']['workersRequests'], before['workersRequests'], msg='a query went out after the login page')

    def test_a_host_without_the_notice_the_plugin_or_the_phrase_stops_without_an_error(self):
        """AC-8954-5: sem o aviso, sem o plugin de toast ou sem a frase, o 403 para a consulta sem erro de script e sem abrir um aviso sem texto."""
        for toast in ('nophrase', 'noplugin', 'none'):
            name = 'workers:host:{0}'.format(toast)
            with self.subTest(scenario=name):
                found = self.scenario(name)
                before = found['beforeForbidden']
                after = found['afterForbidden']
                self.assertEqual(before['pendingWorkers'], 1, msg='precondition: the query is in flight')
                self.assertEqual(after['uncaught'], [], msg='the refusal raised a script error')
                self.assertEqual(after['liveTimers'], 0, msg='a new query was scheduled after the refusal')
                self.assertEqual(found['later']['workersRequests'], before['workersRequests'], msg='a query went out after the refusal')
                self.assertEqual(after['toast']['shown'], 0, msg='a notice without text was opened')
                self.assertEqual(after['toast']['body'], [])

    def test_a_probe_asked_without_the_login_destination_stops_on_the_login_page(self):
        """AC-8954-8: a consulta chamada sem o destino do login para ao receber a tela de login, sem erro de script."""
        found = self.scenario('workers:noarg:login')
        self.assertEqual(found['asked']['pendingWorkers'], 1, msg='precondition: the query is in flight')
        after = found['afterAnswer']
        self.assertEqual(after['uncaught'], [], msg='the login page raised a script error')
        self.assertEqual(after['liveTimers'], 0, msg='a new query was scheduled after the login page')
        self.assertEqual(found['at5000']['workersRequests'], 1, msg='a query went out after the login page')
        self.assertEqual(after['locations'], [], msg='the page went to a login without a destination')

    def test_a_probe_that_fails_for_a_reason_that_passes_asks_again_after_5_seconds(self):
        """AC-8954-6 (controle): o `status` falso, a falha de rede e o erro de servidor refazem a consulta 5 segundos depois."""
        for kind in ('nobody', 'network', 'http500', 'http502'):
            name = 'workers:retry:{0}'.format(kind)
            with self.subTest(scenario=name):
                found = self.scenario(name)
                asked = found['afterAnswer']['workersRequests']
                self.assertEqual(found['at4999']['workersRequests'], asked, msg='the query was asked again before 5 seconds')
                self.assertEqual(found['at5000']['workersRequests'], asked + 1, msg='the query was not asked again after 5 seconds')
        with self.subTest(case='asked without an argument'):
            found = self.scenario('workers:noarg:network')
            self.assertEqual(found['at4999']['workersRequests'], 1)
            self.assertEqual(found['at5000']['workersRequests'], 2)
        with self.subTest(case='a handler without failure_reason'):
            found = self.scenario('workers:retry:network:noreason')
            self.assertEqual(found['afterAnswer']['uncaught'], [])
            self.assertEqual(found['at4999']['workersRequests'], 1)
            self.assertEqual(found['at5000']['workersRequests'], 2)

    def test_the_probe_result_reaches_the_notice_as_text(self):
        """AC-8954-7 (controle): o resultado da consulta de workers vai ao aviso como texto, e a lista não refaz a consulta."""
        listed = self.scenario('workers:result:listed')
        toast = listed['afterAnswer']['toast']
        self.assertEqual(toast['body'], ['celery@w1', '<b>w2</b>'], msg='each worker name is one text of the notice')
        self.assertEqual(toast['badge'], '2')
        self.assertEqual(toast['shown'], 1)
        self.assertEqual(listed['afterAnswer']['liveTimers'], 0)
        self.assertEqual(listed['later']['workersRequests'], 1, msg='the list asked again')
        nobody = self.scenario('workers:result:nobody')
        self.assertEqual(nobody['afterAnswer']['toast']['body'], ['no Celery worker answered'])
        self.assertEqual(nobody['afterAnswer']['toast']['shown'], 1)


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

    def test_jquery_form_asks_before_submit_before_the_ajax_and_aborts_on_false(self):
        # O dublê (world.js, executeStart) chama o beforeSubmit à mão e segue o envio se ele não devolver false, e não executa o
        # jquery.form: é essa ordem que o begin() do ajax() usa (o begin() roda no beforeSubmit, antes do $.ajax do envio, e uma
        # página que o recusa não começa nada). Lida na fonte entregue (jquery.form.v4.4.2.min.js, ajaxSubmit); se a versão
        # do jquery.form mudar, este teste obriga a conferir a ordem de novo.
        source = JQUERY_FORM.read_text(encoding='utf-8')
        calls = list(re.finditer(r't\.beforeSubmit&&!1===t\.beforeSubmit\([^)]*\)\)return', source))
        self.assertEqual(len(calls), 1, msg='the beforeSubmit that aborts the submit on false was not found, or is found twice')
        position = calls[0].start()
        validate = source.index('form-submit-validate')
        ajax = re.search(r'\.ajax\(', source)
        self.assertLess(position, validate, msg='the beforeSubmit no longer runs before the form-submit-validate event')
        self.assertLess(position, ajax.start(), msg='the beforeSubmit no longer runs before the $.ajax of the submit')

    def test_the_notice_of_the_page_has_the_body_and_the_badge_the_double_assumes(self):
        # O dublê (world.js) dá ao #popup-msg um `.toast-body` e um `.online` à mão; o script de workers escreve a frase do 403, os
        # nomes e a mensagem no `.toast-body` e o número no `.online`. Lido na página real (#8954): se o template renomear um dos
        # dois, o aviso sai vazio na tela e os testes de node seguem verdes.
        self.client.force_login(self.user)
        html = self.client.get(INDEX_URL).content.decode('utf-8')
        start = html.index("id='popup-msg'")
        notice = html[start:html.index('id="wrap"', start)]
        self.assertRegex(notice, r'class="[^"]*\btoast-body\b', msg='the notice of the page lost its .toast-body')
        self.assertRegex(notice, r'class="[^"]*\bonline\b', msg='the notice of the page lost its .online badge')

    def test_the_page_loads_the_bootstrap_bundle_that_registers_the_toast_plugin(self):
        # O script de workers só abre o aviso quando `$.fn.toast` existe (um host sem o bootstrap.bundle não o tem, e a chamada lançaria):
        # a página do pacote tem de carregar o bundle, e o bundle vendido tem de registrar o plugin, senão o guarda engole o aviso
        # em silêncio (#8954).
        self.client.force_login(self.user)
        html = self.client.get(INDEX_URL).content.decode('utf-8')
        loaded = re.findall(r'<script[^>]*src="[^"]*(bootstrap-[0-9.]+)/js/bootstrap\.bundle\.min\.js"', html)
        self.assertEqual(len(loaded), 1, msg='the page does not load the bootstrap bundle exactly once')
        bundle = STATIC_CMDASYNC / loaded[0] / 'js' / 'bootstrap.bundle.min.js'
        self.assertRegex(bundle.read_text(encoding='utf-8'), r'\.fn\.toast=[A-Za-z]+\._jQueryInterface',
                         msg='the vendored bundle does not register the toast plugin')

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
