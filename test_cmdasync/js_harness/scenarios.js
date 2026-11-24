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
    // #8961 (AC-8961-1): as outras falhas HTTP da consulta; o `html202` é um 2xx que não é 200 nem 204 com corpo que não é JSON
    // (o que o jQuery manda ao `.fail` como `parsererror`; o 204 é sucesso «nocontent» no jQuery e fica fora, follow-up F3)
    http400: jsonResponse(400, {task: null}),
    http401: {status: 401, body: ''},
    http405: {status: 405, body: ''},
    http503: jsonResponse(503, {task: null}),
    html202: {status: 202, body: '<html>accepted</html>'},
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

// Os modelos dos cenários de falha do #8961: os de sempre e os de um host que traz só parte dos dois textos novos
// (oldtexts: o espaço do aviso e os três textos antigos, sem os novos; noserver: só o de conexão; nonetwork: só o de servidor).
const FAILURE_TEMPLATES = Object.assign({}, TEMPLATES, {
    oldtexts: {failureTexts: 'none'},
    noserver: {failureTexts: 'network'},
    nonetwork: {failureTexts: 'server'},
});

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
    const world = createWorld(FAILURE_TEMPLATES[template]);
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

// #9005 (AC-9005-1 a -5): a consulta de estado FALHA no meio do acompanhamento, sem interrupção: depois de `waits` respostas «não pronta»
// a consulta seguinte sai (a 2ª com waits = 1, a 3ª com waits = 2; a 1ª, com waits = 0, só o AC-9005-5 a usa) e falha. `kind` é um
// tipo de STATUS_FAILURES ou 'cut', o corte real:
// a consulta fica sem resposta e o `$.ajax` a corta aos 30 s (o caminho do timer, outro que o da resposta `status: 0` do tipo `network`).
// `options.world` são as opções do mundo (o texto de execução traduzido, o template sem o aviso); `options.host` registra um ouvinte
// de host ('throws', 'writes-failed' ou 'writes-finish'); `options.next` envia o comando seguinte, que tem de ser acompanhado até o fim.
// A tela traz `updatingCalls` (os avisos de `updating` até aquele momento).
const MIDWAY_HOST_TEXT = 'HOST TEXT';
const TRANSLATED_RUNNING_TEXT = 'Executando comando...';
const MIDWAY_KINDS = Object.keys(STATUS_FAILURES).concat(['cut']);

function hostWritesInPanel(world, event) {
    world.cmdasyncform.add_event_callback(event, function () {
        world.cmdasyncform.$output.text(MIDWAY_HOST_TEXT);
    });
}

function midwayFailure(kind, waits, options) {
    options = options || {};
    const world = createWorld(options.world);
    countUpdating(world);
    if (options.host === 'throws') hostFailsOnUpdateFailed(world);
    else if (options.host === 'writes-failed') hostWritesInPanel(world, 'update-failed');
    else if (options.host === 'writes-finish') hostWritesInPanel(world, 'update-finish');
    trackWaits(world, waits);
    world.advance(1000); // a consulta que vai falhar sai
    const result = {before: world.state()};
    if (kind === 'cut') world.advance(30000);
    else world.answerStatus(STATUS_FAILURES[kind]);
    result.after = world.state();
    if (options.next) followNext(world, result);
    return result;
}

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

// ---------------------------------------------------------------- #8968: o host que chama o ajax() mais de uma vez
const TASK_FORM = 'form#taskform';

// As chamadas a mais do `ajax()` depois do boot da página (que já o chamou uma vez): 'new' (um objeto de opções novo a cada
// chamada), 'same' (o objeto que a chamada anterior devolveu) ou 'mixed' (novo, o devolvido, novo).
function callAjaxAgain(world, kind, calls) {
    for (let i = 0; i < calls; i++) {
        const returned = world.ajaxForm; // o objeto que a chamada anterior deixou no ajaxForm do dublê
        const options = (kind === 'same' || (kind === 'mixed' && i === 1)) ? returned : {};
        world.cmdasyncform.ajax(TASK_FORM, options);
    }
}

