# coding=utf-8
"""The page's asset and CSP posture, and the sinks that reach the DOM.

Three things are guarded here, all of them the kind that break silently:

* nothing executable is inline, and every ``<script>`` carries the nonce -- under
  a ``'strict-dynamic'`` policy a host allowlist buys nothing, so an external
  script without a nonce is refused exactly like an inline one;
* the values the page script needs arrive through ``data-*`` instead of being
  interpolated into JavaScript;
* server-provided text -- command output, tracebacks, worker names, validation
  messages -- is inserted as text, never parsed as HTML.
"""
import base64
import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings

from test_cmdasync.context_processors import NONCE
from test_cmdasync.harness import INDEX_URL, create_user

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / 'commands_async' / 'static' / 'cmdasync'
TEMPLATES_DIR = ROOT / 'commands_async' / 'templates' / 'cmdasync'

# The page scripts: everything that touches data coming back from the server.
PAGE_SCRIPTS = ('js/cmdhandler.js', 'js/celery.workers.status.js', 'js/cmdasync-page.js')

# Published by jquery.com for the 3.7.1 minified build. Pinning the digest is what
# makes "we upgraded jQuery" verifiable instead of a claim about a file name.
JQUERY_SRI = 'sha256-/JqT3SQfawRcv/BIHPThkBvs0OEvtFFmqPF/lYI/Cxo='

BOOTSTRAP_VERSION = '4.6.2'
JQUERY_VERSION = '3.7.1'


class ScriptCollector(HTMLParser):
    """Collects every ``<script>`` tag with its attributes and its inline body."""

    def __init__(self):
        super().__init__()
        self.scripts = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self._current = {'attrs': dict(attrs), 'body': ''}
            self.scripts.append(self._current)

    def handle_data(self, data):
        if self._current is not None:
            self._current['body'] += data

    def handle_endtag(self, tag):
        if tag == 'script':
            self._current = None


def scripts_of(html):
    collector = ScriptCollector()
    collector.feed(html)
    return collector.scripts


def sri_of(path):
    digest = hashlib.sha256(path.read_bytes()).digest()
    return 'sha256-' + base64.b64encode(digest).decode('ascii')


class InlineScriptTest(TestCase):
    """Nothing executable may live in the HTML."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user()

    def setUp(self):
        self.client.force_login(self.user)

    def rendered(self):
        return self.client.get(INDEX_URL).content.decode('utf-8')

    def test_no_script_tag_carries_an_inline_body(self):
        offenders = [script['attrs'].get('src', '<inline>')
                     for script in scripts_of(self.rendered())
                     if script['body'].strip()]
        self.assertEqual(offenders, [],
                         msg='inline script found; a nonce-only CSP refuses it')

    def test_every_script_is_loaded_from_a_file(self):
        for script in scripts_of(self.rendered()):
            with self.subTest(script=script['attrs']):
                self.assertIn('src', script['attrs'])

    def test_the_page_carries_no_inline_event_handlers(self):
        # onclick= and friends need 'unsafe-hashes' to survive; the page wires its
        # handlers from the external script instead.
        html = self.rendered()
        self.assertNotRegex(html, r'\son[a-z]+\s*=\s*["\']')


class NonceTest(TestCase):
    """Every script has to carry the nonce the host project exposes."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user()

    def setUp(self):
        self.client.force_login(self.user)

    def test_every_script_tag_stamps_the_context_nonce(self):
        scripts = scripts_of(self.client.get(INDEX_URL).content.decode('utf-8'))
        self.assertTrue(scripts, msg='no script tags at all -- the page is broken')
        for script in scripts:
            with self.subTest(src=script['attrs'].get('src')):
                self.assertEqual(script['attrs'].get('nonce'), NONCE)

    @override_settings(TEMPLATES=[{
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {'context_processors': [
            'django.template.context_processors.request',
            'django.contrib.auth.context_processors.auth',
            'django.contrib.messages.context_processors.messages',
        ]},
    }])
    def test_the_page_still_renders_where_no_nonce_is_exposed(self):
        # Standalone use, or a project with the CSP off: the attribute comes out
        # empty and the page must not blow up.
        response = self.client.get(INDEX_URL)
        self.assertEqual(response.status_code, 200)
        for script in scripts_of(response.content.decode('utf-8')):
            with self.subTest(src=script['attrs'].get('src')):
                self.assertEqual(script['attrs'].get('nonce'), '')


