# coding=utf-8
"""Os campos de argumentos da tela de comandos nas duas variantes, a larga e a estreita (#9019).

Critérios deste módulo: AC-9019-2 (o erro do 400 ao lado do campo recusado, em cada variante), AC-9019-3 (o texto de um campo vai ao
campo do formulário que segue com o comando), AC-9019-4 (e à outra variante), AC-9019-5 (a próxima resposta deixa só os erros dela
nas variantes), AC-9019-6 (a página não repete `id`), AC-9019-7 (a página traz as duas variantes e a folha de estilos), AC-9019-8 (um
host com uma variante só não lança erro de script), AC-9019-9 (a próxima resposta deixa só o erro dela ao lado do campo do comando),
AC-9019-10 (a mensagem de cada campo entra como texto) e AC-9019-11 (o rótulo e a descrição de cada campo apontam para a própria
variante). O AC-9019-1 (o erro do campo do comando) e o AC-9019-7 (a regra de visibilidade por largura) têm o teste de sempre em
`test_page_script.py`, com o ID novo, e o AC-9019-10 também no `DomSinkTest` de `test_page.py`.

Os scripts REAIS da tela rodam em node, com o dublê de jQuery e de DOM de `js_harness/world.js` (que agora tem as duas variantes, os
eventos `input` e `change`, a remoção de elementos e a lista dos sumidouros de marcação); os testes de página leem o `index.html`
renderizado. A parte `DoubleAssumptions` confere, contra o código e os templates reais, o que cada regra nova do dublê supõe.

Execução (container `django`, com o `pyenv` do projeto; precisa do `node`, que vem no container):
    cd /opt/project/packages/django-cmd-async && /opt/project/pyenv/bin/python runtests.py test_cmdasync.test_args_fields -v 2

O que cada teste falharia se: o `change` de uma variante não tiver ouvinte (AC-9019-3), o texto não chegar ao campo oculto, ao outro campo
ou a nenhum dos dois, o vazio não passar (AC-9019-3 e -4); o erro do 400 só ao lado do primeiro campo da lista, a mensagem sem a marca ou
a marca sem a mensagem (AC-9019-2); a limpeza só do primeiro campo, ou que não roda na falha de rede (AC-9019-5 e -9); uma só mensagem
anexada às duas variantes (AC-9019-5); a mensagem inserida como marcação (AC-9019-10); o script que exige as duas variantes (AC-9019-8);
o `id` da estreita de volta ao da larga ou o `for` e o `aria-describedby` dela apontando para a larga (AC-9019-6 e -11).

Dois testes de robustez da implementação, sem ID de critério (o texto de nenhum critério os diz): o espelho do texto que regrava o campo
em que o usuário digita, e o laço do erro do 400 sem a guarda de campo fora do mapa (`ghost`, `constructor`, `__proto__`).
"""
import re
from collections import Counter
from html.parser import HTMLParser

from django.test import TestCase

from commands_async import settings as app_settings
from test_cmdasync.harness import INDEX_URL, create_user
from test_cmdasync.test_page_script import STATIC_CMDASYNC, ScenarioTestCase, load_scenarios

VARIANTS = ('wide', 'narrow')
FIELDS = ('args', 'kwargs')
EVENTS = ('input', 'change')
PAGE_SCRIPT = STATIC_CMDASYNC / 'js' / 'cmdasync-page.js'
JQUERY = STATIC_CMDASYNC / 'js' / 'jquery-3.7.1.min.js'


def other(field):
    return 'kwargs' if field == 'args' else 'args'


def other_variant(variant):
    return 'narrow' if variant == 'wide' else 'wide'


def typing_name(world, variant, field, event):
    """O nome do cenário em que o usuário muda o texto de um campo: `both` (a página com as duas variantes) ou `solo` (só a que muda)."""
    if world == 'both':
        return 'args:type:both:{0}:{1}:{2}'.format(variant, field, event)
    return 'args:type:solo-{0}:{1}:{2}'.format(variant, field, event)


