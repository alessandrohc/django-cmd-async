// Tells an answer with the form the server sends from one without it: an object with `status` and, with a true `status`, the
// list `workers` (`{"status": true, "workers": [...]}` or `{"status": false, "workers": [], "message": "..."}`). Anything else
// (no data, as with a 204 or a 304; JSON that is not an object or has no `status`; a true `status` whose `workers` is not a
// list) would raise reading it, or show an empty notice. A `status` that exists is read as it always was.
function cmdasyncIsWorkersAnswer(data) {
    return !!data && data.status !== undefined && (!data.status || Array.isArray(data.workers));
}

// options.onLogin (optional): what the page does when the answer is the login page, so a session that fell in
// the middle of the probe takes the user to the login. Every new round carries the same options.
var workers_check_status = function(options) {
    options = options || {};
    var csrftoken = cmdasyncform.getCookie("csrftoken");
    var $toast = $('#popup-msg');
    var again = function () {
        setTimeout(function () { workers_check_status(options); }, 5000);
    };
    $.ajax({
        type: "GET",
        dataType: 'json',
        url: "workers/status/",
        // a query with no answer in this time is cut (jQuery: 'timeout', status 0) and the probe asks again like for any
        // failure; read at each round, so the value a host assigned is the one used (0 turns the cut off)
        timeout: cmdasyncform.query_timeout,
        beforeSend: function(xhr, settings) {
            if (!cmdasyncform.csrfSafeMethod(settings.type) && !this.crossDomain) {
                xhr.setRequestHeader("X-CSRFToken", csrftoken);
            }
        }
    }).done(function(data) {
        // An answer without the form the server sends (a 204 or a 304, which jQuery hands success with no data, JSON that is not
        // an object or has no `status`, or a true `status` with no list of workers) is a query that failed: the notice stays as it
        // was, and the probe asks again like for any failure (the same branch as .fail). It is checked before the notice is touched.
        if (!cmdasyncIsWorkersAnswer(data)) {
            again();
            return;
        }
        var $body = $toast.find(".toast-body").empty();
        if (data.status) {
            $toast.find('.online').text(data.workers.length);
            // Worker names and broker errors come from outside this process: build
            // the element and set its text, never concatenate them into markup.
            $.each(data.workers, function (i, name) {
                $body.append($('<p class="text-muted"></p>').text(name))
            })
        } else {
            $body.append($('<p class="text-muted"></p>').text(data.message));
            again();
        }
        $toast.toast("show");
    }).fail(function(xhr, textStatus){
        // failure_reason belongs to cmdhandler.js: a host with an older one does not have it, and then
        // every failure asks again, as it always did
        var reason = typeof cmdasyncform.failure_reason === 'function' ? cmdasyncform.failure_reason(xhr, textStatus) : null;
        if (reason === 'login') {
            // A layer outside the route answered with the login page: the session is gone. The page takes the
            // user to the login (when it says where), and no new query goes out.
            if (options.onLogin)
                options.onLogin();
        } else if (reason === 'forbidden') {
            // Refused for good: waiting does not grant the permission, so no new query goes out. The notice opens
            // only with the phrase the page published and the Bootstrap toast plugin ($.fn.toast does not exist
            // without the bundle, and calling it would raise); the phrase takes the place of what it showed.
            var text = $toast.data('forbidden-text');
            if (text && typeof $toast.toast === 'function') {
                $toast.find(".toast-body").empty().append($('<p class="text-muted"></p>').text(text));
                $toast.toast("show");
            }
        } else {
            // network failure (the cut included), server error, not found: they pass, so the probe asks again
            again();
        }
    })
};