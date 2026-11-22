# coding=utf-8
import collections
import hashlib
import threading
import time
from urllib.parse import urlsplit
from django.core.cache import caches
from django.core.exceptions import PermissionDenied
from django.shortcuts import resolve_url
from django.utils.cache import add_never_cache_headers
from django.utils.translation import gettext as _
from django.http import HttpResponseForbidden, JsonResponse
from django.views.generic import FormView, View
from celery.result import AsyncResult
from .forms import TaskForm
from django.core.management import get_commands
from commands_async.tasks import command_exec, logger
from celery import current_app as celery_app
from . import settings


def is_ajax(request):
    """Whether the request was issued by XHR.

    Stands in for ``HttpRequest.is_ajax()``, deprecated in Django 3.1 and removed
    in 4.0. The header is what jQuery sets on every XHR, and the bundled
    cmdhandler.js submits this form through jquery.form -- so the check is exact
    for this page, which is the only caller.
    """
    return request.headers.get('x-requested-with') == 'XMLHttpRequest'


def login_redirect(request):
    """Redirects to the login page with the same ``next`` that ``login_required`` builds.

    ``django.contrib.auth.views`` pulls the auth forms and models in at import time, which this module
    does not otherwise depend on, so it is imported here.
    """
    from django.contrib.auth.views import redirect_to_login

    path = request.build_absolute_uri()
    login_url = resolve_url(settings.COMMANDS_ASYNC_LOGIN_URL)
    # Same scheme and net location as the login page: ``next`` is just the path. Otherwise it is absolute.
    login_scheme, login_netloc = urlsplit(login_url)[:2]
    current_scheme, current_netloc = urlsplit(path)[:2]
    if (not login_scheme or login_scheme == current_scheme) and (
            not login_netloc or login_netloc == current_netloc):
        path = request.get_full_path()
    return redirect_to_login(path, login_url)


#: prefixo das chaves do registro do dono: o alias do registro pode ser compartilhado com o do host (o `plus_base` guarda o
#: dele sob `owner:`), e o prefixo próprio impede a colisão
OWNER_KEY_PREFIX = 'cmdasync:owner:'
#: tentativas de gravar o registro: o cache que descarta a gravação sem erro (`IGNORE_EXCEPTIONS` do Redis) só se descobre relendo.
#: Sem pausa entre elas, de propósito: a biblioteca não segura a thread do pedido
OWNER_SAVE_ATTEMPTS = 3

#: segundos que a consulta que o processo já respondeu vale (AC-8879-5, AC-8879-6)
WORKERS_WINDOW_SECONDS = 5
#: o relógio da janela: o teste o troca por um relógio que ele avança
clock = time.monotonic
#: a consulta guardada: `at` o instante em que ela começou, `kind` o tipo do resultado (`WORKERS_LISTED`, `WORKERS_NOBODY` ou
#: `WORKERS_FAILED`) e `names` os nomes (tupla); `at` vazio = nada guardado. Guarda o TIPO e não a frase já traduzida: a
#: frase se traduz na resposta, e o usuário seguinte, de outro idioma, não a recebe no idioma do primeiro. Só se lê e se grava
#: com `workers_lock` tomado. Os testes a zeram (`harness.reset_workers_window`)
workers_window = {'at': None, 'kind': None, 'names': ()}
workers_lock = threading.Lock()
WORKERS_LISTED, WORKERS_NOBODY, WORKERS_FAILED = 'listed', 'nobody', 'failed'

#: limites da pré-verificação do broker (AC-8879-7; o formulário de execução a usa também: AC-8944-1 e AC-8944-5). O Celery do projeto espera até 300 s, em até 100 tentativas, por um broker
#: que não responde (`plus_base/settings/publique.py:814` e `:818`) e a rota seguraria a thread do pedido esse tempo todo: aqui
#: a conexão e a leitura do socket têm 1 s, e a verificação toda 2 s por uma tentativa a mais. Medido no #8879 com `redis://`
#: de um host só: porta recusada 2,0 s e endereço sem rota 4,0 s; Sentinel e cluster não foram medidos. Mudar estes limites pede
#: refazer a medição do endereço sem rota (#8879, `evidence/T-2.md`: 4,0 s com o socket em 1,0 s): o teste automático só cobre a
#: porta recusada (~2 s) e não pegaria um endereço sem rota que passasse dos 5 s do AC-8879-7
BROKER_PRECHECK_SOCKET_TIMEOUT = 1.0
BROKER_PRECHECK_TIMEOUT = 2


