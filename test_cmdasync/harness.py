# coding=utf-8
"""Shared helpers for the suite.

Everything the app talks to outside the process -- the broker (``apply_async``),
the result backend (``AsyncResult``) and worker inspection (``control.inspect``)
-- is replaced here, so no test needs Redis, a worker, or a configured Celery app.
"""
import json
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType

# jQuery sets this header on every XHR (see the bundled jquery-3.2.1.min.js), and
# it is what the app reads to tell an AJAX submit from a plain form post.
AJAX = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}

INDEX_URL = '/admin/command/async/'
WORKERS_URL = '/admin/command/async/workers/status/'


def status_url(task_id):
    return '/admin/command/async/status/{0}'.format(task_id)


def launch(client, task_id):
    """Submit the form as ``client`` with the broker handing back ``task_id``; return the response.

    The task is launched the way the page does it (XHR post), so what the view records about its owner is
    recorded as in use. The precondition is checked here: the launch answers the id it was given.
    """
    with mock.patch('commands_async.views.command_exec') as task:
        task.apply_async.return_value = queued_task(task_id)
        response = client.post(INDEX_URL, {'app_command': 'test_cmdasync.cmdasync_echo', 'args': '', 'kwargs': ''},
                               **AJAX)
    assert response.status_code == 200, 'precondition: the launch of {0} must be accepted'.format(task_id)
    assert json.loads(response.content)['task']['id'] == task_id, 'precondition: the launch answers the task id'
    return response


def reset_workers_window(module=None):
    """Return the window of the workers route (#8879) to "no query kept", in ``module`` (default: ``commands_async.views``).

    The window is state of the module, shared by every test of the process, so each test that asks the workers route
    zeroes it first. ``module`` is a parameter because ``test_settings`` runs a copy of ``views.py`` under another name
    (``views_read_afresh``), with a window of its own.
    """
    if module is None:
        from commands_async import views as module
    module.workers_window.update(at=None, kind=None, names=())


def create_user(username='runner', password='secret-for-tests'):
    return User.objects.create_user(username=username, password=password)


def grant(user, codename, name=None):
    """Give ``user`` a brand new permission and return its ``app_label.codename``.

    The permission is created against the ``auth.User`` content type because the
    app declares no models of its own to hang one on.
    """
    permission = Permission.objects.create(
        codename=codename,
        name=name or codename,
        content_type=ContentType.objects.get_for_model(User),
    )
    user.user_permissions.add(permission)
    # Permissions are cached per user instance on first check.
    user = User.objects.get(pk=user.pk)
    return 'auth.{0}'.format(codename)


def queued_task(task_id='queued-task-id'):
    """Stand-in for the ``AsyncResult`` that ``apply_async`` hands back."""
    task = mock.MagicMock()
    task.id = task_id
    return task


def async_result(task_id='task-id', ready=True, failed=False,
                 traceback='Traceback (most recent call last): ...',
                 result='command output'):
    """Stand-in for a result read back from the backend."""
    stub = mock.MagicMock()
    stub.id = task_id
    stub.ready.return_value = ready
    stub.failed.return_value = failed
    stub.traceback = traceback
    stub.result = result
    return stub
