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
    trackWaits(world, 1);
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

// Os tipos de falha da consulta que chegam ao `.fail` do handler (AC-8967-1).
const FAILURE_KINDS = ['html200', 'http403', 'http404', 'http500', 'network'];

// Conta os avisos do evento `updating` (AC-8967-4 e -5) com um ouvinte de host registrado DEPOIS do da página;
// toda `world.state()` passa a trazer `updatingCalls`, a contagem até aquele momento.
function countUpdating(world) {
    let calls = 0;
    world.cmdasyncform.add_event_callback('updating', function () { calls += 1; });
    const state = world.state;
    world.state = function () {
        return Object.assign(state(), {updatingCalls: calls});
    };
}

// Um ouvinte de host de `update-failed` que lança: a ordem do `empty()` do `.fail` em relação a ele (AC-8967-1).
function hostFailsOnUpdateFailed(world) {
    world.cmdasyncform.add_event_callback('update-failed', function () {
        throw new Error('host callback raised');
    });
}

// A resposta da consulta de estado: 'ready' (pronta, com a saída), 'notready' ou um tipo de STATUS_FAILURES.
function answerAs(world, answer) {
    if (answer === 'ready') world.answerStatus(ready('TASK OUTPUT'));
    else if (answer === 'notready') world.answerStatus(NOT_READY);
    else if (STATUS_FAILURES[answer]) world.answerStatus(STATUS_FAILURES[answer]);
    else throw new Error('harness: unknown answer ' + answer);
}

