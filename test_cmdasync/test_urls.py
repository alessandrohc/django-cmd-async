# coding=utf-8
"""Route names, reverse targets and the login gate on each of the three views.

The names and the ``command-async`` namespace are load-bearing outside this
package: the host project reaches the page through
``reverse("command-async:index")``, so a rename here breaks it silently.
"""
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from test_cmdasync.harness import (INDEX_URL, WORKERS_URL, async_result,
                                   create_user, status_url)


class ReverseTest(TestCase):
    """Each route resolves under the namespace the host project expects."""

    def test_index_reverses_under_the_command_async_namespace(self):
        self.assertEqual(reverse('command-async:index'), INDEX_URL)

    def test_workers_reverses_under_the_command_async_namespace(self):
        self.assertEqual(reverse('command-async:workers'), WORKERS_URL)

    def test_status_reverses_with_the_task_id_in_the_path(self):
        self.assertEqual(reverse('command-async:status', kwargs={'task_id': 'abc-123'}),
                         status_url('abc-123'))

    def test_status_reverses_with_an_empty_task_id(self):
        # The pattern accepts an empty id on purpose -- the JS builds the URL
        # before the task id is known and relies on it not 404ing.
        self.assertEqual(reverse('command-async:status', kwargs={'task_id': ''}),
                         status_url(''))


class LoginGateTest(TestCase):
    """Which routes demand a session, and where they send anonymous callers."""

    def test_anonymous_index_redirects_to_the_login_url(self):
        response = self.client.get(INDEX_URL)
        self.assertRedirects(response, '/login/?next={0}'.format(INDEX_URL),
                             target_status_code=200)

    def test_anonymous_workers_redirects_to_the_login_url(self):
        response = self.client.get(WORKERS_URL)
        self.assertRedirects(response, '/login/?next={0}'.format(WORKERS_URL),
                             target_status_code=200)

    @mock.patch('commands_async.views.AsyncResult')
    def test_status_is_reachable_without_a_session(self, async_result_class):
        # Deliberate: the polling endpoint is unauthenticated so the JS can keep
        # reading a task's output after the session expires. Asserted so the
        # asymmetry with the other two routes is a decision, not an accident.
        async_result_class.return_value = async_result()
        response = self.client.get(status_url('abc-123'))
        self.assertEqual(response.status_code, 200)


class NeverCacheTest(TestCase):
    """Every response has to be uncacheable -- the output is per task."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user()

    def setUp(self):
        self.client.force_login(self.user)

    def test_index_response_is_not_cacheable(self):
        response = self.client.get(INDEX_URL)
        self.assertIn('no-cache', response.headers['Cache-Control'])

    @mock.patch('commands_async.views.celery_app')
    def test_workers_response_is_not_cacheable(self, celery_app):
        celery_app.control.inspect.return_value.active.return_value = {}
        response = self.client.get(WORKERS_URL)
        self.assertIn('no-cache', response.headers['Cache-Control'])

    @mock.patch('commands_async.views.AsyncResult')
    def test_status_response_is_not_cacheable(self, async_result_class):
        async_result_class.return_value = async_result()
        response = self.client.get(status_url('abc-123'))
        self.assertIn('no-cache', response.headers['Cache-Control'])