// O acompanhamento de uma tarefa pelo caminho do envio: o form-valid, uma resposta «não pronta», o timer, a resposta pronta e a espera.
function followOneTask(world) {
    const result = {};
    world.execute('t1');
    result.afterFormValid = world.state();
    world.answerStatus(NOT_READY);
    result.afterNotReady = world.state();
    world.advance(1000);
    result.afterTimer = world.state();
    world.answerStatus(ready('TASK OUTPUT'));
    result.afterReady = world.state();
    world.advance(5000);
    result.afterLater = world.state();
    return result;
}

// AC-8968-1: o host chama o `ajax()` de novo, desde o último `init()`.
function againChain(kind, calls) {
    const world = createWorld();
    callAjaxAgain(world, kind, calls);
    return followOneTask(world);
}

// AC-8968-1 e -3: o `init()` refaz a lista de ouvintes; `calls` chamadas do `ajax()` depois dele (o boot da página já chamou uma antes).
function reinitChain(calls) {
    const world = createWorld();
    world.cmdasyncform.init('#task-output');
    for (let i = 0; i < calls; i++) world.cmdasyncform.ajax(TASK_FORM, {});
    return followOneTask(world);
}

// AC-8968-2: a interrupção durante o envio com o `ajax()` chamado de novo com o objeto que a página passou (o do `ajaxForm()`).
function againInterrupt(trigger) {
    const world = createWorld({commandsList: trigger === 'switch'});
    world.cmdasyncform.ajax(TASK_FORM, world.ajaxForm);
    world.executeStart(); // o formulário está em voo
    const result = {before: world.state()};
    result.clicked = trigger === 'cancel' ? world.click('#btn-task-cancel') : world.switchCommand(0);
    result.afterAbort = world.state();
    world.formValid('t1'); // o servidor aceitou a tarefa
    result.afterAnswer = world.state();
    world.advance(5000);
    result.afterAdvance = world.state();
    followNext(world, result);
    return result;
}

// Robustez (sem ID): o host troca o `on_form_valid` depois do `init()` e antes do `ajax()`: o que ele pôs é o que roda.
function reinitOverride() {
    const world = createWorld();
    const calls = [];
    world.cmdasyncform.init('#task-output');
    world.cmdasyncform.on_form_valid = function (data) { calls.push(data.task.id); };
    world.cmdasyncform.ajax(TASK_FORM, {});
    world.formValid('t1');
    return {calls: calls, after: world.state()};
}

