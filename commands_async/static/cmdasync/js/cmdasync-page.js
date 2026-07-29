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
        runningText: $panel.data('running-text')
    };

    // Maps a form field to the visible input that mirrors it, for error placement.
    var rel_inputs = {
        'args': 'task-args',
        'kwargs': 'task-kwargs',
        'app_command': 'task-app-command'
    };

    if (config.workersStatusCheck) {
        workers_check_status();
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
            // stop status update
            cmdasyncform.update_abort();
            cmdasyncform.clear_output();
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
        cmdasyncform.update_abort();
    });

    $("#btn-task-revoke").on('click', function () {
        var $btnRevoke = $(this).prop('disabled', true);
        $btnRevoke.find(".task-progress").removeClass("d-none");
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
            $form.find("#btn-task-cancel").prop('disabled', false);
            $form.find("#btn-task-revoke").prop('disabled', false);
        },
        error: function (xhr) {
            requestErrorClean();
            switch (xhr.status) {
                case 200:  // need login
                    var url = [config.loginUrl];
                    url.push("?" + config.redirectField + "=");
                    url.push(config.indexUrl);
                    window.location = url.join("");
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
            }
            requestUpdateFinish();
        }
    });

    cmdasyncform.add_event_callback("updating", function ($form) {
        $("#task-output").text(config.runningText);
    });

    cmdasyncform.add_event_callback("form-valid", function (responseText, statusText, xhr, $form) {
        requestErrorClean();
    });

    cmdasyncform.add_event_callback("update-finish", function (form) {
        requestUpdateFinish();
    });
});
