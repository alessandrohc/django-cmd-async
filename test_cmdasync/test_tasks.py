# coding=utf-8
"""The ``command_exec`` task: output capture and how a command's exit is handled.

The task is called directly, which runs its body synchronously -- no broker and
no eager-mode configuration needed.
"""
import sys

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from commands_async.tasks import Output, command_exec


class CommandExecTest(SimpleTestCase):
    """What the worker hands back to the page as the command's output."""

    def test_returns_everything_the_command_wrote_to_stdout(self):
        output = command_exec('cmdasync_echo', 'first', 'second', tag='x')

        self.assertIn("words=['first', 'second']", output)
        self.assertIn('tag=x', output)

    def test_a_clean_exit_is_not_reported_as_a_failure(self):
        # SystemExit(0) is how a command signals "nothing more to do"; the task
        # swallows it, and with it the output collected so far.
        self.assertIsNone(command_exec('cmdasync_exit', code=0))

    def test_a_non_zero_exit_propagates_so_the_task_is_marked_failed(self):
        with self.assertRaises(SystemExit):
            command_exec('cmdasync_exit', code=2)

    def test_a_command_error_propagates(self):
        with self.assertRaises(CommandError):
            command_exec('cmdasync_does_not_exist')


class OutputRestoreTest(SimpleTestCase):
    """``Output`` hijacks ``sys.stdout``; it has to give it back either way.

    A worker process is long-lived and runs one task after another -- a leaked
    redirect would silently swallow the log output of everything that follows.
    """

    def test_stdout_is_restored_after_a_successful_command(self):
        original = sys.stdout
        command_exec('cmdasync_echo')
        self.assertIs(sys.stdout, original)

    def test_stdout_is_restored_after_a_command_that_exits(self):
        original = sys.stdout
        with self.assertRaises(SystemExit):
            command_exec('cmdasync_exit', code=2)
        self.assertIs(sys.stdout, original)

    def test_output_proxies_attribute_access_to_the_wrapped_stream(self):
        from io import StringIO

        stream = StringIO()
        with Output(stream) as proxy:
            proxy.write('written through the proxy')
        self.assertEqual(str(Output(stream)), 'written through the proxy')
