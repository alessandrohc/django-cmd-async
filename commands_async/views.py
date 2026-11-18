# coding=utf-8
import collections
from urllib.parse import urlsplit
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

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            response = self.forbidden_json() if is_ajax(request) else login_redirect(request)
        elif not self.has_permission():
            response = self.forbidden_json() if is_ajax(request) else HttpResponseForbidden()
        else:
            return super().dispatch(request, *args, **kwargs)
        add_never_cache_headers(response)
        return response

    def forbidden_json(self):
        raise NotImplementedError('{0} has to answer its own denial in JSON'.format(type(self).__name__))


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
    ``CommandGateMixin``, and its denial in JSON carries the ``task`` this route answers.
    """

    @staticmethod
    def forbidden_json():
        return JsonResponse({'task': None}, status=403)

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
