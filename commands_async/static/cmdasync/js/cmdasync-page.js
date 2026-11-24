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
        networkErrorText: $panel.data('network-error-text'),
        // The submit the server refuses (any 4xx: the 403 of the CSRF, the 429...): the command was not run, so the text must not say
        // it may still be running; a host without it shows no text for that failure (#9016).
        refusedText: $panel.data('refused-text')
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

    // Maps a form field to the ids of the visible inputs that mirror it, for error placement. The arguments have one input per
    // layout and only one of them is visible at a time: the wide one keeps the ids that a host with a single variant already
    // uses, and the narrow one (response-form-task-args-md.html) takes the -md suffix, so no id is repeated in the page. An id the
    // page does not have (a host with its own template and a single variant) is an empty set in jQuery: it does nothing.
    var rel_inputs = {
        'args': ['task-args', 'task-args-md'],
        'kwargs': ['task-kwargs', 'task-kwargs-md'],
        'app_command': ['task-app-command']
    };

    if (config.workersStatusCheck) {
        workers_check_status({onLogin: goToLogin});
    }

    cmdasyncform.init("#task-output");

    $("#task-copy").on('click', function () {
        cmdasyncform.copyToClipboard("#task-output");
    });

    // The visible inputs feed the hidden fields the form actually submits. Each argument has an input per layout, so what is typed
    // in one also goes to the input of the other layout: the one that shows after the width changes holds what will be sent. The
    // text goes on every change (`input`: each key, paste or erase; `change`: what `input` misses, like the browser autofill),
    // empty included, and each event is its own .on() call (the node harness registers an event by its exact text). The mirror
    // uses .val() only, which fires no event, so the two inputs never call each other; it goes to the OTHER inputs, never back
    // to the one being typed in (writing to it during an IME composition breaks the composition).
    var bindArgument = function (ids, hiddenSelector) {
        ids.forEach(function (id) {
            var passText = function () {
                var text = $(this).val();
                $(hiddenSelector).val(text);
                ids.forEach(function (otherId) {
                    if (otherId !== id)
                        $("#" + otherId).val(text);
                });
            };
            $("#" + id).on('input', passText).on('change', passText);
        });
    };
    bindArgument(rel_inputs.args, "#id_args");
    bindArgument(rel_inputs.kwargs, "#id_kwargs");

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
            rel_inputs[name].forEach(function (id) {
                $("#" + id).removeClass("is-invalid");
            });
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
                    // The server answers a refusal with {"form": {"errors": {field: [messages]}}}. A 400 that did not come from the view
                    // (the page of a proxy or a WAF, Django's own 400 page) has no such body: jQuery only sets responseJSON when the body
                    // parsed as JSON. Each step is read so that it can be missing: with no errors the loop below walks nothing, and the
                    // screen is released like in any other failure (#9017).
                    var errors = ((xhr.responseJSON || {}).form || {}).errors;
                    var $feedback = $('<div class="invalid-feedback"></div>');
                    // Set when a field the page has gets its error: a 400 that marks no field (no errors, or only fields the page does not
                    // show) would leave the user with no text, so it tells the request was refused (#9016).
                    var marked = false;
                    for (var name in errors) {
                        // a field outside the map shows nothing (and is no loop over undefined)
                        var ids = Object.prototype.hasOwnProperty.call(rel_inputs, name) ? rel_inputs[name] : [];
                        ids.forEach(function (id) {
                            var $input = $("#" + id);
                            if ($input.length)
                                marked = true;
                            $input.addClass("is-invalid");
                            // .text(): a validation message echoes back what the user
                            // typed (ast.literal_eval puts the input in its error), so
                            // it must never be parsed as markup. One .clone() per input:
                            // appending the same element to two parents MOVES it, and
                            // it would stay in the last one only. The messages of a field that is no list (null) read as none.
                            $input.parent().append($feedback.clone().text((errors[name] || [])[0]));
                        });
                    }
                    if (!marked)
                        showAlert(config.refusedText);
                    break;
                case 0:  // no answer: the network failed
                    showAlert(config.networkErrorText);
                    break;
                default:
                    // a server error says so; a refusal (the 403 of the CSRF, the 429...) says the command was not run; any other
                    // failure (a 2xx that is no JSON...) shows no text
                    if (xhr.status >= 500)
                        showAlert(config.serverErrorText);
                    else if (xhr.status >= 400)
                        showAlert(config.refusedText);
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
