'use strict';
/**
 * Cenários da tela de comandos em node: cada um monta um mundo novo (world.js), conduz o usuário e as
 * respostas do servidor e devolve o que a tela mostra. A saída é UM JSON na última linha do stdout;
 * o teste de Python (test_page_script.py) afirma sobre ele, um método por critério.
 *
 * Uso: node scenarios.js
 */
const {createWorld, TEXTS, PAGE} = require('./world.js');

const NOT_READY = {status: 200, body: {task: {id: 't1', ready: false, failed: false}}};
const ready = function (output) {
    return {status: 200, body: {task: {id: 't1', ready: true, failed: false, output: output}}};
};
const failedTask = function (traceback) {
    return {status: 200, body: {task: {id: 't1', ready: true, failed: true, traceback: traceback}}};
};
const jsonResponse = function (status, body) {
    return {status: status, body: body};
};
// O navegador segue o 302 do login_required e entrega o HTML do login com 200.
const LOGIN_PAGE = {status: 200, body: '<html><title>Login</title></html>'};

const STATUS_FAILURES = {
    html200: LOGIN_PAGE,
    html502: {status: 502, body: '<html>Bad Gateway</html>'},
    http403: jsonResponse(403, {task: null}),
    http404: jsonResponse(404, {task: null}),
    http429: {status: 429, body: 'Too Many Requests'},
    http500: {status: 500, body: '<html>boom</html>'},
    network: {status: 0, body: ''},
};

const SERVER_ERROR_TEXT = 'Server said: <b>no</b>';
const REVOKE_FAILURES = {
    http403: jsonResponse(403, {task: null}),
    http404: jsonResponse(404, {task: null}),
    http500: {status: 500, body: '<html>boom</html>'},
    html200: LOGIN_PAGE,
    falseWithError: jsonResponse(200, {status: false, error: SERVER_ERROR_TEXT}),
    falseNoError: jsonResponse(200, {status: false}),
};

// full = o template do pacote; nospace = um host sem o espaço do aviso nem os textos;
// spaceonly = com o espaço, sem os textos do cliente.
const TEMPLATES = {
    full: {},
    nospace: {alertSpace: false, texts: false},
    spaceonly: {alertSpace: true, texts: false},
};

// Envia o comando e passa por `waits` respostas "não pronta"; no fim há uma nova consulta AGENDADA (timer vivo).
function trackWaits(world, waits) {
    world.execute('t1');
    for (let i = 0; i < waits; i++) {
        world.answerStatus(NOT_READY);
        if (i < waits - 1) world.advance(1000);
    }
}

function revokeFailure(kind, template, phase) {
    const world = createWorld(TEMPLATES[template]);
    const result = {};
    if (phase === 'window') {
        // a janela entre o clique em Execute e o form-valid (precisa de uma execução anterior: self.task)
        world.execute('t1');
        world.answerStatus(ready('first'));
        world.executeStart();
    } else {
        trackWaits(world, 1);
    }
    result.before = world.state();
    world.click('#btn-task-revoke');
    result.clicked = world.state(); // o botão Revoke desabilitado, com o indicador, à espera do servidor
    if (phase === 'late') {
        // a tela é liberada (a tarefa terminou) antes de a falha da revogação chegar
        world.advance(1000);
        world.answerStatus(ready('done'));
        result.released = world.state();
    }
    world.answerRevoke(REVOKE_FAILURES[kind]);
    result.after = world.state();
    return result;
}

function revokeAccepted() {
    const world = createWorld();
    trackWaits(world, 1);
    const result = {before: world.state()};
    world.click('#btn-task-revoke');
    world.answerRevoke(jsonResponse(200, {status: true}));
    result.afterAccepted = world.state();
    world.advance(1000);
    world.answerStatus(ready('done'));
    result.afterRelease = world.state();
    return result;
}

function statusFailure(kind, template) {
    const world = createWorld(TEMPLATES[template]);
    world.execute('t1');
    world.answerStatus(STATUS_FAILURES[kind]);
    const result = {after: world.state()};
    world.advance(5000);
    result.later = world.state();
    return result;
}

// Um callback de update-failed de outro código (um host) que lança: a tela tem de ser liberada mesmo assim.
function throwingCallback() {
    const world = createWorld();
    world.cmdasyncform.add_event_callback('update-failed', function () {
        throw new Error('host callback raised');
    });
    world.execute('t1');
    world.answerStatus(STATUS_FAILURES.http403);
    return {after: world.state()};
}

// A sessão expira no meio do acompanhamento: a consulta que sai do timer, depois de respostas "não pronta", recebe o login.
function statusFailureMidway(kind) {
    const world = createWorld();
    trackWaits(world, 2);
    world.advance(1000); // a 3ª consulta sai
    world.answerStatus(STATUS_FAILURES[kind]);
    const result = {after: world.state()};
    world.advance(5000);
    result.later = world.state();
    return result;
}

