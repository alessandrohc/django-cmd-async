# coding=utf-8
"""``TaskForm``: turning three hidden text inputs into a command invocation.

The page posts args and kwargs as free text, so the parsing rules here are what
stands between the browser and ``call_command``.
"""
from django.test import SimpleTestCase

from commands_async.forms import TaskForm


def bound(app_command='myapp.mycommand', args='', kwargs=''):
    form = TaskForm(data={'app_command': app_command, 'args': args, 'kwargs': kwargs})
    form.is_valid()
    return form


class AppCommandTest(SimpleTestCase):
    """The dotted value the dropdown submits is split into app and command."""

    def test_a_dotted_value_is_split_into_app_name_and_command(self):
        form = bound(app_command='django.core.check')
        self.assertEqual(form.cleaned_data['app_command'], 'check')
        self.assertEqual(form.app_name, 'django.core')

    def test_a_bare_command_leaves_the_app_name_unset(self):
        form = bound(app_command='check')
        self.assertEqual(form.cleaned_data['app_command'], 'check')
        self.assertIsNone(form.app_name)

    def test_the_trailing_marker_added_by_the_dropdown_is_stripped(self):
        # The list template appends '#' to keep the anchor href from navigating.
        form = bound(app_command='myapp.mycommand#')
        self.assertEqual(form.cleaned_data['app_command'], 'mycommand')

    def test_app_command_is_required(self):
        form = bound(app_command='')
        self.assertIn('app_command', form.errors)


class ArgsTest(SimpleTestCase):
    """Positional arguments: free text in, tuple out."""

    def test_a_bare_list_of_values_becomes_a_tuple(self):
        self.assertEqual(bound(args='"first", "second"').cleaned_data['args'],
                         ('first', 'second'))

    def test_surrounding_parentheses_are_accepted(self):
        self.assertEqual(bound(args='(1, 2)').cleaned_data['args'], (1, 2))

    def test_brackets_are_accepted(self):
        self.assertEqual(bound(args='["only"]').cleaned_data['args'], ('only',))

    def test_an_empty_value_becomes_an_empty_tuple(self):
        self.assertEqual(bound(args='').cleaned_data['args'], ())

    def test_unparsable_input_is_reported_on_the_args_field(self):
        self.assertIn('args', bound(args='"unterminated').errors)

    def test_a_literal_is_never_evaluated_as_an_expression(self):
        # ast.literal_eval, not eval: an expression is a validation error, not a
        # remote code execution primitive.
        self.assertIn('args', bound(args='__import__("os").getcwd()').errors)


class KwargsTest(SimpleTestCase):
    """Keyword arguments: free text in, dict out."""

    def test_a_bare_pair_becomes_a_dict(self):
        self.assertEqual(bound(kwargs='"tag": "x"').cleaned_data['kwargs'], {'tag': 'x'})

    def test_surrounding_braces_are_accepted(self):
        self.assertEqual(bound(kwargs='{"tag": "x"}').cleaned_data['kwargs'], {'tag': 'x'})

    def test_an_empty_value_becomes_an_empty_dict(self):
        self.assertEqual(bound(kwargs='').cleaned_data['kwargs'], {})

    def test_unparsable_input_is_reported_on_the_kwargs_field(self):
        self.assertIn('kwargs', bound(kwargs='"tag": ').errors)
