/**
 * Wiring for the command page.
 *
 * This used to be an inline <script> in index.html, which a nonce-only CSP
 * refuses. Everything it needed from the template now arrives as data-* on
 * #task-panel, so the file is static and cacheable.
 */
$(document).ready(function () {
    var $panel = $('#task-panel');

    // jQuery.data() coerces "true"/"false" to booleans, so the two flags arrive typed.
    var config = {
        commandsList: $panel.data('commands-list'),
        workersStatusCheck: $panel.data('workers-status-check'),
        loginUrl: $panel.data('login-url'),
        redirectField: $panel.data('redirect-field'),
        indexUrl: $panel.data('index-url'),
        runningText: $panel.data('running-text'),
        // The failure messages, translated in the template. A host with a template of
        // its own may not publish them: showAlert() then shows only what the server sent.
        revokeFailedText: $panel.data('revoke-failed-text'),
        forbiddenText: $panel.data('forbidden-text'),
        notFoundText: $panel.data('not-found-text'),
        // The status query or the submit that fails for a server reason, or with no answer at all (the network failed, or
        // the query was cut at query_timeout); a host without them shows no text for that failure.
        serverErrorText: $panel.data('server-error-text'),
        networkErrorText: $panel.data('network-error-text')
    };

    // The alert space (#task-alert) is where a failure is told. .text() only, so what
    // the server sent is never parsed as markup; a missing space or text is a no-op.
    var showAlert = function (text) {
        if (!text)
            return;
        $("#task-alert").text(text).removeClass("d-none");
    };
    var clearAlert = function () {
        $("#task-alert").text("").addClass("d-none");
    };

    // Same destination as the form's "need login" answer below.
    var goToLogin = function () {
        var url = [config.loginUrl];
        url.push("?" + config.redirectField + "=");
        url.push(config.indexUrl);
        window.location = url.join("");
    };

    // Maps a form field to the visible input that mirrors it, for error placement.
    var rel_inputs = {
        'args': 'task-args',
        'kwargs': 'task-kwargs',
        'app_command': 'task-app-command'
    };

    if (config.workersStatusCheck) {
        workers_check_status({onLogin: goToLogin});
    }

    cmdasyncform.init("#task-output");

    $("#task-copy").on('click', function () {
        cmdasyncform.copyToClipboard("#task-output");
    });

    // The visible inputs feed the hidden fields the form actually submits.
    $("#task-args").on('change', function () { $("#id_args").val($(this).val()); });
    $("#task-kwargs").on('change', function () { $("#id_kwargs").val($(this).val()); });

    // Hide the copy button while the command field has focus.
    $("#task-app-command").on('focusin', function () {
        $("#task-copy").addClass("d-none");
    }).on('focusout', function () {
        if ($("#task-output").text().length > 0)
            $("#task-copy").removeClass("d-none");
    });

    var $btnSubmit = $("#btn-task-submit");
    $btnSubmit.on('click', function () {
        $(this).find(".task-progress").removeClass("d-none");
        $("#task-copy").addClass("d-none");
        cmdasyncform.clear_output();
        clearAlert();
    });

    if (config.commandsList) {
        // Dropdown mode: the anchor href carries "<app>.<command>" after the '#'.
        $(".dropdown-menu > a").on('click', function () {
            var value = $(this).attr("href");
            value = value.substring(1, value.length);

            var items = value.split(".");
            var app_command = items[items.length - 1];
            var app_name = items.slice(0, -1).join(".").toUpperCase();

            $("#form-app_name").text(app_name);
            $("#task-app-command").find("> span.info").text(app_command);

            $("input#id_app_command").val(value);
            // stop status update; the output is cleared first because aborting releases
            // the screen at once, and the copy button follows what the output holds
            cmdasyncform.clear_output();
            cmdasyncform.update_abort();
            clearAlert();
        });
    } else {
        // Text input mode: copy what was typed into the hidden field on submit.
        $btnSubmit.on('click', function () {
            $("#id_app_command").val($("#task-app-command").val());
        });
    }

    $("#btn-task-cancel").on('click', function () {
        var $btnCancel = $(this).prop('disabled', true);
        $btnCancel.find(".task-progress").removeClass("d-none");
        clearAlert();
        cmdasyncform.update_abort();
    });

    $("#btn-task-revoke").on('click', function () {
        var $btnRevoke = $(this).prop('disabled', true);
        $btnRevoke.find(".task-progress").removeClass("d-none");
        clearAlert();
        cmdasyncform.revoke_task.bind(cmdasyncform)();
    });

    var requestErrorClean = function () {
        $(".invalid-feedback").remove();
        for (var name in rel_inputs) {
            $("#" + rel_inputs[name]).removeClass("is-invalid");
        }
    };

    var requestUpdateFinish = function () {
        var $btnSubmit = $('#btn-task-submit').prop('disabled', false);
        $btnSubmit.find(".task-progress").addClass("d-none");

        var $btnCancel = $("#btn-task-cancel").prop('disabled', true);
        $btnCancel.find(".task-progress").addClass("d-none");

        var $btnRevoke = $("#btn-task-revoke").prop('disabled', true);
        $btnRevoke.find(".task-progress").addClass("d-none");

        // show/hide copy button
        $("#task-copy")[$("#task-output").text().length > 0 ? "removeClass" : "addClass"]("d-none");
    };

    cmdasyncform.ajax("form#taskform", {
        beforeSubmit: function (arr, $form, options) {
            var $btnSubmit = $form.find('#btn-task-submit');
            if ($btnSubmit.attr('disabled'))
                return false;
            $btnSubmit.prop('disabled', true);
            // Cancel can end the follow-up from the submit on. Revoke waits for the form-valid:
            // until the server answers there is no task to revoke.
            $form.find("#btn-task-cancel").prop('disabled', false);
        },
        error: function (xhr) {
            requestErrorClean();
            switch (xhr.status) {
                case 200:  // need login
                    goToLogin();
                    break;
                case 400:
                    var $input, data = xhr.responseJSON;
                    var $feedback = $('<div class="invalid-feedback"></div>');
                    for (var name in data.form.errors) {
                        $input = $("#" + rel_inputs[name]);
                        $input.addClass("is-invalid");
                        // .text(): a validation message echoes back what the user
                        // typed (ast.literal_eval puts the input in its error), so
                        // it must never be parsed as markup.
                        $input.parent().append($feedback.clone().text(data.form.errors[name][0]));
                    }
                    break;
                case 0:  // no answer: the network failed
                    showAlert(config.networkErrorText);
                    break;
                default:
                    // a server error says so; the other failures (the 403 of the CSRF, the 429...) show no text
                    if (xhr.status >= 500)
                        showAlert(config.serverErrorText);
            }
            requestUpdateFinish();
        }
    });

    cmdasyncform.add_event_callback("updating", function ($form) {
        $("#task-output").text(config.runningText);
    });

    cmdasyncform.add_event_callback("form-valid", function (responseText, statusText, xhr, $form) {
        requestErrorClean();
        // The server accepted the task: Revoke has something to stop. The handler's own form-valid
        // callback runs before this one (ajax() registers it first); when the follow-up was
        // interrupted during the submit it has already released the screen, Execute is enabled
        // and Revoke stays disabled.
        if ($btnSubmit.prop('disabled'))
            $("#btn-task-revoke").prop('disabled', false);
    });

    cmdasyncform.add_event_callback("update-finish", function (form) {
        requestUpdateFinish();
    });

    // The status query got no task: the reason arrives here, and the screen is released
    // right after by update-finish.
    cmdasyncform.add_event_callback("update-failed", function (reason) {
        if (reason === 'login')
            goToLogin();
        else if (reason === 'forbidden')
            showAlert(config.forbiddenText);
        else if (reason === 'not-found')
            showAlert(config.notFoundText);
        else if (reason === 'server')
            showAlert(config.serverErrorText);
        else if (reason === 'network')
            showAlert(config.networkErrorText);
    });

    cmdasyncform.add_event_callback("revoke-failed", function (serverText) {
        showAlert(serverText || config.revokeFailedText);
        // While the follow-up goes on (Execute is still disabled) the user may try to
        // revoke again; on a screen that was already released (a late failure) the
        // button stays disabled. Without a task (the form of a new run is still being sent, so
        // a late failure of the earlier run's revocation lands here) it stays disabled too.
        if ($btnSubmit.prop('disabled') && cmdasyncform.task) {
            var $btnRevoke = $("#btn-task-revoke").prop('disabled', false);
            $btnRevoke.find(".task-progress").addClass("d-none");
        }
    });
});