def broker_precheck():
    """Confere que o broker aceita conexão, em tempo limitado; o que o kombu levantar sobe até o `except` da rota ou do formulário.

    Os timeouts vão no pedido da conexão, e não lidos do `celery_app.conf`: o Celery mescla as `broker_transport_options` do app
    com as passadas e o `connect_timeout` explícito vence o da configuração (`celery/app/base.py:1061-1066`). O `with` solta a
    conexão também quando a exceção sobe.
    """
    with celery_app.connection_for_write(
            transport_options={'socket_connect_timeout': BROKER_PRECHECK_SOCKET_TIMEOUT,
                               'socket_timeout': BROKER_PRECHECK_SOCKET_TIMEOUT},
            connect_timeout=BROKER_PRECHECK_SOCKET_TIMEOUT) as connection:
        connection.ensure_connection(max_retries=1, timeout=BROKER_PRECHECK_TIMEOUT)


def precheck_before_launch():
    """Confere o broker antes do disparo do formulário de execução (#8944), a não ser que o Celery rode em modo síncrono.

    Em modo síncrono (`task_always_eager`) o `apply_async` roda a tarefa no próprio processo, sem broker: a pré-verificação o
    faria falhar onde hoje funciona (medido: 0,08 s com o broker morto, contra 2,0 s e uma falha da pré-verificação). Fora
    dele, o `apply_async` espera o que a abertura da conexão deixar (mais de 100 s com o endereço sem rota; o `retry_policy`
    de publicação do Celery só limita as tentativas depois de aberta) e a pré-verificação responde em ~2 s com a conexão
    recusada e em ~4 s sem rota. O que ela levantar sobe até o `except` do formulário, que responde a mesma 400 de sempre.

    É uma função do módulo, e não a leitura da flag dentro do `form_valid`, para os testes a trocarem sem tocar o `celery_app`
    que um deles põe em `MagicMock` (a leitura da flag ali ficaria registrada nas chamadas do dublê).
    """
    if not celery_app.conf.task_always_eager:
        broker_precheck()


def is_task_id(task_id):
    """Se `task_id` pode ser o id de uma tarefa: texto não vazio.

    O id vem do endereço (`^status/(?P<task_id>.*)`), que entrega o vazio de propósito (AC-8836-9, AC-8836-12), e o backend
    de resultados do Celery recusa o id vazio com `ValueError` no primeiro `ready()` (`celery/backends/base.py:986`). É o
    mesmo critério do `owner_key` («nunca é dono») e da `TaskFormStatus.task_denial`: um só dono para «o que é um id de
    tarefa». O `isinstance` não sai: sem ele um `5` ou um `b'a'` passariam.
    """
    return isinstance(task_id, str) and bool(task_id)


def owner_key(user, task_id):
    """Chave do registro do dono: hash do `pk` do usuário e do id da tarefa, ou `None` quando não há como ser dono.

    O id vem do endereço (`^status/(?P<task_id>.*)`, que aceita o vazio e o com barra), então a chave é um hash com um
    separador entre o `pk` e o id: nunca depende do formato do id. Id vazio, que não é texto, ou usuário sem `pk`
    (o anônimo) nunca é dono.
    """
    pk = getattr(user, 'pk', None)
    if pk is None or not is_task_id(task_id):
        return None
    return OWNER_KEY_PREFIX + hashlib.sha256(('%s\n%s' % (pk, task_id)).encode('utf-8')).hexdigest()


