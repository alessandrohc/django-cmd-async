# coding=utf-8
from django.contrib.auth.decorators import login_required
from django.urls import include, path, re_path
from django.views.decorators.cache import never_cache

from . import views, settings

__author__ = 'alex'


cmd_patterns = ([
    path('', login_required(never_cache(views.TaskFormView.as_view()),
                            login_url=settings.COMMANDS_ASYNC_LOGIN_URL),
         name='index'),
    # Stays a regex: the id may be empty or hold a slash, and every path() converter
    # requires at least one character (the host project builds this URL with ids of
    # both kinds, so the pattern keeps accepting them). The login and the permission
    # are checked by the view itself (TaskFormStatus.dispatch), not by a decorator
    # here: a host that swaps the callback or mounts the view elsewhere keeps the gate,
    # and an XHR gets a JSON 403 where login_required would answer a 302.
    re_path(r'^status/(?P<task_id>.*)', never_cache(views.TaskFormStatus.as_view()),
            name='status'),
    path('workers/status/', login_required(never_cache(views.CeleryWorkerStatus.as_view())),
         name='workers'),
], 'command-async')

urlpatterns = [
    path('command/async/', include(cmd_patterns)),
]