class PageParser(HTMLParser):
    """Lê a tela de comandos e guarda, por variante dos campos de argumentos, o que cada uma traz.

    Acompanha a abertura e o fechamento das `div`: uma variante é a `div` com a classe `form-task-args-lg` ou `form-task-args-md`, e
    tudo o que está dentro dela é dela (os coletores de `test_page.py` e de `test_page_script.py` não acompanham o aninhamento, e
    sobrescrevem o `id` repetido). `ids` guarda um item por elemento com `id` (o repetido aparece repetido); `first_by_id` guarda o
    PRIMEIRO elemento da página com cada `id`, que é o que o navegador resolve num `for` ou num `aria-describedby`.
    """
    VARIANT_CLASSES = {'form-task-args-lg': 'wide', 'form-task-args-md': 'narrow'}

    def __init__(self):
        super().__init__()
        self.ids = []
        self.first_by_id = {}
        self.variants = {}
        self.stylesheets = []
        self.form_inputs = []
        self._stack = []
        self._in_form = False

    def _current_variant(self):
        for entry in reversed(self._stack):
            if entry['variant']:
                return entry['variant']
        return None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set((attributes.get('class') or '').split())
        variant = self._current_variant()
        entry = None
        if tag == 'div':
            own = next((name for css, name in self.VARIANT_CLASSES.items() if css in classes), None)
            variant = own or variant
            if own:
                self.variants.setdefault(own, {'inputs': [], 'labels': [], 'addons': []})
            entry = {'variant': own, 'addon': None}
            self._stack.append(entry)
        record = {'tag': tag, 'variant': variant, 'attrs': attributes, 'text': '', 'addon': False}
        if tag == 'div' and 'input-group-text' in classes and variant:
            record['addon'] = True
            entry['addon'] = record
            self.variants[variant]['addons'].append(record)
        if attributes.get('id'):
            self.ids.append(attributes['id'])
            self.first_by_id.setdefault(attributes['id'], record)
        if tag == 'input':
            if variant:
                self.variants[variant]['inputs'].append(attributes)
            if self._in_form:
                self.form_inputs.append(attributes)
        elif tag == 'label' and variant:
            self.variants[variant]['labels'].append(attributes)
        elif tag == 'link' and attributes.get('rel') == 'stylesheet':
            self.stylesheets.append(attributes.get('href'))
        elif tag == 'form' and attributes.get('id') == 'taskform':
            self._in_form = True

    def handle_data(self, data):
        for entry in reversed(self._stack):
            if entry['addon'] is not None:
                entry['addon']['text'] += data
                return

    def handle_endtag(self, tag):
        if tag == 'div' and self._stack:
            self._stack.pop()
        elif tag == 'form':
            self._in_form = False


