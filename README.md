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
| `command-async:status` | `command/async/status/<task_id>` | yes |
| `command-async:workers` | `command/async/workers/status/` | yes |

`command-async:status` answers the state, output and traceback of a task, and revokes it
(`POST`). It was open to anyone, and it answered the result of **any** task id, not only of the
tasks launched through the page: since this release it checks the user itself (in the view, not
in the URLconf, so a project that mounts or subclasses the view keeps the check):

- the user has to be authenticated, and, when `COMMANDS_ASYNC_PERMISSION_NAME` names a
  permission, has to hold it (the same permission the form asks for; with none named, any
  authenticated user is served, like the form);
- a denied request gets a response without any task data, marked `no-store`: an XHR (a request
  that sends `X-Requested-With: XMLHttpRequest`, as the bundled JS does) gets a `403` with
  `{"task": null}`; any other request is redirected to `COMMANDS_ASYNC_LOGIN_URL` (not logged in)
  or gets the plain `403` (no permission).

A script that polls this route without a session, or that only asks for JSON (without the
`X-Requested-With` header), now has to authenticate.

### The owner of the task

The route also ties a task to the user who launched it through the form: only that user can
read or revoke it (from any of his login sessions). Anyone else, an id that was never launched,
a record that expired and a registry that cannot be read are all answered the same `404` with
`{"task": null}` and no task data, with any method and before anything of the task is read (a
`403` would tell whoever probes that the id exists). The id of the task is still the Celery id.

The owner is recorded in a Django cache when the form launches the task. Because of that:

- **Point `COMMANDS_ASYNC_OWNER_CACHE_ALIAS` at a cache that the processes of the host share,
  that nobody clears, and that is not a `DummyCache`.** The local-memory cache (the Django
  default) is not shared between processes: the owner of a task launched in one process would be
  answered `404` in another. A page cache is not a good choice either: it is a `DummyCache` when
  the host switches its cache off, and it is cleared on every content save. A Django system check
  (`commands_async.W001`) warns when the alias is missing from `CACHES` or is a `DummyCache`.
- A task launched before this release, one older than the record's lifetime (24 hours, as the
  default `result_expires` of Celery), and one whose record could not be written (the error is
  logged with the task id) are no longer readable or revocable by their own user.
- `COMMANDS_ASYNC_TASK_OWNERSHIP = False` switches the owner check off: any authorized user reads
  and revokes any task, as before. This release narrows what `status` serves, so a project that
  relied on a user following another's tasks has to switch it off.

`command-async:workers` lists the names of the Celery workers that answer (`GET`). It was open
to any authenticated user, whatever `COMMANDS_ASYNC_PERMISSION_NAME` said: since this release it
has the same gate as `status`, with the same rules, and every denial is answered before Celery
is asked anything (the probe asks every worker and waits for the answers). The denial of an XHR
is a `403` with `{"status": false, "workers": []}`; any other request is redirected to
`COMMANDS_ASYNC_LOGIN_URL` (not logged in) or gets the plain `403` (no permission). A denied
request gets that answer with any method, where it used to get `405` for a method other than
`GET`; an authorized one still gets the `405`.

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
The probe asks `command-async:workers`, which needs the same permission as the form: when
`COMMANDS_ASYNC_PERMISSION_NAME` names one, a user without it who opens the page gets a `403`,
and the bundled JS asks again every 5 seconds (it retries after any failure).

[bool] COMMANDS_ASYNC_WORKERS_STATUS_CHECK  (default: False)

### Whether the status route serves a task only to the user who launched it.
See "The owner of the task" above. It does not apply to `command-async:workers`.

[bool] COMMANDS_ASYNC_TASK_OWNERSHIP  (default: True)

### The cache alias (a key of `CACHES`) that keeps the owner of each launched task.
It has to be shared by the processes of the host, not cleared and not a `DummyCache`.

[str]  COMMANDS_ASYNC_OWNER_CACHE_ALIAS  (default: "default")

### For how long the owner of a launched task is kept, in seconds.
The default is the default lifetime of a Celery result; after it the user who launched the task is
answered `404` on it, whether or not the backend still has the result.

[int]  COMMANDS_ASYNC_OWNER_TIMEOUT  (default: 86400)

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
