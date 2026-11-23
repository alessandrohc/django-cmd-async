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
`403` would tell whoever probes that the id exists). An id that is not a task id at all (the empty
one, which the route accepts on purpose) gets the same `404` with the owner check on or off and
with any method, and nothing of Celery is asked. The id of the task is still the Celery id.

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
  and revokes any task, as before, but an empty task id is still answered `404` (it used to be a
  `500`, because the result backend refuses an empty id). This release narrows what `status`
  serves, so a project that relied on a user following another's tasks has to switch it off.

`command-async:workers` lists the names of the Celery workers that answer (`GET`). It was open
to any authenticated user, whatever `COMMANDS_ASYNC_PERMISSION_NAME` said: since this release it
has the same gate as `status`, with the same rules, and every denial is answered before Celery
is asked anything (the probe asks every worker and waits for the answers). The denial of an XHR
is a `403` with `{"status": false, "workers": []}`; any other request is redirected to
`COMMANDS_ASYNC_LOGIN_URL` (not logged in) or gets the plain `403` (no permission). A denied
request gets that answer with any method, where it used to get `405` for a method other than
`GET`; an authorized one still gets the `405`.

### Errors, the broker and the 5-second window

Neither route answers the text of a Celery or broker error any more: the text of a kombu error
carries the address of the broker. The detail goes to the log of the package, and the answer
carries a fixed phrase, still with a `200`:

- `command-async:workers`: `{"status": false, "workers": [], "message": "could not ask the Celery
  workers"}` when the query fails, and `"message": "no Celery worker answered"` (with `status`
  false too) when the broadcast collects nothing, which used to surface as the text of a Python
  `TypeError`.
- `command-async:status`, the revocation (`POST`): `{"status": false, "error": "the revocation
  request could not be sent"}`. `COMMANDS_ASYNC_REVOKE_ERROR_DETAIL = True` puts the text of the
  error back in `error`, for a host that shows it only to who may read it. The workers route does
  not read that setting: it always answers the fixed phrase.

The workers route keeps its answer for 5 seconds, per process: a query that arrives less than 5
seconds after the previous one of the same process (from any user, whatever the result: the list,
nobody answered, or a failure) is answered with the previous result without asking Celery again,
and queries that arrive together make one broadcast. So the list can be up to 5 seconds old, a
failure stays for up to 5 seconds after the broker is back, and `N` processes make up to `N`
queries per window. "One broadcast" holds while the query lasts less than 5 seconds: if it lasts
5 seconds or more (a broker that is alive but slow, which the check below does not limit), those
that waited for it ask again, once, and that new query opens the window for the ones that follow.

Both routes first check, with a short limit, that the broker accepts a connection, so that a
broker that refuses the connection or does not answer is answered with a false `status` within 5
seconds. Without the check the Celery configuration of a project may hold the request for minutes
(a `broker_connection_timeout` of 300 seconds, for instance). When the owner registry lives in the
same Redis as the broker and cannot be read, the revocation is answered the `404` of the owner
check before the broker is touched. Only a `redis://` broker on one host was measured: a refused
port answers in 2 seconds and an address with no route in 4. Sentinel and cluster were not
measured (with `N` unreachable sentinels the check should cost about `N` seconds), and a broker
that is alive but slow is not limited by the check.

The execution form makes the same check right before it queues the command. It comes after the
refusal of an ignored command or of a user without the permission, which never touch the broker.
Without it the form waits for the broker as long as opening the connection takes: with an address
that does not answer, more than 100 seconds were measured, while Celery gives up on a refused port
in about 1.3 seconds. With the check the form answers the usual `400` (`command <name> failed to
execute`) in about 2 seconds for a refused port and about 4 for an address with no route (the
slowest of 6 rounds took 4.35 seconds), with no task, and the text of the error and the address of
the broker only reach the log of the package. A host that runs Celery in synchronous mode
(`task_always_eager`) skips the check, because the task runs in the process with no broker. A host
that wraps `shared_task` to swallow connection errors (as the `plus` project does) used to see this
failure in its own log; the exception now reaches the log of this package, with the traceback. The
same limits apply: only a `redis://` broker on one host was measured, and a test that submits the
form without replacing `commands_async.views.precheck_before_launch` opens one connection to the
broker of its environment.

