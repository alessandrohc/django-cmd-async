from io import StringIO

from celery import shared_task
from celery.utils.log import get_task_logger
from django.core.management import call_command
import sys

from commands_async import settings

logger = get_task_logger(__name__)


class Output(object):
    """Collects a command's output, wherever the command decided to write it.

    Passing ``stdout=`` to ``call_command`` only covers commands that write
    through ``self.stdout``; anything using ``print()`` or a library that writes
    to the real ``sys.stdout`` would escape. So this both is handed to
    ``call_command`` and swaps ``sys.stdout`` for the duration of the call --
    which is why restoring it on ``__exit__`` matters: a worker process is
    long-lived and would otherwise keep writing into a dead buffer.

    Attribute access falls through to the wrapped stream, so it can stand in for
    a file object anywhere the command machinery expects one.
    """

    encoding = 'utf-8'

    def __init__(self, stream):
        self.stream = stream
        self.stdout = sys.stdout

    def __enter__(self):
        sys.stdout = self.stream
        return self

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, 'stream'), name)

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stdout = self.stdout

    def __str__(self):
        return self.stream.getvalue()


@shared_task(ignore_result=False,
             track_started=True,
             **settings.COMMANDS_ASYNC_TASK_OPTIONS)
def command_exec(name, *args, **kwargs):
    """ Run a Django command by name

    Returns everything the command wrote, which the page polls for and shows.

    A command that ends in ``SystemExit`` is a normal outcome, not a crash: exit
    code 0 is swallowed so the task is not marked failed (at the cost of the
    output collected so far, since there is no return in that path), while any
    other code propagates so the result carries the traceback.
    """
    with Output(StringIO()) as stream:
        kwargs['stdout'] = stream
        try:
            call_command(name, *args, **kwargs)
            return str(stream)
        except SystemExit as err:
            if hasattr(err, 'code') and err.code != 0:
                raise
