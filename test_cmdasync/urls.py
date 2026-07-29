# coding=utf-8
"""URLconf for the suite -- mounts the app under a prefix, as a host project does.

``commands_async.urls`` already prefixes its own patterns with ``command/async/``
and namespaces them under ``command-async``, so the routes land on
``/admin/command/async/...`` here.
"""
from django.contrib.auth.views import LoginView
from django.urls import include, path

urlpatterns = [
    path('admin/', include('commands_async.urls')),
    # Target of the login_required redirect; needs to resolve for the assertions
    # on the redirect chain to mean anything.
    path('login/', LoginView.as_view(template_name='cmdasync/base.html'), name='login'),
]