class ArgsFieldsScenarioTest(ScenarioTestCase):
    """O script da tela com os campos de argumentos das duas variantes (AC-9019-2 a -5, -8, -9 e -10)."""

    def typed(self):
        return self.constants['typedText']

    def refusal_message(self, kind, field):
        return self.constants['refusals'][kind][field][0]

    def assertTyped(self, state, variant, field, text, world, msg):
        """O campo do formulário que segue com o comando tem o texto, e o do outro argumento não mudou."""
        self.assertEqual(state['uncaught'], [], msg=msg)
        self.assertEqual(state['hiddenFields'][field], text, msg='the form field of {0} does not hold the text. {1}'.format(field, msg))
        self.assertEqual(state['hiddenFields'][other(field)], '',
                         msg='the form field of {0} changed with the text of {1}. {2}'.format(other(field), field, msg))
        self.assertIsNotNone(state['argumentFields'][variant], msg='precondition: the page has the {0} variant. {1}'.format(variant, msg))

    # ---------------------------------------------------------------- AC-9019-3
    def test_the_wide_variant_hands_its_text_to_the_form_field_when_the_field_changes(self):
        """AC-9019-3 (controle): o texto de um campo da variante larga vai ao campo do formulário quando o campo muda, e o vazio o esvazia."""
        for world, field in (('both', 'args'), ('both', 'kwargs'), ('solo', 'args'), ('solo', 'kwargs')):
            name = typing_name(world, 'wide', field, 'change')
            with self.subTest(scenario=name):
                found = self.scenario(name)
                self.assertTyped(found['afterText'], 'wide', field, self.typed(), world, name)
                self.assertTyped(found['afterEmpty'], 'wide', field, '', world, name)

    def test_every_other_field_and_event_hands_its_text_to_the_form_field(self):
        """AC-9019-3: o texto de um campo de argumentos de qualquer variante vai ao campo do formulário a cada alteração (`input` e `change`), e o vazio o esvazia, com as duas variantes ou com uma só."""
        for world in ('both', 'solo'):
            for variant in VARIANTS:
                for field in FIELDS:
                    for event in EVENTS:
                        if variant == 'wide' and event == 'change':
                            continue  # o controle, no teste acima
                        name = typing_name(world, variant, field, event)
                        with self.subTest(scenario=name):
                            found = self.scenario(name)
                            self.assertTyped(found['afterText'], variant, field, self.typed(), world, name)
                            self.assertTyped(found['afterEmpty'], variant, field, '', world, name)

    # ---------------------------------------------------------------- AC-9019-4
    def test_the_text_of_a_field_reaches_the_field_of_the_same_argument_in_the_other_variant(self):
        """AC-9019-4: o texto de um campo de argumentos passa ao campo do mesmo argumento da outra variante (e o vazio o esvazia), sem mudar o campo do outro argumento; o campo de origem segue com o texto digitado."""
        for variant in VARIANTS:
            for field in FIELDS:
                for event in EVENTS:
                    name = 'args:type:both:{0}:{1}:{2}'.format(variant, field, event)
                    with self.subTest(scenario=name):
                        found = self.scenario(name)
                        after_text = found['afterText']['argumentFields']
                        self.assertEqual(after_text[variant][field], self.typed(), msg='the field typed in lost its own text. ' + name)
                        self.assertEqual(after_text[other_variant(variant)][field], self.typed(),
                                         msg='the same argument of the other variant did not receive the text. ' + name)
                        self.assertEqual(after_text[other_variant(variant)][other(field)], '',
                                         msg='the other argument of the other variant changed. ' + name)
                        self.assertEqual(after_text[variant][other(field)], '', msg='the other argument of the same variant changed. ' + name)
                        self.assertEqual(found['afterEmpty']['argumentFields'][other_variant(variant)][field], '',
                                         msg='erasing the text did not erase it in the other variant. ' + name)
                        self.assertEqual(found['afterText']['uncaught'], [])

    def test_the_mirror_writes_only_to_the_other_variant_and_never_back_to_the_field_being_typed_in(self):
        """Robustez do espelho do texto (sem ID de critério): o script grava o texto, por `.val()`, só no campo do mesmo argumento da outra variante; o campo em que o usuário digita não recebe escrita do script (regravá-lo durante uma composição de IME a quebra), nem o campo do outro argumento de nenhuma variante."""
        for variant in VARIANTS:
            for field in FIELDS:
                for event in EVENTS:
                    name = 'args:type:both:{0}:{1}:{2}'.format(variant, field, event)
                    with self.subTest(scenario=name):
                        found = self.scenario(name)
                        mirrored = other_variant(variant)
                        # a precondição: o espelho gravou no campo da outra variante, uma vez a cada alteração (senão «nenhuma escrita
                        # de volta» valeria à toa)
                        self.assertEqual(found['afterText']['argumentWrites'][mirrored][field], [self.typed()],
                                         msg='precondition: the mirror did not write the text once. ' + name)
                        self.assertEqual(found['afterEmpty']['argumentWrites'][mirrored][field], [self.typed(), ''],
                                         msg='precondition: the mirror did not write the erasing once. ' + name)
                        for phase in ('afterText', 'afterEmpty'):
                            writes = found[phase]['argumentWrites']
                            self.assertEqual(writes[variant][field], [],
                                             msg='the script wrote back to the field being typed in. {0} {1}'.format(name, phase))
                            self.assertEqual(writes[variant][other(field)], [],
                                             msg='the script wrote to the other argument of the same variant. {0} {1}'.format(name, phase))
                            self.assertEqual(writes[mirrored][other(field)], [],
                                             msg='the script wrote to the other argument of the other variant. {0} {1}'.format(name, phase))

    # ---------------------------------------------------------------- AC-9019-2
    def assertRefused(self, state, kind, variants, msg):
        """A mensagem e a marca de erro do campo recusado ao lado do campo de cada variante; nenhuma ao lado do campo não recusado."""
        self.assertEqual(state['uncaught'], [], msg=msg)
        for field in FIELDS:
            refused = field in self.constants['refusals'][kind]
            expected = [self.refusal_message(kind, field)] if refused else []
            if 'wide' in variants:
                self.assertEqual(state['fieldErrors'][field], expected, msg='wide message of {0}. {1}'.format(field, msg))
                self.assertEqual(state['invalidFields'][field], refused, msg='wide mark of {0}. {1}'.format(field, msg))
            if 'narrow' in variants:
                self.assertEqual(state['fieldErrorsNarrow'][field], expected, msg='narrow message of {0}. {1}'.format(field, msg))
                self.assertEqual(state['invalidFieldsNarrow'][field], refused, msg='narrow mark of {0}. {1}'.format(field, msg))

    def test_a_400_puts_the_message_and_the_mark_next_to_the_refused_field_in_both_variants(self):
        """AC-9019-2: o 400 com o erro de `args`, de `kwargs` ou dos dois põe a mensagem e a marca ao lado do campo de cada variante do argumento recusado, e de nenhum outro."""
        for kind in ('args', 'kwargs', 'all'):
            name = 'errors:400:both:' + kind
            with self.subTest(scenario=name):
                state = self.scenario(name)['after']
                self.assertRefused(state, kind, VARIANTS, name)
                # o erro do campo do comando segue ao lado dele, e só quando ele foi recusado
                self.assertEqual(state['fieldErrors']['app_command'], [self.refusal_message('all', 'app_command')] if kind == 'all' else [])

    def test_a_page_with_only_the_narrow_variant_shows_the_error_next_to_it(self):
        """AC-9019-2: numa página com só a variante estreita (um host de template próprio), o erro do 400 aparece ao lado dos campos que ela tem."""
        state = self.scenario('errors:400:solo-narrow:all')['after']
        self.assertIsNone(state['argumentFields']['wide'], msg='precondition: the page has no wide variant')
        self.assertRefused(state, 'all', ('narrow',), 'solo-narrow')

    def test_a_page_with_only_the_wide_variant_shows_the_error_next_to_it(self):
        """AC-9019-2 (controle): numa página com só a variante larga (o template de hoje de um host), o erro do 400 aparece ao lado dos campos que ela tem."""
        state = self.scenario('errors:400:solo-wide:all')['after']
        self.assertIsNone(state['argumentFields']['narrow'], msg='precondition: the page has no narrow variant')
        self.assertRefused(state, 'all', ('wide',), 'solo-wide')

    def test_a_400_with_fields_the_page_does_not_map_shows_nothing_for_them_and_raises_no_error(self):
        """Robustez do erro do 400 (sem ID de critério): o 400 com um campo que a tela não mapeia (`ghost`) ou cujo nome existe em todo objeto (`constructor`, `__proto__`) não lança erro de script, não põe mensagem ao lado de campo nenhum por ele, solta a tela e ainda mostra o erro do campo conhecido que vem depois deles, nas duas variantes."""
        state = self.scenario('errors:400:both:unknown')['after']
        # a precondição: o campo conhecido (`args`), que vem DEPOIS dos três que a tela não mapeia, aparece nas duas variantes (um erro de
        # script no laço o esconderia), e `uncaught` vem vazio
        self.assertRefused(state, 'unknown', VARIANTS, 'unknown')
        self.assertEqual(state['feedbackAttached'], 2, msg='a message was attached for a field the page does not map')
        self.assertEqual(state['fieldErrors']['app_command'], [], msg='a message was put next to the command field')
        self.assertFalse(state['execute']['disabled'], msg='the screen was not released after the refusal')

    # ---------------------------------------------------------------- AC-9019-5 e AC-9019-9
    def test_the_next_answer_leaves_only_its_own_errors_in_every_variant_of_the_arguments(self):
        """AC-9019-5: o fim do envio (resposta aceita, falha de rede ou outra recusa) deixa em cada variante dos argumentos só a mensagem e a marca dessa resposta."""
        for second, kept in (('accepted', 0), ('network', 0), ('second', 1)):
            name = 'errors:clean:all:' + second
            with self.subTest(scenario=name):
                found = self.scenario(name)
                # a precondição: depois da 1ª recusa, a mensagem e a marca estão nas duas variantes (senão «nenhuma depois» vale à toa)
                self.assertRefused(found['afterFirst'], 'all', VARIANTS, 'first refusal of ' + name)
                after = found['afterSecond']
                self.assertEqual(after['uncaught'], [])
                for field in FIELDS:
                    self.assertEqual(after['fieldErrors'][field], [], msg='wide message of {0} was left on the page. {1}'.format(field, name))
                    self.assertEqual(after['fieldErrorsNarrow'][field], [], msg='narrow message of {0} was left on the page. {1}'.format(field, name))
                    self.assertFalse(after['invalidFields'][field], msg='wide mark of {0} was left. {1}'.format(field, name))
                    self.assertFalse(after['invalidFieldsNarrow'][field], msg='narrow mark of {0} was left. {1}'.format(field, name))
                self.assertEqual(after['feedbackAttached'], kept, msg='messages left attached to the page. ' + name)

    def test_the_next_answer_leaves_only_its_own_error_next_to_the_command_field(self):
        """AC-9019-9 (controle): o fim do envio (resposta aceita, falha de rede, outra recusa do comando ou recusa de outro campo) deixa ao lado do campo do comando só a mensagem e a marca dessa resposta."""
        first_message = self.refusal_message('all', 'app_command')
        second_message = self.refusal_message('second', 'app_command')
        for first, second, expected in (('all', 'accepted', []), ('all', 'network', []), ('all', 'second', [second_message]),
                                        ('command', 'accepted', []), ('command', 'network', []), ('command', 'args', [])):
            name = 'errors:clean:{0}:{1}'.format(first, second)
            with self.subTest(scenario=name):
                found = self.scenario(name)
                self.assertEqual(found['afterFirst']['fieldErrors']['app_command'], [first_message], msg='precondition: ' + name)
                self.assertTrue(found['afterFirst']['invalidFields']['app_command'], msg='precondition: ' + name)
                after = found['afterSecond']
                self.assertEqual(after['uncaught'], [])
                self.assertEqual(after['fieldErrors']['app_command'], expected, msg='command message after the next answer. ' + name)
                self.assertEqual(after['invalidFields']['app_command'], bool(expected), msg='command mark after the next answer. ' + name)

    # ---------------------------------------------------------------- AC-9019-10
    def test_the_message_of_the_wide_variant_and_of_the_command_field_enters_as_text(self):
        """AC-9019-10 (controle): a mensagem de erro do campo do comando e dos argumentos da variante larga entra na página como texto, com o que o servidor ecoou (`<b>…</b>`) sem virar marcação."""
        state = self.scenario('errors:400:both:echo')['after']
        echoed = self.constants['echoedText']
        self.assertEqual(state['uncaught'], [])
        for field in ('app_command', 'args', 'kwargs'):
            with self.subTest(field=field):
                self.assertEqual(state['fieldErrors'][field], [echoed], msg='the echoed text did not arrive as it came')
                self.assertNotIn('string', state['fieldAppended'][field], msg='a string (markup) was handed to the parent of {0}'.format(field))
        self.assertEqual(state['markupSinks'], [], msg='the script handed data to jQuery as markup')

    def test_the_message_of_the_narrow_variant_enters_as_text(self):
        """AC-9019-10: a mensagem de erro dos argumentos da variante estreita entra na página como texto, com o que o servidor ecoou (`<b>…</b>`) sem virar marcação."""
        state = self.scenario('errors:400:both:echo')['after']
        echoed = self.constants['echoedText']
        self.assertEqual(state['uncaught'], [])
        for field, key in (('args', 'argsNarrow'), ('kwargs', 'kwargsNarrow')):
            with self.subTest(field=field):
                self.assertEqual(state['fieldErrorsNarrow'][field], [echoed], msg='the echoed text did not arrive as it came')
                self.assertIn('element', state['fieldAppended'][key], msg='nothing was appended next to the narrow field of {0}'.format(field))
                self.assertNotIn('string', state['fieldAppended'][key], msg='a string (markup) was handed to the parent of {0}'.format(field))
        self.assertEqual(state['markupSinks'], [], msg='the script handed data to jQuery as markup')

    # ---------------------------------------------------------------- AC-9019-8
    def test_a_page_with_only_one_variant_raises_no_script_error(self):
        """AC-9019-8 (controle): numa página com só uma das variantes, digitar, receber o 400 e receber a resposta aceita não lançam erro de script nem escrevem no console."""
        for variant in VARIANTS:
            name = 'host:' + variant
            with self.subTest(scenario=name):
                found = self.scenario(name)
                for phase in ('afterTyping', 'afterRefusal', 'afterAccepted'):
                    state = found[phase]
                    self.assertIsNone(state['argumentFields'][other_variant(variant)], msg='precondition: the page has only the {0} variant'.format(variant))
                    self.assertEqual(state['uncaught'], [], msg='script error. {0} {1}'.format(name, phase))
                    self.assertEqual(state['consoleErrors'], [], msg='console error. {0} {1}'.format(name, phase))


