# coding=utf-8
"""Shared helpers for the suite.

Everything the app talks to outside the process -- the broker (``apply_async``),
the result backend (``AsyncResult``) and worker inspection (``control.inspect``)
-- is replaced here, so no test needs Redis, a worker, or a configured Celery app.
"""
import json
import socket
import threading
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

    A conferência do broker antes do disparo (``precheck_before_launch``, #8944) é trocada: o ``celery_app`` do teste é o
    padrão, ou um ``MagicMock`` que o AC-8864-7 exige intocado, e nenhum disparo daqui toca um broker.
    """
    with mock.patch('commands_async.views.precheck_before_launch'), \
            mock.patch('commands_async.views.command_exec') as task:
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


class SilentSentinel(object):
    """Um endereço local que ACEITA a conexão, grava o que recebe e NUNCA responde (#8973).

    É o que um sentinela (ou um Redis) parado/sem resposta faz ao cliente: o `connect` do kernel passa, o cliente manda o
    `HELLO` e espera. Medido contra o container pausado da pilha Sentinel do compose, os dois dão o mesmo tempo da pré-verificação
    do broker (15,3 a 15,6 s com o código anterior ao #8973, 4,0 a 4,4 s com o do #8973). Nada sai da máquina: o endereço é `127.0.0.1`.

    `received` junta os bytes de todas as conexões (a senha do `sentinel_kwargs` do host chega aqui no `HELLO 3 AUTH <usuário>
    <senha>` do redis-py 8). O teste o fecha no `addCleanup(sentinel.close)`.
    """

    def __init__(self):
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(32)
        self.port = self.listener.getsockname()[1]
        self.received = b''
        self.connections = []
        self.lock = threading.Lock()
        threading.Thread(target=self.serve, daemon=True).start()

    @property
    def address(self):
        return '127.0.0.1', self.port

    def serve(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return  # o listener foi fechado
            with self.lock:
                self.connections.append(connection)
            threading.Thread(target=self.read, args=(connection,), daemon=True).start()

    def read(self, connection):
        while True:
            try:
                data = connection.recv(4096)
            except OSError:
                return
            if not data:
                return
            with self.lock:
                self.received += data

    def close(self):
        self.listener.close()
        with self.lock:
            for connection in self.connections:
                try:
                    connection.close()
                except OSError:
                    pass


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