def remember_task(user, task_id):
    """Registra que `user` disparou a tarefa `task_id`. Devolve se o registro ficou.

    Toda a gravação (a chave, o alias, o `set` e a releitura) fica no `try`: um alias que não existe levanta
    `InvalidCacheBackendError` e um erro aqui nunca pode derrubar o disparo (o usuário reenviaria o comando). A releitura
    pega o cache que descarta em silêncio. Se o registro não ficar, o erro vai ao log com o id da tarefa (o do Celery): o
    dono verá a tarefa como "não encontrada", mas ela roda.
    """
    key = owner_key(user, task_id)
    if key is None:
        return False
    for attempt in range(OWNER_SAVE_ATTEMPTS):
        try:
            cache = caches[settings.COMMANDS_ASYNC_OWNER_CACHE_ALIAS]
            cache.set(key, True, timeout=settings.COMMANDS_ASYNC_OWNER_TIMEOUT)
            if cache.get(key):
                return True
        except Exception:
            logger.exception('the owner record of task {0!s} could not be written'.format(task_id))
    logger.error('the owner record of task {0!s} was not kept after {1} attempts'.format(task_id, OWNER_SAVE_ATTEMPTS))
    return False


def owns_task(user, task_id):
    """Se `user` disparou a tarefa `task_id` (e o registro ainda vale). Falha fechada: a leitura que levanta ou volta vazia
    é "não é dono". O id pedido é texto do cliente e não vai ao log."""
    key = owner_key(user, task_id)
    if key is None:
        return False
    try:
        return bool(caches[settings.COMMANDS_ASYNC_OWNER_CACHE_ALIAS].get(key))
    except Exception:
        logger.exception('the owner record of a task could not be read')
        return False


class CommandPermissionMixin(object):
    """The optional permission to run commands, shared by the form and the status and workers routes."""

    command_permission_name = settings.COMMANDS_ASYNC_PERMISSION_NAME

    def has_permission(self):
        if bool(self.command_permission_name):
            has_perm = self.request.user.has_perm(self.command_permission_name)
        else:
            has_perm = True
        return has_perm


class CommandGateMixin(CommandPermissionMixin):
    """Login and permission for the routes that read or act on tasks, checked by the view itself.

    The routes used to stay open to anyone (the status) or to any logged user (the workers). The gate lives in
    ``dispatch``, and not in the URLconf: a host that mounts or subclasses the view keeps it. Every denial is a
    response the view returns (never a raised ``PermissionDenied``, which Django turns into the 403 outside this view),
    marked ``no-store`` here so that it does not depend on the ``never_cache`` of ``urls.py``, and it comes before
    anything the view does (a denied request never makes the workers view ask Celery). An XHR (what the bundled JS
    sends) gets a 403 in JSON, which its ``.fail`` handles; a 302 to the login page would be followed and read as an
    answer, so any other request is redirected, or answered the plain 403.

    The JSON of the denial is each view's own: ``forbidden_json`` has no default, so that a new view does not
    inherit the body of another. A view that uses this mixin must not be a subclass of ``TaskFormStatus`` or of
    ``TaskFormView``: the host project swaps the route of every view that is (``plus_base``, ``_is_route_of``).
    """

    #: marca como ``no-store`` também a resposta que passou do gate (atendida, negada por ``task_denial`` ou o 405 do método):
    #: a rota status a liga, para o cabeçalho não depender do ``never_cache`` da ``urls.py``
    never_cache_response = False

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            response = self.forbidden_json() if is_ajax(request) else login_redirect(request)
        elif not self.has_permission():
            response = self.forbidden_json() if is_ajax(request) else HttpResponseForbidden()
        else:
            response = self.task_denial(request, *args, **kwargs)
            if response is None:
                response = super().dispatch(request, *args, **kwargs)
            if self.never_cache_response:
                add_never_cache_headers(response)
            return response
        add_never_cache_headers(response)
        return response

    def forbidden_json(self):
        raise NotImplementedError('{0} has to answer its own denial in JSON'.format(type(self).__name__))

    def task_denial(self, request, *args, **kwargs):
        """Extension point after the login and the permission and before the method: ``None`` lets the request go on,
        a response answers it instead. The route that reads a task uses it to refuse who did not launch the task."""
        return None


