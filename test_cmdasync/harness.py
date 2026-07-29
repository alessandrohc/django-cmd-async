# coding=utf-8
"""Shared helpers for the suite.

Everything the app talks to outside the process -- the broker (``apply_async``),
the result backend (``AsyncResult``) and worker inspection (``control.inspect``)
-- is replaced here, so no test needs Redis, a worker, or a configured Celery app.
"""
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
