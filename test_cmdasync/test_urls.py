# coding=utf-8
"""Route names, reverse targets and the login gate on each of the three views.

The names and the ``command-async`` namespace are load-bearing outside this
package: the host project reaches the page through
``reverse("command-async:index")``, so a rename here breaks it silently.
"""
from unittest import mock

from django.test import TestCase
from django.urls import resolve, reverse

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
        """AC-8836-9 (controle): an empty task id still builds the status address."""
        # The pattern accepts an empty id on purpose -- the host project relies on it
        # not 404ing (its own tests send an empty id through this route).
        self.assertEqual(reverse('command-async:status', kwargs={'task_id': ''}),
                         status_url(''))

    def test_status_reverses_with_a_task_id_that_has_a_slash(self):
        """AC-8836-9 (controle): a task id with a slash still builds the status address, as it came."""
        self.assertEqual(reverse('command-async:status', kwargs={'task_id': 'a/b'}),
                         status_url('a/b'))


class ResolveTest(TestCase):
    """The status pattern keeps matching an empty id and one with a slash, handing the id over as it came."""

    def test_status_resolves_an_empty_task_id(self):
        """AC-8836-12 (controle): the address without an id resolves to the status route with an empty id."""
        match = resolve(status_url(''))
        self.assertEqual((match.namespace, match.url_name), ('command-async', 'status'))
        self.assertEqual(match.kwargs, {'task_id': ''})

    def test_status_resolves_a_task_id_with_a_slash(self):
        """AC-8836-12 (controle): an id with a slash resolves to the status route and reaches the view whole."""
        match = resolve(status_url('a/b'))
        self.assertEqual((match.namespace, match.url_name), ('command-async', 'status'))
        self.assertEqual(match.kwargs, {'task_id': 'a/b'})


class RoundTripTest(TestCase):
    """The address ``reverse`` builds is served by the status view, which gets the id as it came."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user()

    def setUp(self):
        self.client.force_login(self.user)

    def test_an_empty_task_id_and_one_with_a_slash_reach_the_view_as_they_came(self):
        """AC-8836-9, AC-8836-12 (controle): the built address is requested and the view receives the same id."""
        # The host project builds this URL with ids of both kinds (see ReverseTest), so the id has to arrive whole:
        # the view reads it from kwargs['task_id'] and hands it to AsyncResult (views.py, TaskFormStatus.get).
        for task_id in ('', 'a/b'):
            with self.subTest(task_id=task_id):
                with mock.patch('commands_async.views.AsyncResult') as async_result_class:
                    async_result_class.return_value = async_result()
                    url = reverse('command-async:status', kwargs={'task_id': task_id})
                    response = self.client.get(url)

                self.assertEqual(url, status_url(task_id))
                self.assertEqual(response.status_code, 200, msg='the built address must be served')
                async_result_class.assert_called_once()
                self.assertEqual(async_result_class.call_args.kwargs['id'], task_id)


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
    def test_anonymous_status_redirects_to_the_login_url(self, async_result_class):
        """AC-8836-1: the status route demands a session like the other two (it was open until #8836)."""
        # The route used to stay open "so the JS can keep reading after the session expires": it exposed
        # the output of any task to anyone. The bundled JS asks for a status only after the form post
        # (which needs a session) returned the task id.
        async_result_class.return_value = async_result()
        response = self.client.get(status_url('abc-123'))
        self.assertRedirects(response, '/login/?next={0}'.format(status_url('abc-123')),
                             fetch_redirect_response=False)


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
