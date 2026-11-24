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
    // How long (ms) a status query, and the workers probe, waits for an answer before it is cut and told as a network
    // failure ('network'); 0 turns the cut off, as in jQuery. The default lives here and not in init(): a host that
    // assigns it before or after init() is served, and init() never undoes it. Read when each query goes out.
    query_timeout: 30000,
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
            // the status query failed; the argument says why ('login', 'forbidden', 'not-found', 'server' or
            // 'network', see failure_reason)
            'update-failed': [],
            // the revocation request failed; the argument is the text the server sent, or null
            'revoke-failed': []
        };
        this.$form = null;
        this.task = null;
        this.requestTimeout = null;
        // The bound on_form_valid that ajax() registers lives and dies with the lists above: after a
        // new init() the next ajax() binds the on_form_valid the object has then.
        this.form_valid_callback = null;
    },
    // The follow-up starts when the form is sent, not at the first poll: from here Cancel and the
    // command switch have something to end (update_abort reads `running`), and no task is known yet.
    // `task` is cleared so that the task of an earlier run is never the one revoked in this run.
    begin: function () {
        this.running = true;
        this.update = true;
        this.task = null;
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
            dataType: "json",
            // a query with no answer in this time is aborted by jQuery as a 'timeout' (status 0), which
            // failure_reason tells as 'network'
            timeout: self.query_timeout
        });
        req.done(function (data) {
            var task = data.task;
            var $output = self.$output;
            if (self.update) {
                // `updating` only while the follow-up goes on: the page writes the running text in
                // the panel on it, and after an interruption that text would be left in the
                // released screen (with the Copy button over it)
                self.exc_event_callbacks('updating', self.$form);
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
                // interrupted: the screen is released with an empty panel, before `update-finish`
                // (the page shows the Copy button by what the panel holds when it is released)
                $output.empty();
                self.running = false;
                self.exc_event_callbacks('update-finish', self.$form);
            }
        });
        req.fail(function (xhr, textStatus) {
            self.running = false;
            // Whatever ended the query, the panel is emptied: with the follow-up interrupted (as in .done) or going on, where the
            // failure belies the running text the last answer left there (the page shows the Copy button by what the panel holds).
            // It is emptied before the callbacks: one that raises must not skip it, and what a host writes in the panel on
            // `update-failed` or `update-finish` stays.
            self.$output.empty();
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
        // no answer at all: the network failed, or jQuery cut the query at query_timeout (status 0)
        if (xhr.status === 0)
            return 'network';
        // any other failure: an HTTP error (400, 401, 405, 429, a 5xx, an HTML error page) or a 2xx with a body that is
        // not JSON. The user needs the same thing for all of them, so the reason is not told by status
        return 'server';
    },
    revoke_task : function () {
        var self = this;
        // No task was accepted by the server in this run (the form is still being sent, or was
        // refused): there is nothing to revoke. It is told as a failed revocation, so a page that
        // enabled its button early ends the click like any other failure.
        if (!self.task) {
            self.exc_event_callbacks('revoke-failed', null);
            return;
        }
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
            // the panel still holds the running text of the last query: empty it before the screen
            // is released
            this.$output.empty();
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
        if (this.running && !this.update) {
            // The follow-up was interrupted while the form was being sent: end it here. No query
            // goes out; the task keeps running (Revoke is what stops it). `running` is part of the
            // test so that a host that never went through ajax() (no begin()) follows as before.
            this.running = false;
            this.exc_event_callbacks('update-finish', this.$form);
            return;
        }
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

        // bind() returns a new function on every call, so a test against `this.on_form_valid` never
        // found the one an earlier call had registered: each ajax() call added another follow-up per
        // task. The bound function is made once and looked up in the current list.
        if (!this.form_valid_callback)
            this.form_valid_callback = this.on_form_valid.bind(this);
        if (this.events['form-valid'].indexOf(this.form_valid_callback) === -1)
            this.add_event_callback("form-valid", this.form_valid_callback);

        // The follow-up begins with the submit. The page's own beforeSubmit may refuse the submit
        // (it returns false, e.g. a second click while one runs): then nothing begins and the
        // follow-up in course is left alone, exactly as jquery.form aborts the submit.
        var pageBeforeSubmit = options.beforeSubmit;
        options.beforeSubmit = function () {
            var proceed = pageBeforeSubmit ? pageBeforeSubmit.apply(this, arguments) : undefined;
            if (proceed !== false)
                self.begin();
            return proceed;
        };

        options.success = function(responseText, statusText, xhr, $form) {
            self.exc_event_callbacks('form-valid', responseText, statusText, xhr, $form);
        };
        this.$form = $(elem).ajaxForm(options);
        return options;
    }
};