class TaskFormView(CommandPermissionMixin, FormView):
    """ Task execution form """

    template_name = "cmdasync/index.html"

    form_class = TaskForm
    success_url = '.'

    commands_skip = settings.COMMANDS_ASYNC_COMMANDS_IGNORE

    def is_command_valid(self, command_name, app_name=None):
        """Whether the command is allowed by COMMANDS_ASYNC_COMMANDS_IGNORE.

        Both spellings have to miss the ignore list: the bare name (``shell``)
        and the app-qualified one (``django.core.shell``). Ignoring by bare name
        therefore blocks the command in every app that provides it.
        """
        items = [command_name]
        if app_name is not None:
            items.append(app_name + "." + command_name)

        def check_command_name(name):
            return str(name) not in self.commands_skip

        return all(map(check_command_name , items))

    def get_context_data(self, **kwargs):
        context = super(TaskFormView, self).get_context_data(**kwargs)

        commands = collections.OrderedDict()
        commands_all = get_commands()

        # merge
        for command_name in commands_all:
            app_name = commands_all[command_name]
            items = commands.setdefault(app_name, [])
            if self.is_command_valid(command_name, app_name=app_name):
                items.append(command_name)

        # order
        for key in commands:
            commands[key].sort()

        context['commands'] = commands
        context['settings'] = settings
        return context

    def form_invalid(self, form):
        """Answers with the field errors instead of re-rendering the page.

        Deliberately unlike FormView: the form is submitted through jquery.form
        and the page paints the errors next to the inputs it already has.
        """
        return JsonResponse({
            'form': {
                'errors': form.errors
            }
        }, status=400)

    def form_valid(self, form):
        """Executes the task"""
        app_command = form.cleaned_data['app_command']
        command_args = form.cleaned_data['args']
        command_kwargs = form.cleaned_data['kwargs']

        if not self.is_command_valid(app_command, app_name=form.app_name) or not self.has_permission():
            if not is_ajax(self.request):
                raise PermissionDenied  # Can not execute this command.
            else:
                return JsonResponse({
                    'form': {'errors': {'app_command': [_("not allowed to run this command")]}}
                }, status=400)
        try:
            # confere o broker antes do disparo: o `apply_async` espera sem limite por um broker sem rota (#8944)
            precheck_before_launch()
            task = command_exec.apply_async(args=(app_command,) + command_args,
                                            kwargs=command_kwargs,
                                            priority=settings.COMMANDS_ASYNC_TASK_PRIORITY)
        except Exception:
            logger.exception('failed to execute {0!s}'.format(app_command))
            task = None
        if task is None:
            return JsonResponse({
                'form': {
                    'errors': {'app_command': [
                        _("command %(command)s failed to execute") % {'command': app_command}
                    ]}
                }
            }, status=400)
        if settings.COMMANDS_ASYNC_TASK_OWNERSHIP:
            # o dono entra no registro antes de a resposta sair: a primeira consulta da tela já o encontra
            remember_task(self.request.user, task.id)
        return JsonResponse({
            'task': {
                'id': task.id,
                'command': {
                    'name': app_command
                }
            }
        })


class CeleryWorkerStatus(CommandGateMixin, View):
    """Reports status of workers celery, for an authenticated user who may run commands."""

    @staticmethod
    def forbidden_json():
        # The shape the route already answers on failure, with no ``message`` and no worker name.
        return JsonResponse({'status': False, 'workers': []}, status=403)

    def get(self, request, **kwargs):
        """Lists the workers that answered, from the query this process made in the last ``WORKERS_WINDOW_SECONDS``.

        The window is per process and holds the result of any outcome, a failure included: the broadcast waits for the
        workers and the page asks again every few seconds, so a flood of requests would otherwise be a flood of
        broadcasts. The lock is held through the query, and whoever waits for it reads the state again once it has it:
        simultaneous requests make one broadcast and share its result.
        """
        with workers_lock:
            started = clock()
            if workers_window['at'] is None or not 0 <= started - workers_window['at'] < WORKERS_WINDOW_SECONDS:
                self.ask_workers(started)
            kind, names = workers_window['kind'], workers_window['names']
        return JsonResponse(self.workers_data(kind, names))

    @staticmethod
    def ask_workers(started):
        """Asks the workers and keeps the result in the window, as of ``started``. The caller holds ``workers_lock``.

        The detail of a failure goes to the log, never to the user: the text of the kombu error carries the address of the
        broker. ``active()`` answers ``None`` when no worker answers (the Celery ``Inspect`` returns whatever the broadcast
        collected, and nothing collected is ``None``).
        """
        try:
            broker_precheck()
            active = celery_app.control.inspect().active()
            kind, names = (WORKERS_NOBODY, ()) if active is None else (WORKERS_LISTED, tuple(active))
        except Exception:
            logger.exception('the Celery workers could not be asked')
            kind, names = WORKERS_FAILED, ()
        workers_window.update(at=started, kind=kind, names=names)

    @staticmethod
    def workers_data(kind, names):
        """The answer of a result of the window. The phrase is translated here, for the language of whoever asks now."""
        if kind == WORKERS_LISTED:
            return {'status': True, 'workers': list(names)}
        message = _('no Celery worker answered') if kind == WORKERS_NOBODY else _('could not ask the Celery workers')
        return {'status': False, 'workers': [], 'message': message}


