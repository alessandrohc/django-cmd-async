from django import forms
import ast


class TaskForm(forms.Form):
    """ Form that displays the task list """

    app_command = forms.CharField(max_length=255,
                                  required=True,
                                  widget=forms.HiddenInput())

    args = forms.CharField(max_length=1024,
                           widget=forms.HiddenInput(),
                           required=False)

    kwargs = forms.CharField(max_length=1024,
                             widget=forms.HiddenInput(),
                             required=False)

    def __init__(self, *args, **kwargs):
        super(TaskForm, self).__init__(*args, **kwargs)
        self.app_name = None

    def clean_app_command(self):
        """Splits the submitted value into the app name and the command name.

        The dropdown submits ``app.label.command`` with a trailing ``#`` (kept in
        the anchor href so clicking it does not navigate); the text input submits
        a bare command name, and then there is no app to record.
        """
        app_command = self.cleaned_data['app_command']
        app_command = app_command.rstrip("#")
        try:
            self.app_name, app_command = app_command.rsplit(".", 1)
        except ValueError:
            self.app_name = None
        return app_command

    def clean_args(self):
        """Parses the free-text arguments into a tuple.

        The user types what they would type on a command line, so the brackets
        are added when missing -- ``"a", "b"``, ``("a", "b")`` and ``["a", "b"]``
        all have to work. ``literal_eval`` and never ``eval``: this input reaches
        the form from the browser, so an expression must be a validation error,
        not something the worker evaluates.
        """
        command_args = self.cleaned_data['args']
        command_args = command_args.strip("() ")
        if isinstance(command_args, str):
            if not command_args.startswith("["):
                command_args = "[" + command_args
            if not command_args.endswith("]"):
                command_args = command_args + "]"
        try:
            command_args = tuple(ast.literal_eval(command_args))
        except Exception as err:
            raise forms.ValidationError(str(err))
        return command_args

    def clean_kwargs(self):
        """Parses the free-text options into a dict -- see clean_args."""
        command_kwargs = self.cleaned_data['kwargs']
        command_kwargs = command_kwargs.strip()
        if isinstance(command_kwargs, str):
            if not command_kwargs.startswith("{"):
                command_kwargs = "{" + command_kwargs
            if not command_kwargs.endswith("}"):
                command_kwargs = command_kwargs + "}"
        try:
            command_kwargs = ast.literal_eval(command_kwargs)
        except Exception as err:
            raise forms.ValidationError(str(err))
        return command_kwargs