### The status query and the result backend

When a read of the Celery result backend raises (the backend refuses the connection, does not
answer, or fails in any other way), `command-async:status` answers `503` with `{"task": null}`,
`no-store` like every answer of the route, and the package logs the error with its traceback; the
message of the record leaves out the task id, which is client text when the owner check is off.
Before, the exception reached Django, which answered a `500` page. The text of the error and the
address of the backend never reach the answer. The route stops at the read that failed: the
state of a task is read once per query, whatever the state (the retries the client of the backend
makes below Celery do not count), and no read follows the one that failed. The bundled page stops following the task and releases the form on
any HTTP error, as it did with the `500`.

The route adds no time limit of its own: how long a read waits for a backend that does not answer
is what the Celery of the host gives it. For Redis that is `redis_socket_timeout`
(`CELERY_REDIS_SOCKET_TIMEOUT` with the usual namespace; the Celery default is 120 seconds),
`redis_socket_connect_timeout` (no default: the connection waits as long as the system lets it)
and `redis_retry_on_timeout` (one more attempt per read). Measured on a `redis://` backend on one
host, with 2 seconds for the connection, 3 seconds for the read and `redis_retry_on_timeout` on
(Celery 5.6.3, redis-py 8.1.0): a refused port fails at once, an address with no route in about 4
seconds (two attempts of 2 seconds) and a Redis that accepts the connection and never answers in
about 6 seconds (two attempts of 3 seconds). A host that leaves the Celery defaults may hold the
thread of the server for much longer on each read, so set those options. With Sentinel the limit
for the connections to the sentinels is `result_backend_transport_options['sentinel_kwargs']`,
which Celery leaves empty (and then redis-py does not hand the socket options to the sentinels):
that case was read in the code, not measured.

When the owner registry lives in the same Redis as the result backend and cannot be read, the
query is answered the `404` of the owner check before the backend is touched, as the revocation is
(see above): the `503` is for a failure of the result backend while the owner registry answers.
A task whose state is `REVOKED` or `RETRY` has an exception as its result, which cannot be
serialized: the route answers it `200`, with the text of the exception as `output` (an empty text when
the exception has none, and `null` for a `RETRY` that stored no exception), `ready` true and `failed`
false for a revoked task, and both false for a task being retried.

### The command page: Cancel, the switch of command and Revoke

The bundled page follows a task from the moment the form is sent, not only from the second status
query. **Cancel** (and, when the page shows the list of commands, the **switch of command**) ends
that follow-up wherever it is: while the form is being sent, with the first status query in flight,
or between two queries. The screen is released when the answer in flight arrives, or at once when a
new query was only scheduled, and no status query goes out after it. The released screen has an
empty output panel and no Copy button: the running text of the last query is not left in it.
**The task keeps running**:
stopping it is Revoke, to be used before Cancel, because the page disables Revoke when it releases
the screen. Before, Cancel and the switch were ignored until the second query: the follow-up went
on to the end, with Cancel disabled and its indicator lit.

**Revoke** stays disabled from the submit until the server answers the form with the task, and then
asks for the revocation of **that** task. It used to be enabled at the submit: a click before the
answer raised a script error on the first run, and on the next runs revoked the task of the previous
run.

A host with a page of its own that uses `cmdhandler.js` gets the same through
`cmdasyncform.ajax()`, which wraps the `beforeSubmit` of the page and calls the new `begin()`
(the follow-up is on, no task is known yet) unless the page refused the submit by returning `false`.
`revoke_task()` with no task accepted in the run sends nothing and fires `revoke-failed`, so a page
that enabled its Revoke button early ends the click like any failed revocation. A host that never
goes through `ajax()` keeps the old behaviour of the interruption: it is ignored until the second
query. The `updating` event is fired for the answer of each status query while the follow-up goes
on, and **not** for the answer of an interrupted follow-up (the handler empties the output panel
and releases the screen instead). `ajax()` may be called more than once: the handler registers its
own `form-valid` callback once per `init()`, so a task is followed by a single chain of status
queries however many times it is called.