class TaskFormStatus(CommandGateMixin, View):
    """Reads and revokes a task, for an authenticated user who may run commands.

    The route used to stay open, and it answered the output of any task to anyone: the gate is the one of
    ``CommandGateMixin``, and its denial in JSON carries the ``task`` this route answers. Since #8864 the task has to
    be one the user launched through the form (``task_denial``), and every answer of this route is ``no-store``.
    """

    never_cache_response = True

    @staticmethod
    def forbidden_json():
        return JsonResponse({'task': None}, status=403)

    def task_denial(self, request, *args, **kwargs):
        """404 for a task that is not the user's, with any method and before anything of the task is read.

        The same answer for another user's task, an id that never existed, a record that expired or a registry that cannot
        be read: a 403 would tell whoever probes that the id exists. An id that is not a task id at all (empty, which the
        route accepts on purpose, or not text) gets the same 404 with the owner check on or off (#8867): the result
        backend refuses it with a ``ValueError`` (a 500), and a revoke of it would broadcast an id that is nobody's.
        """
        task_id = kwargs.get('task_id')
        if is_task_id(task_id) and (not settings.COMMANDS_ASYNC_TASK_OWNERSHIP or owns_task(request.user, task_id)):
            return None
        return JsonResponse({'task': None}, status=404)

    def get(self, request, **kwargs):
        """Reports task status.

        Only the reads of the result backend sit under the ``try`` (``ready()``, ``failed()`` and then ``traceback`` or ``result``:
        a task that is not ready costs one read each). When one of them raises, the route stops at that read, logs the error and
        answers 503 with ``{"task": null}``: the text of the error carries the address of the backend, so it goes to the log and
        never to the answer, and the message of the record leaves out the task id (client text when the owner check is off).

        The route adds no time limit of its own: how long a read waits for a backend that does not answer is what the Celery of
        the host gives it (``redis_socket_timeout``, ``redis_socket_connect_timeout`` and ``redis_retry_on_timeout`` for Redis).
        The ``JsonResponse`` stays outside the ``try`` on purpose: a result that cannot be serialized (the result of a REVOKED or
        RETRY task is an exception) is not a failed read, and it is answered as before.
        """
        task_id = str(kwargs.get("task_id"))
        try:
            async_result = AsyncResult(id=task_id, app=celery_app)
            data = {
                'task': {
                    'id': async_result.id,
                    'ready': async_result.ready(),
                }
            }
            if async_result.failed():
                data['task']['failed'] = True
                data['task']['traceback'] = async_result.traceback
            else:
                data['task']['failed'] = False
                data['task']['output'] = async_result.result
        except Exception:
            logger.exception('the task status could not be read')
            return JsonResponse({'task': None}, status=503)
        return JsonResponse(data)

    def post(self, request, **kwargs):
        """Revokes the task. A failure to send is answered with a fixed phrase, and its detail goes to the log.

        The text of the error carries the address of the broker, so it only reaches the answer where the host
        turns ``COMMANDS_ASYNC_REVOKE_ERROR_DETAIL`` on (read from the settings module at the time of use). The id of
        the task is not logged: with the owner check off it is text from the client.
        """
        task_id = kwargs.get('task_id')
        try:
            broker_precheck()
            celery_app.control.revoke(task_id, terminate=True)
            data = {"status": True}
        except Exception as err:
            logger.exception('the revocation request could not be sent')
            reason = str(err) if settings.COMMANDS_ASYNC_REVOKE_ERROR_DETAIL else _('the revocation request could not be sent')
            data = {"status": False, "error": reason}
        return JsonResponse(data)
