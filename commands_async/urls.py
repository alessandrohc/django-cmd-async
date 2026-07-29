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
    # Stays a regex: the id has to be optional, because the page builds this URL
    # before the task has one, and every path() converter requires at least one
    # character. Unauthenticated on purpose -- the browser keeps polling for the
    # output of a task it already started.
    re_path(r'^status/(?P<task_id>.*)', never_cache(views.TaskFormStatus.as_view()),
            name='status'),
    path('workers/status/', login_required(never_cache(views.CeleryWorkerStatus.as_view())),
         name='workers'),
], 'command-async')

urlpatterns = [
    path('command/async/', include(cmd_patterns)),
]