### The workers probe of the page

With `COMMANDS_ASYNC_WORKERS_STATUS_CHECK = True` the page asks `command-async:workers` when it
opens (`celery.workers.status.js`) and shows the notice with the names of the workers, or with the
message of a false `status`. What the probe does with a failure depends on why it failed:

- **`403`** (the gate of the route denied the XHR: no session, or no permission): the probe stops, no
  new query goes out, and the notice opens with "Access denied: your session may have expired, or you
  do not have permission to see the Celery workers." in the place of what it showed. The template of
  the notice publishes the phrase (`data-forbidden-text` of `toast.html`), so it is in the catalog of
  the package and translated (pt_BR).
- **The login page** (an answer that is not JSON, which the browser reaches when a layer outside the
  route redirects): the probe stops and the page takes the user to the login.
- **A false `status`** (nobody answered, the broker failed), **a network failure or a server error**:
  the probe asks again after 5 seconds, as before.

A host with a page of its own calls `workers_check_status({onLogin: <function>})`: that function is
what runs on the login page (the bundled page passes its `goToLogin`), and without it the probe just
stops. A notice without `data-forbidden-text`, or a page without the Bootstrap toast plugin (the
`bootstrap.bundle`), stops on the `403` with no notice and no script error. A host with an older
`cmdhandler.js` (without `failure_reason`) keeps asking again after any failure.

## DJANGO settings

### It should show a list of commands to the user. Otherwise, a text input will be displayed.
[bool] COMMANDS_ASYNC_LIST (default: False)

### Login is a required feature, because we need a url.
The way back to the page travels in Django's `next` parameter (`REDIRECT_FIELD_NAME`); there is
no setting for its name.

[str]  COMMANDS_ASYNC_LOGIN_URL  (default: settings.LOGIN_URL)

### List with commands that should be ignored. [runserver, shell, etc]. An error will be shown to the user when the command is in that list.
Entries match either the bare command name (`shell`) or the app-qualified one
(`django.core.shell`).

[list] COMMANDS_ASYNC_COMMANDS_IGNORE  (default: [])

### The user who will perform tasks must have this permission. It does not need to be configured, but if configured, the user will need to have this permission to execute commands. An error message will be shown to the user if he does not have this permission.
[str]  COMMANDS_ASYNC_PERMISSION_NAME  (default: None)

### Options handed to the Celery task, merged over the defaults `{"expires": 1800, "retries": 0}`.
A `priority` key is accepted here and forwarded to `apply_async` instead of the task
decorator.

[dict] COMMANDS_ASYNC_TASK_OPTIONS  (default: {"expires": 1800, "retries": 0})

### Whether the page should probe the workers and warn when none is up.
The probe asks `command-async:workers`, which needs the same permission as the form: when
`COMMANDS_ASYNC_PERMISSION_NAME` names one, a user without it who opens the page gets a `403`,
and the bundled JS stops asking and tells why (see "The workers probe of the page" above).

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

### Whether the failure of a revocation answers the text of the error.
Off (the default), `error` is a fixed phrase and the text of the error goes to the log. See "Errors,
the broker and the 5-second window" above. It does not apply to `command-async:workers`.

[bool] COMMANDS_ASYNC_REVOKE_ERROR_DETAIL  (default: False)

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

`node` is needed for the tests of the page scripts (`test_cmdasync/test_page_script.py` runs the real
scripts under it, with a double of jQuery and of the DOM); without it they **fail**, on purpose: a skip
would turn them green without proving anything. The CI installs it (`actions/setup-node`).

## Visual Result

![Input view](https://github.com/alessandrohc/django-cmd-async/raw/python3-dj32/look/command.output.input.method.PNG)
![List view](https://github.com/alessandrohc/django-cmd-async/raw/python3-dj32/look/command.output.list.method.PNG)

## Credits

Maintained by [Alessandro Hecht](https://github.com/alessandrohc).
Originally created by [Alex Sandro](https://github.com/alexsilva) — released under the MIT license (see [LICENSE](LICENSE)).