// `answer` só vale com a consulta em voo (padrão 'ready', como antes); `hostFails` registra o ouvinte que lança.
function abort(trigger, waits, flight, answer, hostFails) {
    const world = createWorld({commandsList: trigger === 'switch'});
    countUpdating(world);
    if (hostFails) hostFailsOnUpdateFailed(world);
    trackWaits(world, waits);
    if (flight) world.advance(1000); // a consulta saiu e a resposta ainda não veio
    const result = {before: world.state()};
    if (trigger === 'cancel') world.click('#btn-task-cancel');
    else world.switchCommand(0);
    result.afterAbort = world.state();
    if (flight) {
        answerAs(world, answer || 'ready');
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

// `runningText` (opcional): o texto de execução que o template publica, como uma tradução (AC-8967-3).
function noAbortControl(waits, runningText) {
    const world = createWorld(runningText ? {runningText: runningText} : undefined);
    countUpdating(world);
    trackWaits(world, waits);
    const result = {before: world.state()};
    world.advance(1000);
    result.after = world.state();
    return result;
}

// AC-8967-4 (controle): sem interrupção, uma resposta «não pronta» e depois a pronta; cada uma avisa o `updating` uma vez.
function followReady() {
    const world = createWorld();
    countUpdating(world);
    world.execute('t1');
    world.answerStatus(NOT_READY);
    const result = {afterNotReady: world.state()};
    world.advance(1000);
    world.answerStatus(ready('TASK OUTPUT'));
    result.afterReady = world.state();
    return result;
}

function clickTrigger(world, trigger) {
    if (trigger === 'cancel') world.click('#btn-task-cancel');
    else world.switchCommand(0);
}

// A resposta de um comando seguinte, para provar que ele é acompanhado até o fim.
function followNext(world, result) {
    world.execute('t2');
    world.answerStatus(ready('SECOND OUTPUT'), 'last');
    result.second = world.state();
}

// AC-8955-1: interromper (Cancel ou troca de comando) com o formulário em voo, antes da resposta do servidor.
// `interrupt` false é o controle: sem a interrupção o mesmo envio consulta.
function abortDuringSubmit(trigger, interrupt) {
    const world = createWorld({commandsList: trigger === 'switch'});
    world.executeStart(); // o formulário está em voo
    const result = {before: world.state()};
    if (interrupt) clickTrigger(world, trigger);
    result.afterAbort = world.state();
    world.formValid('t1'); // o servidor aceitou a tarefa
    result.afterAnswer = world.state();
    world.advance(5000);
    result.afterAdvance = world.state();
    if (interrupt) followNext(world, result);
    return result;
}

// AC-8955-2: interromper com a 1ª consulta de estado em voo; `answer` é a resposta que ela recebe
// ('ready', 'notready' ou um tipo de falha de STATUS_FAILURES, AC-8967-1).
function abortFirstInFlight(trigger, answer) {
    const world = createWorld({commandsList: trigger === 'switch'});
    countUpdating(world);
    world.execute('t1'); // a 1ª consulta está em voo
    const result = {before: world.state()};
    clickTrigger(world, trigger);
    result.afterAbort = world.state();
    answerAs(world, answer);
    result.afterAnswer = world.state();
    world.advance(5000);
    result.afterAdvance = world.state();
    followNext(world, result);
    return result;
}

function revokeUrls(world) {
    return world.revokeRequests().map(function (request) { return request.url; });
}

// Chama o revoke_task() como um script faria (a página só o chama pelo botão, desabilitado na janela do envio).
function scriptRevoke(world) {
    try {
        world.cmdasyncform.revoke_task();
        return null;
    } catch (error) {
        return String(error);
    }
}

// AC-8955-3: o Revoke na janela do envio (a 1ª execução e a 2ª, depois de uma tarefa pronta) e depois da resposta.
function revokeWindow(run) {
    const world = createWorld();
    if (run === 'second') {
        world.execute('t1');
        world.answerStatus(ready('first'));
    }
    world.executeStart();
    const result = {inWindow: world.state()};
    result.clicked = world.click('#btn-task-revoke'); // false: o botão está desabilitado
    result.afterClick = world.state();
    result.scriptThrew = scriptRevoke(world); // a guarda e o aviso
    result.afterScript = world.state();
    world.formValid(run === 'second' ? 't2' : 't1');
    result.afterAnswer = world.state();
    return result;
}

// AC-8955-3: a falha tardia da revogação da execução anterior chega na janela do envio seguinte.
function revokeLateFailureInWindow() {
    const world = createWorld();
    trackWaits(world, 1);
    world.click('#btn-task-revoke'); // o POST fica em voo
    world.advance(1000);
    world.answerStatus(ready('done')); // a tela é liberada pela conclusão
    const result = {released: world.state()};
    world.executeStart(); // novo envio: o Execute desabilitado, nenhuma tarefa aceita
    result.inWindow = world.state();
    world.answerRevoke(REVOKE_FAILURES.http500); // a falha da execução anterior chega agora
    result.afterFailure = world.state();
    return result;
}

// AC-8955-4: o clique no Revoke depois da resposta pede a tarefa dessa execução, e não a anterior.
function revokeAfterAnswer() {
    const world = createWorld();
    world.execute('t1');
    world.answerStatus(ready('first'));
    world.execute('t2');
    const result = {afterAnswer: world.state()};
    result.clicked = world.click('#btn-task-revoke');
    result.afterClick = world.state();
    result.revokeUrls = revokeUrls(world);
    return result;
}

// AC-8955-5: o revoke_task() por script sem tarefa aceita na execução em curso.
function scriptRevokeWithoutTask(kind, template) {
    const world = createWorld(TEMPLATES[template]);
    if (kind === 'second-window' || kind === 'refused') {
        world.execute('t1');
        world.answerStatus(ready('first')); // t1 terminou
    }
    if (kind !== 'before-any-submit') world.executeStart();
    if (kind === 'refused') world.formError({status: 400, responseJSON: {form: {errors: {}}}}); // o servidor recusou
    const result = {before: world.state()};
    result.threw = scriptRevoke(world);
    result.after = world.state();
    result.revokeUrls = revokeUrls(world);
    return result;
}

// Robustez (sem ID): o envio duplicado que a página recusa (o Execute está desabilitado) não mexe no acompanhamento.
function duplicateSubmit(window) {
    const world = createWorld();
    const result = {};
    if (window === 'after-cancel') {
        world.executeStart();
        world.click('#btn-task-cancel');
        result.duplicateProceeded = world.executeStart(); // false: a página o recusa
        world.formValid('t1');
        result.afterAnswer = world.state();
    } else {
        world.execute('t1');
        world.answerStatus(NOT_READY);
        result.duplicateProceeded = world.executeStart();
        result.click = world.click('#btn-task-revoke');
        result.revokeUrls = revokeUrls(world);
        result.afterClick = world.state();
    }
    return result;
}

// Robustez (sem ID): interrompe no envio, o servidor recusa (400), troca de comando, e o comando seguinte é acompanhado.
function refusedAfterInterrupt() {
    const world = createWorld({commandsList: true});
    world.executeStart();
    world.switchCommand(0);
    world.formError({status: 400, responseJSON: {form: {errors: {}}}});
    const result = {afterRefusal: world.state()};
    world.switchCommand(0); // a tela ociosa
    followNext(world, result);
    return result;
}

// Robustez (sem ID): um host que dispara o form-valid sem o ajax() do handler (sem begin()), depois de uma interrupção.
function hostWithoutBegin() {
    const world = createWorld();
    world.executeStart();
    world.click('#btn-task-cancel');
    world.formValid('t1'); // a tela liberada pela interrupção: running e update falsos
    const result = {released: world.state()};
    world.cmdasyncform.on_form_valid({task: {id: 't2', command: {name: 'cmdasync_echo'}}});
    result.afterDirect = world.state();
    world.answerStatus(ready('HOST OUTPUT'), 'last');
    result.afterAnswer = world.state();
    return result;
}

// Robustez (sem ID): a tela liberada por uma interrupção no envio segue pedindo a última tarefa (a fronteira do AC-8955-5).
function releasedBoundary() {
    const world = createWorld();
    world.executeStart();
    world.click('#btn-task-cancel');
    world.formValid('t1');
    const result = {released: world.state()};
    result.threw = scriptRevoke(world);
    result.after = world.state();
    result.revokeUrls = revokeUrls(world);
    return result;
}

// Robustez (sem ID): o ajax() chamado com um objeto sem beforeSubmit.
function ajaxWithoutBeforeSubmit() {
    const world = createWorld();
    const options = world.cmdasyncform.ajax('form#taskform', {});
    const result = {beforeSubmitType: typeof options.beforeSubmit};
    if (typeof options.beforeSubmit === 'function') {
        try {
            result.returnedType = typeof options.beforeSubmit([], null, options);
        } catch (error) {
            result.threw = String(error);
        }
    }
    world.cmdasyncform.update_abort(); // o envio está em curso (o begin() rodou)
    world.cmdasyncform.on_form_valid({task: {id: 't9', command: {name: 'cmdasync_echo'}}});
    result.afterFormValid = world.state();
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

// ---------------------------------------------------------------- a consulta de workers (celery.workers.status.js, #8954)
const WORKERS_FORBIDDEN = jsonResponse(403, {status: false, workers: []});
const WORKERS_NOBODY = jsonResponse(200, {status: false, workers: [], message: 'no Celery worker answered'});
const WORKERS_LISTED = jsonResponse(200, {status: true, workers: ['celery@w1', '<b>w2</b>']});
const WORKERS_FAILURES = {
    nobody: WORKERS_NOBODY,
    network: {status: 0, body: ''},
    http500: {status: 500, body: '<html>boom</html>'},
    http502: {status: 502, body: '<html>Bad Gateway</html>'},
};

// As falhas que a consulta recebe antes da recusa: a sessão pode cair no meio do acompanhamento.
function workersPrior(world, prior) {
    (prior === 'first' ? [] : prior.split('+')).forEach(function (kind) {
        world.answerWorkers(WORKERS_FAILURES[kind]);
        world.advance(5000); // a consulta refeita sai
    });
}

// AC-8954-1 e -2: o 403, na 1ª consulta ou depois de consultas que falharam ou não acharam worker.
function workersForbidden(prior, options) {
    const world = createWorld(Object.assign({workers: 'page'}, options || {}));
    workersPrior(world, prior);
    const result = {beforeForbidden: world.state()};
    world.answerWorkers(WORKERS_FORBIDDEN);
    result.afterForbidden = world.state();
    world.advance(30000);
    result.later = world.state();
    return result;
}

// AC-8954-3 e -8: o 200 que não é JSON (a tela de login), na 1ª consulta ou depois de uma falha.
function workersLogin(prior) {
    const world = createWorld({workers: 'page'});
    workersPrior(world, prior);
    const result = {beforeLogin: world.state()};
    world.answerWorkers(LOGIN_PAGE);
    result.afterLogin = world.state();
    world.advance(30000);
    result.later = world.state();
    return result;
}

// AC-8954-8 e o controle do AC-8954-6: a consulta chamada sem argumento (o setting desligado: a página não a abre).
function workersWithoutArgument(response) {
    const world = createWorld({workers: 'manual'});
    const result = {before: world.state()};
    world.workersCheck();
    result.asked = world.state();
    world.answerWorkers(response);
    result.afterAnswer = world.state();
    world.advance(4999);
    result.at4999 = world.state();
    world.advance(1);
    result.at5000 = world.state();
    return result;
}

// AC-8954-6: a falha que refaz a consulta 5 s depois; `how`: a página, a chamada sem argumento ou o handler sem `failure_reason`.
function workersRetry(kind, how) {
    const options = how === 'page' ? {workers: 'page'} : (how === 'noarg' ? {workers: 'manual'} : {workers: 'page', noFailureReason: true});
    const world = createWorld(options);
    if (how === 'noarg') world.workersCheck();
    world.answerWorkers(WORKERS_FAILURES[kind]);
    const result = {afterAnswer: world.state()};
    world.advance(4999);
    result.at4999 = world.state();
    world.advance(1);
    result.at5000 = world.state();
    return result;
}

// AC-8954-7: o resultado no aviso (a lista como texto, ou a `message` do `status` falso).
function workersResult(kind) {
    const world = createWorld({workers: 'page'});
    world.answerWorkers(kind === 'listed' ? WORKERS_LISTED : WORKERS_NOBODY);
    const result = {afterAnswer: world.state()};
    world.advance(30000);
    result.later = world.state();
    return result;
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
    register('revoke:' + kind + ':full:late', function () { return revokeFailure(kind, 'full', 'late'); });
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
register('abort:none:1', function () { return noAbortControl(1); });
// AC-8967-3: o template publica o texto de execução traduzido; é ele que fica no painel enquanto o acompanhamento segue
register('abort:none:1:translated', function () { return noAbortControl(1, 'Executando comando...'); });

// AC-8967-1: a consulta em voo que termina depois da interrupção por resposta «não pronta» ou por falha
['cancel', 'switch'].forEach(function (trigger) {
    [2, 3].forEach(function (waits) {
        register('abort:' + trigger + ':' + waits + ':flight:notready', function () { return abort(trigger, waits, true, 'notready'); });
        FAILURE_KINDS.forEach(function (kind) {
            register('abort:' + trigger + ':' + waits + ':flight:fail:' + kind, function () { return abort(trigger, waits, true, kind); });
        });
    });
    FAILURE_KINDS.forEach(function (kind) {
        register('first:' + trigger + ':fail:' + kind, function () { return abortFirstInFlight(trigger, kind); });
    });
});
// ... com um ouvinte de host de `update-failed` que lança: o painel já está vazio quando ele lança
register('abort:cancel:2:flight:fail:callback-throws', function () { return abort('cancel', 2, true, 'http403', true); });
// AC-8967-4 (controle): sem interrupção, um aviso de `updating` por resposta
register('follow:ready', followReady);

// AC-8955-1 e -2
['cancel', 'switch'].forEach(function (trigger) {
    register('submit:' + trigger, function () { return abortDuringSubmit(trigger, true); });
    ['notready', 'ready'].forEach(function (answer) {
        register('first:' + trigger + ':' + answer, function () { return abortFirstInFlight(trigger, answer); });
    });
});
register('submit:none', function () { return abortDuringSubmit('cancel', false); });

// AC-8955-3, -4 e -5
['first', 'second'].forEach(function (run) {
    register('revoke-window:' + run, function () { return revokeWindow(run); });
});
register('revoke-window:late-failure', revokeLateFailureInWindow);
register('revoke:after-answer', revokeAfterAnswer);
['before-any-submit', 'first-window', 'second-window', 'refused'].forEach(function (kind) {
    register('script-revoke:' + kind + ':full', function () { return scriptRevokeWithoutTask(kind, 'full'); });
});
register('script-revoke:first-window:nospace', function () { return scriptRevokeWithoutTask('first-window', 'nospace'); });

// Robustez (sem ID)
register('robust:duplicate:after-cancel', function () { return duplicateSubmit('after-cancel'); });
register('robust:duplicate:following', function () { return duplicateSubmit('following'); });
register('robust:refused-after-interrupt', refusedAfterInterrupt);
register('robust:host-without-begin', hostWithoutBegin);
register('robust:released-boundary', releasedBoundary);
register('robust:ajax-without-before-submit', ajaxWithoutBeforeSubmit);

['submit', 'cancel', 'switch', 'revoke', 'none'].forEach(function (trigger) {
    register('clear:' + trigger, function () { return clearAlert(trigger); });
});

register('done:output', function () { return done('output'); });
register('done:traceback', function () { return done('traceback'); });
register('form:login200', formLogin);
register('boot:list', function () { return boot(true); });
register('boot:text', function () { return boot(false); });

// AC-8954-1, -2 (o 403) e -3 (a tela de login): na 1ª consulta e depois de falhas
['first', 'network', 'nobody', 'nobody+network'].forEach(function (prior) {
    register('workers:forbidden:' + prior, function () { return workersForbidden(prior); });
    register('workers:login:' + prior, function () { return workersLogin(prior); });
});
// AC-8954-5: o host sem a frase, sem o plugin de toast e sem o #popup-msg
['nophrase', 'noplugin', 'none'].forEach(function (toast) {
    register('workers:host:' + toast, function () { return workersForbidden('first', {toast: toast}); });
});
// AC-8954-8 (o login sem destino) e o controle do AC-8954-6 (a chamada sem argumento)
register('workers:noarg:login', function () { return workersWithoutArgument(LOGIN_PAGE); });
register('workers:noarg:network', function () { return workersWithoutArgument(WORKERS_FAILURES.network); });
// AC-8954-6: refaz em 5 s
Object.keys(WORKERS_FAILURES).forEach(function (kind) {
    register('workers:retry:' + kind, function () { return workersRetry(kind, 'page'); });
});
register('workers:retry:network:noreason', function () { return workersRetry('network', 'noreason'); });
// AC-8954-7: o resultado no aviso
register('workers:result:listed', function () { return workersResult('listed'); });
register('workers:result:nobody', function () { return workersResult('nobody'); });
// AC-8954-9: a tela com a consulta de workers ligada
register('boot:workers', function () {
    const world = createWorld({workers: 'page'});
    return {state: world.state(), boot: world.boot()};
});

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