// Robustez (sem ID): o `init()` sem o `ajax()` não liga o acompanhamento (um form-valid à mão não consulta).
function initWithoutAjax() {
    const world = createWorld();
    world.cmdasyncform.init('#task-output');
    world.cmdasyncform.exc_event_callbacks('form-valid', {task: {id: 't1', command: {name: 'cmdasync_echo'}}});
    return {after: world.state()};
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

// ---------------------------------------------------------------- #8961: o motivo da falha, o aviso e o tempo limite
// As falhas do envio do formulário (o `error` do ajaxForm recebe o jqXHR; o 400 traz os erros do formulário em JSON).
const FIELD_ERRORS_400 = {app_command: ['bad command'], args: ['bad args'], kwargs: ['bad kwargs']};
const FORM_FAILURES = {
    http400: {status: 400, responseJSON: {form: {errors: FIELD_ERRORS_400}}},
    http401: {status: 401},
    http403: {status: 403},
    http413: {status: 413},
    http429: {status: 429},
    http500: {status: 500},
    http503: {status: 503},
    network: {status: 0},
};

// AC-8961-4, -6, -9 e -12: o envio que falha, num modelo de template.
function formFailure(kind, template) {
    const world = createWorld(FAILURE_TEMPLATES[template]);
    world.executeStart();
    world.formError(FORM_FAILURES[kind]);
    return {after: world.state()};
}

// AC-8961-4 e -12: o 400 com os erros em JSON e todo status de 401 a 499, um mundo novo para cada: o aviso de cada um e a tela.
function formSweep() {
    const statuses = [400];
    for (let status = 401; status <= 499; status++) statuses.push(status);
    const rows = statuses.map(function (status) {
        const world = createWorld();
        world.executeStart();
        world.formError(status === 400 ? FORM_FAILURES.http400 : {status: status});
        const state = world.state();
        return {status: status, alert: state.alert, execute: state.execute, cancel: state.cancel, revoke: state.revoke, uncaught: state.uncaught};
    });
    return {rows: rows};
}

// AC-8961-4: o Cancel ou a troca de comando com o envio em voo, e o envio falha depois.
function submitInterruptedFailure(trigger, kind) {
    const world = createWorld({commandsList: trigger === 'switch'});
    world.executeStart();
    clickTrigger(world, trigger);
    const result = {afterAbort: world.state()};
    world.formError(FORM_FAILURES[kind]);
    result.afterFail = world.state();
    return result;
}

// AC-8961-3 e -6: a 1ª consulta de estado sem resposta; o corte aos 30 s (o padrão do handler).
function timeoutFirst(template) {
    const world = createWorld(FAILURE_TEMPLATES[template]);
    world.execute('t1');
    const result = {start: world.state()};
    world.advance(29999);
    result.at29999 = world.state();
    world.advance(1);
    result.at30000 = world.state();
    world.advance(60000);
    result.later = world.state();
    return result;
}

// AC-8961-3: a 2ª consulta (sai em t = 1000, depois de uma resposta «não pronta») tem o limite dela, contado do envio dela.
// Aqui não se afirma o painel: o que a falha da 2ª consulta em diante deixa nele (e o corte aos 30 s) é o AC-9005-1 (`FailureMidwayTest`).
function timeoutSecond() {
    const world = createWorld();
    world.execute('t1');
    world.answerStatus(NOT_READY);
    world.advance(1000);
    const result = {start: world.state()};
    world.advance(29999);
    result.at29999 = world.state();
    world.advance(1);
    result.at30000 = world.state();
    return result;
}

// AC-8961-3 (controle): a consulta respondida a tempo não é cortada.
function timeoutAnswered() {
    const world = createWorld();
    world.execute('t1');
    world.advance(29999);
    world.answerStatus(ready('done'));
    const result = {afterAnswer: world.state()};
    world.advance(120000);
    result.later = world.state();
    return result;
}

// AC-8961-3: o valor que o host atribui DEPOIS do init() (a página já o chamou): 5000 corta aos 5 s, e 0 desliga o corte.
function timeoutValue(value) {
    const world = createWorld();
    world.cmdasyncform.query_timeout = value;
    world.execute('t1');
    const result = {start: world.state()};
    world.advance(value === 0 ? 120000 : value - 1);
    result.beforeCut = world.state();
    if (value > 0) {
        world.advance(1);
        result.afterCut = world.state();
    }
    return result;
}

// AC-8961-3: o valor que o host atribui ANTES do init() sobrevive a ele, e a consulta seguinte o leva.
function timeoutBeforeInit() {
    const world = createWorld();
    world.cmdasyncform.query_timeout = 5000;
    world.cmdasyncform.init('#task-output');
    const result = {afterInit: world.cmdasyncform.query_timeout};
    world.cmdasyncform.request({id: 't9'});
    result.afterRequest = world.state();
    return result;
}

// AC-8961-3: o Cancel ou a troca de comando com a consulta em voo, e o corte aos 30 s depois deles.
function timeoutInterrupted(trigger) {
    const world = createWorld({commandsList: trigger === 'switch'});
    world.execute('t1');
    clickTrigger(world, trigger);
    const result = {afterAbort: world.state()};
    world.advance(30000);
    result.afterCut = world.state();
    return result;
}

// AC-8961-8 e -10: os ouvintes de um host veem o motivo e, depois dele, o `update-finish`.
function failureReasons(kind) {
    const world = createWorld();
    const log = [];
    world.cmdasyncform.add_event_callback('update-failed', function (reason) { log.push('failed:' + reason); });
    world.cmdasyncform.add_event_callback('update-finish', function () { log.push('finish'); });
    world.execute('t1');
    if (kind === 'timeout') world.advance(30000);
    else world.answerStatus(STATUS_FAILURES[kind]);
    return {log: log, after: world.state()};
}

// AC-8961-5: o corte da consulta de workers. 'default': a página (30 s); 'custom': o host atribui 5000 antes da consulta
// (modo manual); 'hostload': um script do host atribui 5000 na carga e a 1ª consulta da página usa o valor; 'zero': o corte desligado.
function workersTimeout(how) {
    const world = how === 'default' ? createWorld({workers: 'page'})
        : how === 'hostload' ? createWorld({workers: 'page', queryTimeout: 5000})
            : createWorld({workers: 'manual'});
    if (how === 'custom') world.cmdasyncform.query_timeout = 5000;
    if (how === 'zero') world.cmdasyncform.query_timeout = 0;
    if (how === 'custom' || how === 'zero') world.workersCheck();
    const result = {start: world.state()};
    if (how === 'zero') {
        world.advance(120000);
        result.later = world.state();
        return result;
    }
    const limit = how === 'default' ? 30000 : 5000;
    world.advance(limit - 1);
    result.beforeCut = world.state();
    world.advance(1);
    result.atCut = world.state();
    world.advance(4999);
    result.beforeAgain = world.state();
    world.advance(1);
    result.again = world.state();
    return result;
}

// AC-8961-5: o valor que o host atribui DEPOIS da 1ª consulta é o limite da consulta seguinte (a sonda lê a propriedade a cada
// rodada, e não a guarda na 1ª): a 1ª leva o padrão e é cortada aos 30 s, e a 2ª, que sai 5 s depois, leva o valor novo.
function workersTimeoutLater() {
    const world = createWorld({workers: 'page'});
    const result = {start: world.state()};
    world.cmdasyncform.query_timeout = 5000;
    world.advance(30000);
    result.atFirstCut = world.state();
    world.advance(5000);
    result.secondSent = world.state();
    world.advance(4999);
    result.beforeSecondCut = world.state();
    world.advance(1);
    result.atSecondCut = world.state();
    return result;
}

// AC-8961-11 (`SHALL CONTINUE TO`): a revogação em voo não é cortada por tempo, qualquer que seja o `query_timeout`
// ('default', '5000' ou 'zero'); o `ajaxForm` do envio não leva `timeout` (o estado traz `formTimeout`).
function outOfScope(how) {
    const world = createWorld();
    if (how === '5000') world.cmdasyncform.query_timeout = 5000;
    if (how === 'zero') world.cmdasyncform.query_timeout = 0;
    trackWaits(world, 1);
    world.click('#btn-task-revoke');
    const result = {inFlight: world.state()};
    world.advance(120000);
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

// #9005: a falha da consulta no meio do acompanhamento, de cada tipo e do corte aos 30 s, na 2ª e na 3ª consulta (AC-9005-1, -5) e, só
// para o AC-9005-5, na 1ª (waits = 0)
[0, 1, 2].forEach(function (waits) {
    MIDWAY_KINDS.forEach(function (kind) {
        register('midway:' + waits + ':' + kind, function () { return midwayFailure(kind, waits); });
    });
});
// ... com o texto de execução traduzido (AC-9005-1), com o ouvinte de host que lança (AC-9005-1, -3) e com os que escrevem no painel
// (AC-9005-2), com o comando seguinte (AC-9005-4) e com o template sem o espaço do aviso (robustez, sem ID)
register('midway:1:http500:translated', function () {
    return midwayFailure('http500', 1, {world: {runningText: TRANSLATED_RUNNING_TEXT}});
});
register('midway:2:http403:throws', function () { return midwayFailure('http403', 2, {host: 'throws'}); });
register('midway:2:http500:writes-failed', function () { return midwayFailure('http500', 2, {host: 'writes-failed'}); });
register('midway:2:http500:writes-finish', function () { return midwayFailure('http500', 2, {host: 'writes-finish'}); });
['html200', 'http500', 'network', 'cut'].forEach(function (kind) {
    register('midway:2:' + kind + ':next', function () { return midwayFailure(kind, 2, {next: true}); });
});
register('midway:2:http500:nospace', function () { return midwayFailure('http500', 2, {world: TEMPLATES.nospace}); });

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

// #8968, AC-8968-1 a -3: o host que chama o ajax() mais de uma vez, e o init() no meio
register('ajax:again:new', function () { return againChain('new', 1); });
register('ajax:again:same', function () { return againChain('same', 1); });
register('ajax:again:many', function () { return againChain('new', 3); });
register('ajax:again:mixed', function () { return againChain('mixed', 3); });
register('ajax:reinit:many', function () { return reinitChain(2); });
register('ajax:reinit', function () { return reinitChain(1); });
['cancel', 'switch'].forEach(function (trigger) {
    register('ajax:again:same:interrupt:' + trigger, function () { return againInterrupt(trigger); });
});
// Robustez (sem ID)
register('ajax:reinit:override', reinitOverride);
register('robust:init-without-ajax', initWithoutAjax);

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

// #8961: o motivo da falha, o aviso e o tempo limite
// AC-8961-6: o host que traz só parte dos textos novos (os modelos de FAILURE_TEMPLATES que não são os de sempre)
['http500', 'http429', 'html502', 'network'].forEach(function (kind) {
    ['oldtexts', 'noserver', 'nonetwork'].forEach(function (template) {
        register('status:' + kind + ':' + template, function () { return statusFailure(kind, template); });
    });
});
// AC-8961-3 e -6: o corte da 1ª consulta (o modelo de cada host), o da 2ª, o da respondida a tempo (controle), o valor do host
['full', 'nospace', 'spaceonly', 'oldtexts'].forEach(function (template) {
    register('timeout:status:first:' + template, function () { return timeoutFirst(template); });
});
register('timeout:status:second', timeoutSecond);
register('timeout:status:answered', timeoutAnswered);
register('timeout:status:value:5000', function () { return timeoutValue(5000); });
register('timeout:status:value:zero', function () { return timeoutValue(0); });
register('timeout:status:before-init', timeoutBeforeInit);
['cancel', 'switch'].forEach(function (trigger) {
    register('timeout:status:interrupted:' + trigger, function () { return timeoutInterrupted(trigger); });
});
// AC-8961-4, -6, -9 e -12: o envio que falha
['http500', 'http503', 'network', 'http400'].forEach(function (kind) {
    register('form:' + kind + ':full', function () { return formFailure(kind, 'full'); });
});
['http500', 'network'].forEach(function (kind) {
    ['nospace', 'spaceonly', 'oldtexts'].forEach(function (template) {
        register('form:' + kind + ':' + template, function () { return formFailure(kind, template); });
    });
});
register('form:4xx-sweep', formSweep);
['cancel', 'switch'].forEach(function (trigger) {
    ['http500', 'network'].forEach(function (kind) {
        register('submit:' + trigger + ':fail:' + kind, function () { return submitInterruptedFailure(trigger, kind); });
    });
});
// AC-8961-8 e -10: o motivo que os ouvintes do evento recebem
['http400', 'http429', 'http500', 'html502', 'html202', 'network', 'timeout', 'http403', 'http404', 'html200'].forEach(function (kind) {
    register('reasons:' + kind, function () { return failureReasons(kind); });
});
// AC-8961-5: o corte da consulta de workers
['default', 'custom', 'hostload', 'zero'].forEach(function (how) {
    register('workers:timeout:' + how, function () { return workersTimeout(how); });
});
// AC-8961-6 (o host com só um dos textos): o envio que falha e o corte da consulta de estado
['http500', 'network'].forEach(function (kind) {
    ['noserver', 'nonetwork'].forEach(function (template) {
        register('form:' + kind + ':' + template, function () { return formFailure(kind, template); });
    });
});
['noserver', 'nonetwork'].forEach(function (template) {
    register('timeout:status:first:' + template, function () { return timeoutFirst(template); });
});
// AC-8961-5: o valor que o host atribui depois da 1ª consulta
register('workers:timeout:later', workersTimeoutLater);
// AC-8961-11: o envio e a revogação sem corte por tempo
['default', '5000', 'zero'].forEach(function (how) {
    register('timeout:out-of-scope:' + how, function () { return outOfScope(how); });
});

// ---------------------------------------------------------------- #9019: os campos de argumentos nas duas variantes
// A variante larga mantém os ids de sempre; a estreita leva o sufixo `-md` (os do `response-form-task-args-md.html`).
const ARGS_SELECTORS = {
    wide: {args: '#task-args', kwargs: '#task-kwargs'},
    narrow: {args: '#task-args-md', kwargs: '#task-kwargs-md'},
};
const TYPED_TEXT = '"a", "b"';
// O que o servidor ecoa na mensagem de erro de um campo (o `ast.literal_eval` devolve o que o usuário digitou): marcação, de propósito.
const ECHOED_TEXT = '<b>typed</b>';
const REFUSALS = {
    args: {args: ['bad args']},
    kwargs: {kwargs: ['bad kwargs']},
    command: {app_command: ['bad command']},
    all: FIELD_ERRORS_400,
    // o campo do comando recusado de novo, com outra mensagem (a segunda resposta de uma sequência)
    second: {app_command: ['second bad command']},
    echo: {app_command: [ECHOED_TEXT], args: [ECHOED_TEXT], kwargs: [ECHOED_TEXT]},
    // campos que a tela não mapeia: um nome qualquer, e dois que existem em todo objeto (`constructor`, `__proto__`: o `JSON.parse` os
    // cria como chaves PRÓPRIAS, como o corpo de um 400 de verdade; um literal `{__proto__: …}` mudaria o protótipo). O campo conhecido
    // (`args`) vem DEPOIS deles: um erro de script no laço o esconderia.
    unknown: JSON.parse('{"ghost": ["no such field"], "constructor": ["inherited name"], "__proto__": ["proto name"], "args": ["bad args"]}'),
};
function refusal(kind) {
    return {status: 400, responseJSON: {form: {errors: REFUSALS[kind]}}};
}

// AC-9019-3 e -4: o usuário muda o texto de um campo (um evento por vez) e o apaga depois; o que o campo do formulário e o campo da
// outra variante guardam, num mundo com as duas variantes ou com uma só (o host).
function argsTyping(variants, variant, field, event) {
    const world = createWorld({argsVariants: variants});
    const selector = ARGS_SELECTORS[variant][field];
    world.type(selector, TYPED_TEXT, [event]);
    const result = {afterText: world.state()};
    world.type(selector, '', [event]);
    result.afterEmpty = world.state();
    return result;
}

// AC-9019-2 e -10: o 400 com os erros do formulário, num mundo com as duas variantes ou com uma só.
function argsRefusal(variants, kind) {
    const world = createWorld({argsVariants: variants});
    world.executeStart();
    world.formError(refusal(kind));
    return {after: world.state()};
}

// AC-9019-5 e -9: a recusa e, depois, a resposta seguinte (aceita, falha de rede ou outra recusa): o que sobra na tela.
function errorsThenAnswer(first, second) {
    const world = createWorld();
    world.executeStart();
    world.formError(refusal(first));
    const result = {afterFirst: world.state()};
    world.executeStart();
    if (second === 'accepted') world.formValid('t1');
    else if (second === 'network') world.formError({status: 0});
    else world.formError(refusal(second));
    result.afterSecond = world.state();
    return result;
}

// AC-9019-8: a página de um host que traz só uma das variantes: digitar, a recusa e a resposta aceita.
function hostFlow(variant) {
    const world = createWorld({argsVariants: variant});
    ['args', 'kwargs'].forEach(function (field) {
        ['input', 'change'].forEach(function (event) { world.type(ARGS_SELECTORS[variant][field], TYPED_TEXT, [event]); });
    });
    const result = {afterTyping: world.state()};
    world.executeStart();
    world.formError(refusal('all'));
    result.afterRefusal = world.state();
    world.executeStart();
    world.formValid('t1');
    result.afterAccepted = world.state();
    return result;
}

// AC-9019-3 e -4: um mundo por variante, argumento e evento, com as duas variantes e com a variante só
['wide', 'narrow'].forEach(function (variant) {
    ['args', 'kwargs'].forEach(function (field) {
        ['input', 'change'].forEach(function (event) {
            const suffix = variant + ':' + field + ':' + event;
            register('args:type:both:' + suffix, function () { return argsTyping('both', variant, field, event); });
            register('args:type:solo-' + suffix, function () { return argsTyping(variant, variant, field, event); });
        });
    });
});
// AC-9019-2 e -10: o 400 com as duas variantes (o erro de cada campo, o de todos, o texto ecoado) e com uma variante só
['args', 'kwargs', 'command', 'all', 'echo', 'unknown'].forEach(function (kind) {
    register('errors:400:both:' + kind, function () { return argsRefusal('both', kind); });
});
['wide', 'narrow'].forEach(function (variant) {
    register('errors:400:solo-' + variant + ':all', function () { return argsRefusal(variant, 'all'); });
});
// AC-9019-5 e -9: a recusa seguida de resposta aceita, de falha de rede e de outra recusa
[['all', 'accepted'], ['all', 'network'], ['all', 'second'], ['command', 'accepted'], ['command', 'network'], ['command', 'args']]
    .forEach(function (pair) {
        register('errors:clean:' + pair[0] + ':' + pair[1], function () { return errorsThenAnswer(pair[0], pair[1]); });
    });
// AC-9019-8: o host de uma variante só
['wide', 'narrow'].forEach(function (variant) {
    register('host:' + variant, function () { return hostFlow(variant); });
});

const initial = createWorld().state();
process.stdout.write(JSON.stringify({
    constants: {
        texts: TEXTS,
        page: PAGE,
        serverErrorText: SERVER_ERROR_TEXT,
        fieldErrors400: FIELD_ERRORS_400,
        argsSelectors: ARGS_SELECTORS,
        typedText: TYPED_TEXT,
        echoedText: ECHOED_TEXT,
        refusals: REFUSALS,
        hiddenSelectors: {args: '#id_args', kwargs: '#id_kwargs'},
        // #9005: os tipos de falha da consulta (a lista de `STATUS_FAILURES`, tirada daqui), os do meio (mais o corte real) e os textos
        statusFailureKinds: Object.keys(STATUS_FAILURES),
        midwayKinds: MIDWAY_KINDS,
        midwayHostText: MIDWAY_HOST_TEXT,
        translatedRunningText: TRANSLATED_RUNNING_TEXT,
        initialButtons: {
            execute: initial.execute.disabled,
            cancel: initial.cancel.disabled,
            revoke: initial.revoke.disabled,
        },
    },
    scenarios: scenarios,
}) + '\n');
