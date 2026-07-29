# django-cmd-async

Django app that makes it possible to execute management commands through your web browser.
It uses bootstrap v4 to give the interface a better look.
Commands are run through [Celery](https://docs.celeryq.dev) - Distributed Task Queue, which
allows us to manage execution and return data with ease.

## Requirements

| | |
| --- | --- |
| Python | 3.10 – 3.13 |
| Django | 4.2 – 5.2 |
| Celery | 5.5 – 5.x |

A running Celery worker and broker are required: the page queues a task and then polls for
its output.

## Install

```
pip install django-cmd-async
```

## App configure

#### Add the app to INSTALLED_APPS

```python
INSTALLED_APPS.append("commands_async")
```

#### Include app urls

```python
from django.urls import include, path

urlpatterns.append(path("app/", include("commands_async.urls")))
```

The app prefixes its own routes with `command/async/`, so the example above serves the page
at `/app/command/async/`. Three routes are registered under the `command-async` namespace:

| Name | Path | Login required |
| --- | --- | --- |
| `command-async:index` | `command/async/` | yes |
| `command-async:status` | `command/async/status/<task_id>` | no |
| `command-async:workers` | `command/async/workers/status/` | yes |

`command-async:status` is deliberately open: the browser keeps polling for the output of a
task it already started, and it reveals nothing but that task's own result.

## DJANGO settings

### It should show a list of commands to the user. Otherwise, a text input will be displayed.
[bool] COMMANDS_ASYNC_LIST (default: False)

### Login is a required feature, because we need a url.
[str]  COMMANDS_ASYNC_LOGIN_URL  (default: settings.LOGIN_URL)

### List with commands that should be ignored. [runserver, shell, etc]. An error will be shown to the user when the command is in that list.
Entries match either the bare command name (`shell`) or the app-qualified one
(`django.core.shell`).

[list] COMMANDS_ASYNC_COMMANDS_IGNORE  (default: [])

### The user who will perform tasks must have this permission. It does not need to be configured, but if configured, the user will need to have this permission to execute commands. An error message will be shown to the user if he does not have this permission.
[str]  COMMANDS_ASYNC_PERMISSION_NAME  (default: None)

### Parameter `next` to the login url.
[str]  COMMANDS_ASYNC_REDIRECT_FIELD_NAME (default: django.contrib.auth.REDIRECT_FIELD_NAME)

### Options handed to the Celery task, merged over the defaults `{"expires": 1800, "retries": 0}`.
A `priority` key is accepted here and forwarded to `apply_async` instead of the task
decorator.

[dict] COMMANDS_ASYNC_TASK_OPTIONS  (default: {"expires": 1800, "retries": 0})

### Whether the page should probe the workers and warn when none is up.
[bool] COMMANDS_ASYNC_WORKERS_STATUS_CHECK  (default: False)

> These settings are read **once, at import time**. Changing them at runtime (including with
> `override_settings`) has no effect until the process restarts.

## Test (python manage.py runserver 8080)
In browser: http://localhost:8080/app/command/async/

## Running the test suite

The suite runs on Django's native test runner and needs neither a broker nor a real database
-- the sqlite in-memory one exists only for the `auth` tables:

```
pip install -e ".[test]"
python runtests.py                            # whole suite
python runtests.py test_cmdasync.test_views   # a single module
coverage run runtests.py && coverage report   # with coverage
```

`msgfmt` (GNU gettext) is needed for the catalog checks; without it they skip.

## Visual Result

![Input view](https://github.com/alessandrohc/django-cmd-async/raw/python3-dj32/look/command.output.input.method.PNG)
![List view](https://github.com/alessandrohc/django-cmd-async/raw/python3-dj32/look/command.output.list.method.PNG)

## Credits

Maintained by [Alessandro Hecht](https://github.com/alessandrohc).
Originally created by [Alex Sandro](https://github.com/alexsilva) — released under the MIT license (see [LICENSE](LICENSE)).
