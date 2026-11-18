# coding=utf-8
import collections
import hashlib
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


def owner_key(user, task_id):
    """Chave do registro do dono: hash do `pk` do usuário e do id da tarefa, ou `None` quando não há como ser dono.

    O id vem do endereço (`^status/(?P<task_id>.*)`, que aceita o vazio e o com barra), então a chave é um hash com um
    separador entre o `pk` e o id: nunca depende do formato do id. Id vazio, que não é texto, ou usuário sem `pk`
    (o anônimo) nunca é dono.
    """
    pk = getattr(user, 'pk', None)
    if pk is None or not isinstance(task_id, str) or not task_id:
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
        data = {'status': True, 'workers': []}
        try:
            for worker in celery_app.control.inspect().active():
                data['workers'].append(worker)
        except Exception as err:
            data['status'] = False
            data['message'] = str(err)
        return JsonResponse(data)


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
        be read: a 403 would tell whoever probes that the id exists.
        """
        if not settings.COMMANDS_ASYNC_TASK_OWNERSHIP or owns_task(request.user, kwargs.get('task_id')):
            return None
        return JsonResponse({'task': None}, status=404)

    def get(self, request, **kwargs):
        """Reports task status"""
        task_id = str(kwargs.get("task_id"))
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
        return JsonResponse(data)

    def post(self, request, **kwargs):
        task_id = kwargs.get('task_id')
        try:
            celery_app.control.revoke(task_id, terminate=True)
            data = {"status": True}
        except Exception as err:
            data = {"status": False, "error": str(err)}
        return JsonResponse(data)