class ArgsFieldsPageTest(TestCase):
    """A tela renderizada: os `id`, as duas variantes e a folha de estilos, o rótulo e a descrição de cada campo (AC-9019-6, -7 e -11)."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user()

    def setUp(self):
        self.client.force_login(self.user)

    def page(self, commands_list=False):
        original = app_settings.COMMANDS_ASYNC_LIST
        app_settings.COMMANDS_ASYNC_LIST = commands_list
        self.addCleanup(setattr, app_settings, 'COMMANDS_ASYNC_LIST', original)
        parser = PageParser()
        parser.feed(self.client.get(INDEX_URL).content.decode('utf-8'))
        return parser

    def test_the_page_repeats_the_id_of_no_element(self):
        """AC-9019-6: a tela não repete o `id` de nenhum elemento, com a lista de comandos ligada ou desligada."""
        for commands_list in (False, True):
            with self.subTest(commands_list=commands_list):
                parser = self.page(commands_list)
                # a precondição: os 4 campos de argumentos das duas variantes estão na página (o teste não passa à toa)
                self.assertEqual({name: len(parser.variants[name]['inputs']) for name in VARIANTS}, {'wide': 2, 'narrow': 2})
                repeated = {name: count for name, count in Counter(parser.ids).items() if count > 1}
                self.assertEqual(repeated, {}, msg='ids repeated in the page')

    def test_the_page_brings_both_variants_and_the_stylesheet_that_hides_one_of_them(self):
        """AC-9019-7 (controle): a tela traz as duas variantes dos campos de argumentos e a folha de estilos que esconde a estreita a partir de 769 px e a larga até 768 px."""
        parser = self.page()
        self.assertEqual(sorted(parser.variants), ['narrow', 'wide'], msg='the page lost a variant of the argument fields')
        linked = [href for href in parser.stylesheets if href and href.endswith('cmdasync/styles.css')]
        self.assertEqual(len(linked), 1, msg='the page does not link the stylesheet of the package exactly once')
        css = (STATIC_CMDASYNC / 'styles.css').read_text(encoding='utf-8')
        self.assertRegex(css, r'@media screen and \(max-width:\s*768px\)', msg='the rule for the screens up to 768 px is gone')
        self.assertRegex(css, r'@media screen and \(min-width:\s*769px\)', msg='the rule for the screens above 768 px is gone')

    def test_each_variant_ties_its_label_and_its_description_to_its_own_field(self):
        """AC-9019-11: em cada variante dos campos de argumentos, o rótulo aponta para o campo e o campo aponta para o texto `args` ou `kwargs` da mesma variante."""
        parser = self.page()
        for variant in VARIANTS:
            data = parser.variants[variant]
            self.assertEqual(len(data['inputs']), 2, msg='precondition: the {0} variant has the two fields'.format(variant))
            for field in data['inputs']:
                with self.subTest(variant=variant, field=field.get('id')):
                    # o rótulo: o `for` resolve (o primeiro elemento da página com esse id, como no navegador) ao campo desta variante
                    tied = [label for label in data['labels'] if parser.first_by_id.get(label.get('for'), {}).get('attrs') is field]
                    self.assertEqual(len(tied), 1, msg='the label of this variant does not resolve to its own field')
                    # a descrição: o `aria-describedby` resolve ao texto desta variante, `args` ou `kwargs` conforme o campo
                    tokens = (field.get('aria-describedby') or '').split()
                    self.assertEqual(len(tokens), 1, msg='the field does not name its description')
                    described = parser.first_by_id.get(tokens[0])
                    self.assertIsNotNone(described, msg='the description does not exist in the page')
                    self.assertEqual(described['variant'], variant, msg='the description resolves to the other variant')
                    self.assertTrue(described['addon'], msg='the description is not the text of the field')
                    self.assertEqual(described['text'].strip(), 'kwargs' if 'kwargs' in field['id'] else 'args')


class ArgsFieldsDoubleAssumptionsTest(TestCase):
    """O que as regras novas do dublê de node (#9019) supõem, conferido contra o código e os templates reais."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user()

    def setUp(self):
        self.client.force_login(self.user)

    @staticmethod
    def jquery():
        return JQUERY.read_text(encoding='utf-8')

    @staticmethod
    def page_script_without_comments():
        source = PAGE_SCRIPT.read_text(encoding='utf-8')
        source = re.sub(r'/\*.*?\*/', '', source, flags=re.S)
        return re.sub(r'//[^\n]*', '', source)

    def test_the_vendored_jquery_resolves_an_id_to_one_element(self):
        # O dublê tem UM nó por `id` (world.js, `registry`): o `$("#id")` do jQuery entregue casa o `rquickExpr` e resolve por
        # `getElementById`, que devolve só o primeiro elemento da árvore com aquele id; é por isso que a variante estreita precisa
        # do id próprio. Se a versão do jQuery mudar, este teste obriga a conferir de novo (#9019).
        source = self.jquery()
        self.assertIn(r'^(?:\s*(<[\w\W]+>)[^>]*|#([\w-]+))$', source, msg='the quick expression of the vendored jQuery changed')
        self.assertRegex(source, r'getElementById\(\w+\[2\]\)', msg='the vendored jQuery no longer resolves an id by getElementById')

    def test_the_double_removes_an_element_from_its_parent_like_the_vendored_jquery(self):
        # O `remove()` do dublê (world.js) tira o elemento do pai: o do jQuery entregue chama a função que faz
        # `parentNode.removeChild` (jquery-3.7.1.min.js). O nome da função é minificado, então ele é lido do `remove:function`.
        source = self.jquery()
        found = re.search(r'remove:function\((\w+)\)\{return ([\w$]+)\(this,\1\)\}', source)
        self.assertIsNotNone(found, msg='the remove of the vendored jQuery is no longer `remove: function(e) { return helper(this, e) }`')
        helper = found.group(2)
        self.assertRegex(source, r'function ' + re.escape(helper) + r'\(\w+,\w+,\w+\)\{[^\n]*?\.parentNode\.removeChild\(',
                         msg='the helper of remove no longer removes the element from its parent')

    def test_the_double_moves_an_element_appended_twice_like_the_vendored_jquery(self):
        # O `append` do dublê MOVE o elemento já anexado (a regra do DOM): o do jQuery entregue o anexa por `appendChild`, que o
        # move, e só clona para os outros alvos quando o conjunto tem vários (o dublê não o modela: o script anexa a um pai por vez).
        self.assertRegex(self.jquery(), r'append:function\(\)\{return [\w$]+\(this,arguments,function\(\w+\)\{[^}]*\.appendChild\(\w+\)\}\)\}',
                         msg='the append of the vendored jQuery no longer attaches by appendChild')

    def test_the_double_leaves_val_without_an_event_like_the_vendored_jquery(self):
        # O `.val(x)` do dublê só grava o valor: o do jQuery entregue grava `this.value` e não dispara evento (o DOM também não: atribuir
        # `value` não dispara `input` nem `change`), então o espelho do texto entre as duas variantes não cai em laço (#9019).
        source = self.jquery()
        start = source.index('val:function(n){')
        setter = source[start:source.index('):t?(', start)]  # o setter vai até o `:` do ternário que abre o getter
        self.assertIn('this.value=', setter, msg='the setter of val no longer writes the value')
        self.assertNotIn('trigger', setter, msg='the setter of val now triggers an event')

    def test_the_page_script_never_triggers_an_event_to_mirror_the_text(self):
        # Sem ID (a guarda do mecanismo do espelho, no molde do `DomSinkTest`): o texto passa de um campo ao outro só por `.val()`; um
        # `.trigger('input')` entre os dois campos entraria em recursão no navegador (A dispara B, B dispara A), e o dublê não o veria.
        source = self.page_script_without_comments()
        self.assertNotRegex(source, r'\.trigger\(|\.triggerHandler\(|dispatchEvent', msg='the script triggers an event on a field')

    def test_the_ids_of_the_double_exist_in_the_real_templates_each_inside_its_variant(self):
        # O dublê (world.js) registra os campos por `id`: a larga com os de sempre, a estreita com o sufixo `-md`. Cada um tem de
        # existir na página real, dentro da `div` da sua variante; senão os testes de node provam campos que a tela não tem (#9019).
        parser = PageParser()
        parser.feed(self.client.get(INDEX_URL).content.decode('utf-8'))
        selectors = load_scenarios()['constants']['argsSelectors']
        for variant in VARIANTS:
            with self.subTest(variant=variant):
                expected = {field: selectors[variant][field].lstrip('#') for field in FIELDS}
                found = {field.get('id') for field in parser.variants[variant]['inputs']}
                self.assertEqual(found, set(expected.values()), msg='the ids of the {0} variant in the page differ from the double'.format(variant))

    def test_the_form_fields_the_script_fills_are_hidden_inputs_of_the_form_of_the_page(self):
        # O que o `change` preenche (`#id_args` e `#id_kwargs`, world.js `hiddenFields`) é o que vai com o comando: os campos
        # `HiddenInput` do `TaskForm` (forms.py:12-18), dentro do `form#taskform` da página.
        parser = PageParser()
        parser.feed(self.client.get(INDEX_URL).content.decode('utf-8'))
        selectors = load_scenarios()['constants']['hiddenSelectors']
        in_form = {field.get('id'): field for field in parser.form_inputs}
        for name in FIELDS:
            with self.subTest(field=name):
                found = in_form.get(selectors[name].lstrip('#'))
                self.assertIsNotNone(found, msg='the form of the page has no field {0}'.format(selectors[name]))
                self.assertEqual(found.get('type'), 'hidden', msg='the field {0} is not a hidden input'.format(selectors[name]))