class DataAttributeContractTest(TestCase):
    """What the external script reads instead of interpolated template values."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user()

    def setUp(self):
        self.client.force_login(self.user)

    def rendered(self):
        return self.client.get(INDEX_URL).content.decode('utf-8')

    def test_the_page_publishes_every_value_the_script_needs(self):
        html = self.rendered()
        for attribute in ('data-commands-list', 'data-workers-status-check',
                          'data-login-url', 'data-redirect-field',
                          'data-index-url', 'data-running-text'):
            with self.subTest(attribute=attribute):
                self.assertIn(attribute, html)

    def test_the_index_url_and_redirect_field_are_the_real_ones(self):
        html = self.rendered()
        self.assertIn('data-index-url="{0}"'.format(INDEX_URL), html)
        self.assertIn('data-redirect-field="next"', html)

    def test_the_login_url_comes_from_the_app_settings(self):
        self.assertIn('data-login-url="/login/"', self.rendered())

    def test_the_flags_are_published_as_json_booleans(self):
        from commands_async import settings as app_settings

        html = self.rendered()
        self.assertIn('data-commands-list="false"', html)
        self.assertIn('data-workers-status-check="false"', html)

        with self.settings():  # keeps the module patch below scoped to this test
            original = app_settings.COMMANDS_ASYNC_LIST
            app_settings.COMMANDS_ASYNC_LIST = True
            try:
                self.assertIn('data-commands-list="true"', self.rendered())
            finally:
                app_settings.COMMANDS_ASYNC_LIST = original

    def test_the_running_message_is_translated_in_the_template(self):
        # The string stays a {% trans %} in the HTML -- moving it into the .js
        # would take it out of the catalog.
        self.assertIn('data-running-text="Running command..."', self.rendered())


class DomSinkTest(SimpleTestCase):
    """Server data must never be handed to jQuery as markup.

    ``$output.append(task.output)`` parsed a command's stdout as HTML, so any
    command printing untrusted data -- ``read_logs`` is the example in the
    project's own admin action -- ran it in the superuser's browser.
    """

    def test_no_page_script_writes_html(self):
        for name in PAGE_SCRIPTS:
            with self.subTest(script=name):
                source = (STATIC / name).read_text(encoding='utf-8')
                self.assertNotIn('.html(', source,
                                 msg='use .text() or a text node instead')

    def test_command_output_reaches_the_dom_as_a_text_node(self):
        source = (STATIC / 'js' / 'cmdhandler.js').read_text(encoding='utf-8')
        self.assertIn('createTextNode', source)
        self.assertNotIn('append(task.output)', source)
        self.assertNotIn('append(task.traceback)', source)

    def test_no_page_script_builds_markup_by_concatenation(self):
        # '<p class="...">' + value + '</p>' is the pattern that put worker names
        # and error messages into the DOM as HTML.
        pattern = re.compile(r"""['"]\s*<[a-z]+[^'"]*['"]\s*\+""", re.IGNORECASE)
        for name in PAGE_SCRIPTS:
            with self.subTest(script=name):
                source = (STATIC / name).read_text(encoding='utf-8')
                self.assertIsNone(pattern.search(source),
                                  msg='markup concatenated with a value')


class VendoredAssetTest(SimpleTestCase):
    """The bundled third-party assets are the releases we claim they are."""

    def test_jquery_matches_the_published_digest_for_its_release(self):
        path = STATIC / 'js' / 'jquery-{0}.min.js'.format(JQUERY_VERSION)
        self.assertTrue(path.exists(), msg='{0} is missing'.format(path.name))
        self.assertEqual(sri_of(path), JQUERY_SRI)

    def test_bootstrap_js_announces_the_expected_version(self):
        # The bundle, not bootstrap.min.js: it carries the Popper build Bootstrap
        # expects, which is one script tag (and one nonce) instead of two.
        path = STATIC / 'bootstrap-{0}'.format(BOOTSTRAP_VERSION) / 'js' / 'bootstrap.bundle.min.js'
        self.assertTrue(path.exists(), msg='{0} is missing'.format(path))
        self.assertIn('Bootstrap v{0}'.format(BOOTSTRAP_VERSION),
                      path.read_text(encoding='utf-8')[:400])

    def test_the_bootstrap_bundle_carries_popper(self):
        path = STATIC / 'bootstrap-{0}'.format(BOOTSTRAP_VERSION) / 'js' / 'bootstrap.bundle.min.js'
        self.assertIn('Popper', path.read_text(encoding='utf-8'),
                      msg='the bundle must include Popper, or dropdowns break')

    def test_bootstrap_css_announces_the_expected_version(self):
        path = STATIC / 'bootstrap-{0}'.format(BOOTSTRAP_VERSION) / 'css' / 'bootstrap.min.css'
        self.assertTrue(path.exists(), msg='{0} is missing'.format(path))
        self.assertIn('Bootstrap v{0}'.format(BOOTSTRAP_VERSION),
                      path.read_text(encoding='utf-8')[:400])

    def test_the_superseded_releases_are_gone(self):
        for stale in ('js/jquery-3.2.1.min.js', 'bootstrap-4.3.1'):
            with self.subTest(path=stale):
                self.assertFalse((STATIC / stale).exists(),
                                 msg='{0} still ships'.format(stale))

    def test_every_static_path_referenced_by_a_template_exists(self):
        # A renamed vendor directory leaves a 404 that no Python test would see.
        pattern = re.compile(r"""{%\s*static\s+["']([^"']+)["']\s*%}""")
        for template in sorted(TEMPLATES_DIR.glob('*.html')):
            source = template.read_text(encoding='utf-8')
            for reference in pattern.findall(source):
                with self.subTest(template=template.name, static=reference):
                    self.assertTrue((STATIC.parent / reference).exists(),
                                    msg='{0} references a missing {1}'.format(
                                        template.name, reference))
