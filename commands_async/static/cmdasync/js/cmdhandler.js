/**
 * Everything a command prints is untrusted input.
 *
 * A management command's stdout can carry anything the system logged, including
 * data submitted by third parties -- `read_logs` is the example in the project's
 * own admin action. Handing that to jQuery as a string parses it as HTML, so it
 * only ever reaches the DOM through a text node.
 */
function cmdasyncTextNode(value) {
    return document.createTextNode(value === null || value === undefined ? '' : String(value));
}

cmdasyncform = {
    csrfSafeMethod: function(method) {
        // these HTTP methods do not require CSRF protection
        return (/^(GET|HEAD|OPTIONS|TRACE)$/.test(method));
    },
    getCookie: function(name) {
      var start = document.cookie.indexOf(name + "=");
      var len = start + name.length + 1;
      if ((!start) && (name !== document.cookie.substring(0, name.length))) {
        return null;
      }
      if (start === -1) return null;
      var end = document.cookie.indexOf(';', len);
      if (end === -1) end = document.cookie.length;
      return document.cookie.substring(len, end);
    },
    selectText: function(element) {
        element = element.replace(/^#+/, "");
        var doc = document;
        var text = doc.getElementById(element);
        var range, selection;
        if (doc.body.createTextRange) {
            range = doc.body.createTextRange();
            range.moveToElementText(text);
            range.select();
        } else if (window.getSelection) {
            selection = window.getSelection();
            range = document.createRange();
            range.selectNodeContents(text);
            selection.removeAllRanges();
            selection.addRange(range);
        }
    },
    copyToClipboard: function(element) {
        this.selectText(element);
        document.execCommand("copy");
    },
    init: function (outputelem, debug) {
        this.$output = $(outputelem);
        this.update = true;
        this.running = false;
        this.debug = debug || false;
        this.events = {
            'form-valid': [],
            'update-finish': [],
            'updating': [],
            // the status query was refused or never got a JSON answer; the argument says why
            // ('login', 'forbidden' or 'not-found')
            'update-failed': [],
            // the revocation request failed; the argument is the text the server sent, or null
            'revoke-failed': []
        };
        this.$form = null;
        this.task = null;
        this.requestTimeout = null;
    },
    request: function(task) {
        if (!this.running)
            this.update = true;
        var self = this;
        // dataType json: a page that is not JSON (the login page an expired session
        // gets after the redirect) lands in .fail as a parsererror instead of in
        // .done, where there is no task to read.
        var req = $.ajax({
            type: "get",
            url: "status/" + task.id,
            cache: false,
            dataType: "json"
        });
        req.done(function (data) {
            var task = data.task;
            var $output = self.$output;
            self.exc_event_callbacks('updating', self.$form);
            if (self.update) {
                if (task.ready) {
                    $output.empty();
                    if (!task.failed) {
                        $output.append(cmdasyncTextNode(task.output))
                    } else {
                        $output.append(cmdasyncTextNode(task.traceback))
                    }
                    if (self.debug) {
                        $output.prepend(cmdasyncTextNode("arguments(" + self.$form.serialize() + ")\n"));
                        $output.prepend(cmdasyncTextNode("command(" + self.task.command.name + ")\n"));
                        $output.prepend(cmdasyncTextNode("task-id(" + task.id + ")\n"));
                    }
                    self.running = false;
                    self.exc_event_callbacks('update-finish', self.$form);
                } else {
                    // new update check; the timer is kept on the form (not on `this`,
                    // which inside this callback is the ajax settings) so update_abort
                    // can cancel it
                    self.requestTimeout = setTimeout(function () {
                        self.requestTimeout = null;
                        self.running = true;
                        self.request(task)
                    }, 1000);
                }
            } else {
                self.running = false;
                self.exc_event_callbacks('update-finish', self.$form);
            }
        });
        req.fail(function (xhr, textStatus) {
            self.running = false;
            try {
                var reason = self.failure_reason(xhr, textStatus);
                if (reason)
                    self.exc_event_callbacks('update-failed', reason);
            } finally {
                // whatever the page does with the reason, the screen is released
                self.exc_event_callbacks('update-finish', self.$form);
            }
        });
    },
    failure_reason: function (xhr, textStatus) {
        // 200 that is not JSON: the browser followed the login redirect
        if (textStatus === 'parsererror' && xhr.status === 200)
            return 'login';
        if (xhr.status === 403)
            return 'forbidden';
        if (xhr.status === 404)
            return 'not-found';
        // server errors and network failures stop the follow-up without a reason
        return null;
    },
    revoke_task : function () {
        var self = this;
        var csrftoken = this.getCookie("csrftoken");
        $.ajax({
            type: "POST",
            dataType: 'json',
            url: "status/" + self.task.id,
            beforeSend: function(xhr, settings) {
                if (!self.csrfSafeMethod(settings.type) && !this.crossDomain) {
                    xhr.setRequestHeader("X-CSRFToken", csrftoken);
                }
            },
            success: function(data) {
                if (!data.status)
                    self.exc_event_callbacks('revoke-failed', data.error || null);
            },
            // HTTP error, or a 200 that is not JSON (parsererror, because of dataType)
            error: function() {
                self.exc_event_callbacks('revoke-failed', null);
            }
        })
    },
    clear_output: function () {
      this.$output.empty();
    },

    update_abort: function () {
        if (this.requestTimeout !== null) {
            // A new query is scheduled: end the follow-up whole. Nobody else would
            // release the screen (the query that did it never goes out), and
            // `running` has to be clean for the next command to be followed.
            clearTimeout(this.requestTimeout);
            this.requestTimeout = null;
            this.running = false;
            this.update = true;
            this.exc_event_callbacks('update-finish', this.$form);
            return;
        }
        // A query in flight (or the form being submitted): its answer decides.
        this.update = !this.running;
    },

    add_event_callback: function(name, fn) {
        this.events[name].push(fn);
    },

    on_form_valid : function (data) {
        this.task = data.task;
        this.request(this.task);
    },

    exc_event_callbacks: function(name) {
        var event_callbacks = this.events[name];
        var length = event_callbacks.length;
            for (var i=0; i < length; i++) {
                event_callbacks[i].apply(this, Array.prototype.slice.call(arguments, 1));
            }
    },
    ajax: function (elem, options) {
        var self = this;
        options = options || {};
        options.dataType = 'json';

        if (this.events['form-valid'].indexOf(this.on_form_valid) === -1)
            this.add_event_callback("form-valid", this.on_form_valid.bind(this));

        options.success = function(responseText, statusText, xhr, $form) {
            self.exc_event_callbacks('form-valid', responseText, statusText, xhr, $form);
        };
        this.$form = $(elem).ajaxForm(options);
        return options;
    }
};
