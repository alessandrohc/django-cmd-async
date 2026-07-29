var workers_check_status = function() {
    var csrftoken = cmdasyncform.getCookie("csrftoken");
    var $toast = $('#popup-msg');
    $.ajax({
        type: "GET",
        dataType: 'json',
        url: "workers/status/",
        beforeSend: function(xhr, settings) {
            if (!cmdasyncform.csrfSafeMethod(settings.type) && !this.crossDomain) {
                xhr.setRequestHeader("X-CSRFToken", csrftoken);
            }
        }
    }).done(function(data) {
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
            setTimeout(workers_check_status, 5000);
        }
        $toast.toast("show");
    }).fail(function(){
        setTimeout(workers_check_status, 5000);
    })
};