function abort(trigger, waits, flight) {
    const world = createWorld({commandsList: trigger === 'switch'});
    trackWaits(world, waits);
    if (flight) world.advance(1000); // a consulta saiu e a resposta ainda não veio
    const result = {before: world.state()};
    if (trigger === 'cancel') world.click('#btn-task-cancel');
    else world.switchCommand(0);
    result.afterAbort = world.state();
    if (flight) {
        world.answerStatus(ready('TASK OUTPUT'));
        result.afterAnswer = world.state();
    } else {
        world.advance(5000);
        result.afterAdvance = world.state();
        // o comando seguinte tem de ser acompanhado até o fim
        world.execute('t2');
        world.answerStatus(ready('SECOND OUTPUT'), 'last');
        result.second = world.state();
    }
    return result;
}

function noAbortControl(waits) {
    const world = createWorld();
    trackWaits(world, waits);
    const result = {before: world.state()};
    world.advance(1000);
    result.after = world.state();
    return result;
}

function cancelWithoutTimer(phase) {
    const world = createWorld();
    if (phase === 'first-inflight') world.execute('t1'); // a 1ª consulta está em voo
    else world.executeStart(); // o envio do formulário em curso
    const result = {before: world.state()};
    world.click('#btn-task-cancel');
    result.after = world.state();
    return result;
}

function clearAlert(trigger) {
    const world = createWorld({commandsList: trigger === 'switch'});
    let before;
    if (trigger === 'submit') {
        world.execute('t1');
        world.answerStatus(jsonResponse(404, {task: null})); // o aviso «Task not found.» e a tela liberada
        before = world.state();
        world.executeStart();
    } else {
        trackWaits(world, 1);
        world.click('#btn-task-revoke');
        world.answerRevoke(REVOKE_FAILURES.http403); // o aviso da falha, com o acompanhamento em curso
        before = world.state();
        if (trigger === 'cancel') world.click('#btn-task-cancel');
        else if (trigger === 'switch') world.switchCommand(0);
        else if (trigger === 'revoke') world.click('#btn-task-revoke');
    }
    return {before: before, after: world.state()};
}

function done(kind) {
    const world = createWorld();
    world.execute('t1');
    world.answerStatus(kind === 'output' ? ready('<b>bold</b> output') : failedTask('Traceback <i>x</i>'));
    return {after: world.state()};
}

function formLogin() {
    const world = createWorld();
    world.executeStart();
    world.formError({status: 200}); // o ajaxForm (dataType json) recebeu o HTML do login: cai no error
    // o dataType que o cmdasyncform.ajax() entregou ao ajaxForm: é ele que manda o HTML do login ao error
    return {after: world.state(), dataType: world.ajaxForm.dataType || null};
}

function boot(commandsList) {
    const world = createWorld({commandsList: commandsList});
    return {state: world.state(), boot: world.boot()};
}

const scenarios = {};
function register(name, fn) {
    try {
        scenarios[name] = fn();
    } catch (error) {
        scenarios[name] = {harnessError: error && error.stack ? error.stack : String(error)};
    }
}

Object.keys(REVOKE_FAILURES).forEach(function (kind) {
    Object.keys(TEMPLATES).forEach(function (template) {
        register('revoke:' + kind + ':' + template + ':tracking', function () { return revokeFailure(kind, template, 'tracking'); });
    });
    ['late', 'window'].forEach(function (phase) {
        register('revoke:' + kind + ':full:' + phase, function () { return revokeFailure(kind, 'full', phase); });
    });
});
register('revoke:accepted', revokeAccepted);

Object.keys(STATUS_FAILURES).forEach(function (kind) {
    Object.keys(TEMPLATES).forEach(function (template) {
        register('status:' + kind + ':' + template, function () { return statusFailure(kind, template); });
    });
});

register('status:callback-throws', throwingCallback);

register('status:html200:midway', function () { return statusFailureMidway('html200'); });

['cancel', 'switch'].forEach(function (trigger) {
    [1, 2, 3].forEach(function (waits) {
        register('abort:' + trigger + ':' + waits + ':timer', function () { return abort(trigger, waits, false); });
    });
    [2, 3].forEach(function (waits) {
        register('abort:' + trigger + ':' + waits + ':flight', function () { return abort(trigger, waits, true); });
    });
});
register('abort:none:2', function () { return noAbortControl(2); });
['first-inflight', 'submitting'].forEach(function (phase) {
    register('abort:cancel:' + phase, function () { return cancelWithoutTimer(phase); });
});

['submit', 'cancel', 'switch', 'revoke', 'none'].forEach(function (trigger) {
    register('clear:' + trigger, function () { return clearAlert(trigger); });
});

register('done:output', function () { return done('output'); });
register('done:traceback', function () { return done('traceback'); });
register('form:login200', formLogin);
register('boot:list', function () { return boot(true); });
register('boot:text', function () { return boot(false); });

const initial = createWorld().state();
process.stdout.write(JSON.stringify({
    constants: {
        texts: TEXTS,
        page: PAGE,
        serverErrorText: SERVER_ERROR_TEXT,
        initialButtons: {
            execute: initial.execute.disabled,
            cancel: initial.cancel.disabled,
            revoke: initial.revoke.disabled,
        },
    },
    scenarios: scenarios,
}) + '\n');
