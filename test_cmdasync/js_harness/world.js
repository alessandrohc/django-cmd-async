'use strict';
/**
 * Dublê de jQuery, de DOM e de relógio para rodar, em node, os scripts REAIS da tela de comandos
 * (`cmdhandler.js` e `cmdasync-page.js`). Não há navegador nem jsdom: o que o script pede ao `$`
 * fica gravado, e o teste lê o estado.
 *
 * O dublê repete as regras do jQuery 3 de que os scripts dependem (medidas no Chrome com o jQuery
 * do xadmin; a 3.7.1 do pacote tem a mesma API):
 *   - `$.ajax` devolve um jqXHR com `.done` e `.fail`; os callbacks rodam com `this` = o objeto de
 *     opções (uma cópia, como o jQuery faz com o `s`), e `success`/`error` das opções rodam ANTES
 *     dos `.done`/`.fail` encadeados;
 *   - sem `dataType`, o HTML do login (200) chega ao `.done` como texto; com `dataType: 'json'` vai
 *     ao `.fail` com `textStatus` 'parsererror' (e status 200);
 *   - um erro HTTP chega ao `.fail` com `textStatus` 'error' e o `status` do jqXHR (0 = rede);
 *   - uma exceção dentro de um callback para os callbacks seguintes da mesma lista e escapa como erro
 *     não capturado (aqui, registrado em `uncaught`, como o `window.onerror` faria);
 *   - `$(function)` e `$(document).ready` rodam numa macrotarefa depois da carga, e a exceção deles
 *     também escapa (jQuery.readyException): `flush()` os roda e registra o que lançou;
 *   - `attr('disabled')` reflete `prop('disabled')`; `.text(undefined)` não faz nada (o getter-setter
 *     do jQuery trata o `undefined` como "nenhum valor"); botão desabilitado não recebe clique;
 *   - o jQuery é no-op em conjunto vazio: um seletor que não existe na página devolve um conjunto
 *     vazio, sem erro.
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const PACKAGE_ROOT = path.resolve(__dirname, '..', '..');
const JS_DIR = path.join(PACKAGE_ROOT, 'commands_async', 'static', 'cmdasync', 'js');

// As frases do template do pacote (index.html): o que o script lê por data-*.
const TEXTS = {
    runningText: 'Running command...',
    revokeFailedText: 'The revocation request could not be sent.',
    forbiddenText: 'Access denied: your session may have expired, or you do not have permission to see this task.',
    notFoundText: 'Task not found.',
    // O que o toast.html do pacote publica em data-forbidden-text (o teste do template, AC-8954-4, confere o real).
    workersForbiddenText: 'Access denied: your session may have expired, or you do not have permission to see the Celery workers.',
};
const PAGE = {
    loginUrl: '/login/',
    redirectField: 'next',
    indexUrl: '/admin/command/async/',
};

function describe(error) {
    if (error && error.name) return error.name + ': ' + error.message;
    return String(error);
}

class Node {
    constructor(label, init) {
        init = init || {};
        this.label = label;
        this.props = Object.assign({}, init.props);
        this.attrs = Object.assign({}, init.attrs);
        this.dataMap = Object.assign({}, init.data);
        this.classes = new Set(init.classes || []);
        this.textValue = init.text || '';
        this.valueStr = init.val || '';
        this.handlers = {};
        this.finds = {};
        this.parentNode = null;
        // o que entrou por append/prepend, na ordem: 'text' (nó de texto), 'element' ou 'string'
        this.appended = [];
        // o texto de cada elemento que entrou por append (o `<p>` com `.text()` do aviso dos workers)
        this.childTexts = [];
    }
}

function createWorld(options) {
    // workers: false (o script de workers não é carregado), 'page' (carregado e chamado pela página, com o setting
    // ligado) ou 'manual' (carregado, setting desligado: o teste chama `world.workersCheck()`);
    // toast: 'full' (o aviso do pacote), 'nophrase' (sem o data-forbidden-text), 'noplugin' (sem o `$.fn.toast`, como num
    // host sem o bootstrap.bundle) ou 'none' (sem o #popup-msg); noFailureReason: um cmdhandler.js sem o `failure_reason`.
    const opts = Object.assign({commandsList: false, alertSpace: true, texts: true, workers: false, toast: 'full', noFailureReason: false}, options || {});
    const world = {
        opts: opts,
        toasts: [],
        requests: [],
        locations: [],
        alerts: [],
        uncaught: [],
        consoleErrors: [],
        readyQueue: [],
        ajaxForm: null,
        cmdasyncform: null,
        clock: {now: 0, timers: [], nextId: 1},
    };

    function runGuarded(source, fn, args, thisArg) {
        try {
            return fn.apply(thisArg === undefined ? null : thisArg, args);
        } catch (error) {
            world.uncaught.push({source: source, message: describe(error)});
            return undefined;
        }
    }

    // ---------------------------------------------------------------- DOM por seletor
    const registry = {};
    function add(selector, node) {
        (registry[selector] = registry[selector] || []).push(node);
        return node;
    }

    function button(id, disabled) {
        const node = new Node(id, {props: {disabled: disabled}});
        const progress = new Node(id + ' .task-progress', {classes: ['spinner-grow', 'd-none', 'task-progress']});
        node.finds['.task-progress'] = [progress];
        node.progress = progress;
        return add('#' + id, node);
    }

    // O estado inicial é o do template: Execute habilitado, Cancel e Revoke desabilitados (index.html:60-73).
    const submit = button('btn-task-submit', false);
    const cancel = button('btn-task-cancel', true);
    const revoke = button('btn-task-revoke', true);

    const form = add('form#taskform', new Node('form'));
    form.finds['#btn-task-submit'] = [submit];
    form.finds['#btn-task-cancel'] = [cancel];
    form.finds['#btn-task-revoke'] = [revoke];

    const panelData = {
        'commands-list': opts.commandsList,
        'workers-status-check': opts.workers === 'page',
        'login-url': PAGE.loginUrl,
        'redirect-field': PAGE.redirectField,
        'index-url': PAGE.indexUrl,
        'running-text': TEXTS.runningText,
    };
    if (opts.texts) {
        panelData['revoke-failed-text'] = TEXTS.revokeFailedText;
        panelData['forbidden-text'] = TEXTS.forbiddenText;
        panelData['not-found-text'] = TEXTS.notFoundText;
    }
    add('#task-panel', new Node('panel', {data: panelData}));

    const output = add('#task-output', new Node('output'));
    const copy = add('#task-copy', new Node('copy', {classes: ['btn', 'd-none']}));
    const alertNode = opts.alertSpace
        ? add('#task-alert', new Node('alert', {classes: ['alert', 'alert-danger', 'd-none']}))
        : null;
    // O aviso dos workers (cmdasync/toast.html): o corpo, o badge e, no template do pacote, a frase em data-forbidden-text.
    const toastBody = new Node('toast body');
    const toastBadge = new Node('toast badge');
    if (opts.toast !== 'none') {
        const toastNode = add('#popup-msg', new Node('toast', {
            data: opts.toast === 'nophrase' ? {} : {'forbidden-text': TEXTS.workersForbiddenText},
        }));
        toastNode.finds['.toast-body'] = [toastBody];
        toastNode.finds['.online'] = [toastBadge];
    }
    add('#task-args', new Node('args'));
    add('#task-kwargs', new Node('kwargs'));
    add('#id_args', new Node('hidden args'));
    add('#id_kwargs', new Node('hidden kwargs'));
    add('input#id_app_command', new Node('hidden command'));
    add('#form-app_name', new Node('app name'));
    const appCommand = add('#task-app-command', new Node('app command'));
    if (opts.commandsList) {
        appCommand.finds['> span.info'] = [new Node('command info')];
        add('.dropdown-menu > a', new Node('item 1', {attrs: {href: '#test_cmdasync.cmdasync_echo'}}));
        add('.dropdown-menu > a', new Node('item 2', {attrs: {href: '#test_cmdasync.cmdasync_other'}}));
    }

    // ---------------------------------------------------------------- jQuery
    const documentNode = new Node('document');
    const documentStub = {
        cookie: 'csrftoken=abc',
        body: {},
        createTextNode: function (value) {
            return {isTextNode: true, text: String(value)};
        },
        getElementById: function () {
            return null;
        },
    };

    class JQ {
        constructor(nodes) {
            this.nodes = nodes;
            this.length = nodes.length;
            this.isJQ = true;
        }
        ready(fn) {
            world.readyQueue.push(fn);
            return this;
        }
        on(event, fn) {
            this.nodes.forEach(function (node) {
                (node.handlers[event] = node.handlers[event] || []).push(fn);
            });
            return this;
        }
        find(selector) {
            const found = [];
            this.nodes.forEach(function (node) {
                (node.finds[selector] || []).forEach(function (child) { found.push(child); });
            });
            return new JQ(found);
        }
        prop(name, value) {
            if (arguments.length === 1) return this.nodes.length ? this.nodes[0].props[name] : undefined;
            this.nodes.forEach(function (node) { node.props[name] = value; });
            return this;
        }
        attr(name, value) {
            if (arguments.length === 1) {
                if (!this.nodes.length) return undefined;
                if (name === 'disabled') return this.nodes[0].props.disabled ? 'disabled' : undefined;
                return this.nodes[0].attrs[name];
            }
            this.nodes.forEach(function (node) {
                if (name === 'disabled') node.props.disabled = !!value;
                else node.attrs[name] = value;
            });
            return this;
        }
        val(value) {
            if (arguments.length === 0) return this.nodes.length ? this.nodes[0].valueStr : undefined;
            this.nodes.forEach(function (node) { node.valueStr = String(value); });
            return this;
        }
        data(key) {
            return this.nodes.length ? this.nodes[0].dataMap[key] : undefined;
        }
        addClass(names) {
            this.nodes.forEach(function (node) {
                String(names).split(/\s+/).filter(Boolean).forEach(function (name) { node.classes.add(name); });
            });
            return this;
        }
        removeClass(names) {
            this.nodes.forEach(function (node) {
                String(names).split(/\s+/).filter(Boolean).forEach(function (name) { node.classes.delete(name); });
            });
            return this;
        }
        text(value) {
            if (arguments.length === 0) return this.nodes.length ? this.nodes[0].textValue : '';
            if (value === undefined) return this; // o getter-setter do jQuery: sem valor, nada muda
            this.nodes.forEach(function (node) {
                node.textValue = String(value);
                node.appended = [];
                node.childTexts = [];
            });
            return this;
        }
        empty() {
            this.nodes.forEach(function (node) {
                node.textValue = '';
                node.appended = [];
                node.childTexts = [];
            });
            return this;
        }
        append(child) {
            this.nodes.forEach(function (node) {
                if (child && child.isTextNode) {
                    node.textValue += child.text;
                    node.appended.push('text');
                } else if (child && child.isJQ) {
                    child.nodes.forEach(function (inner) {
                        inner.parentNode = node;
                        node.childTexts.push(inner.textValue);
                    });
                    node.appended.push('element');
                } else {
                    // uma string chegaria ao jQuery como marcador (HTML): é o que o DomSinkTest proíbe
                    node.appended.push('string');
                }
            });
            return this;
        }
        prepend(child) {
            this.nodes.forEach(function (node) {
                if (child && child.isTextNode) {
                    node.textValue = child.text + node.textValue;
                    node.appended.unshift('text');
                } else {
                    node.appended.unshift(child && child.isJQ ? 'element' : 'string');
                }
            });
            return this;
        }
        remove() {
            return this;
        }
        parent() {
            return new JQ(this.nodes.map(function (node) { return node.parentNode || new Node('parent'); }));
        }
        clone() {
            return new JQ(this.nodes.map(function (node) {
                const copy = new Node(node.label + ' (clone)');
                node.classes.forEach(function (name) { copy.classes.add(name); });
                return copy;
            }));
        }
        serialize() {
            return '';
        }
        ajaxForm(options) {
            world.ajaxForm = options;
            return this;
        }
    }

    // O plugin de toast do Bootstrap 4 (`$.fn.toast`) só existe quando o bootstrap.bundle é carregado, e num conjunto vazio
    // é no-op (a interface do plugin usa `this.each`); o toast 'noplugin' é o host sem ele: `$toast.toast` é `undefined`.
    if (opts.toast !== 'noplugin') {
        JQ.prototype.toast = function (arg) {
            if (this.nodes.length) world.toasts.push(String(arg));
            return this;
        };
    }

    function $(arg) {
        if (typeof arg === 'function') {
            world.readyQueue.push(arg);
            return undefined;
        }
        if (arg === documentStub) return new JQ([documentNode]);
        if (arg instanceof Node) return new JQ([arg]);
        if (arg && arg.isJQ) return arg;
        if (typeof arg === 'string') {
            if (arg.trim().charAt(0) === '<') {
                const created = new Node(arg);
                const klass = /class="([^"]*)"/.exec(arg);
                if (klass) klass[1].split(/\s+/).filter(Boolean).forEach(function (name) { created.classes.add(name); });
                return new JQ([created]);
            }
            return new JQ((registry[arg] || []).slice());
        }
        return new JQ([]);
    }

    $.each = function (list, fn) {
        list.forEach(function (item, index) { fn(index, item); });
        return list;
    };

    $.ajax = function (options) {
        const settings = Object.assign({}, options); // o jQuery chama os callbacks com esta cópia
        const request = {
            id: world.requests.length,
            url: String(options.url),
            type: String(options.type || 'GET').toUpperCase(),
            dataType: options.dataType,
            opts: settings,
            done: [],
            fail: [],
            responded: false,
        };
        if (settings.beforeSend) {
            runGuarded('beforeSend ' + request.url, settings.beforeSend, [{setRequestHeader: function () {}}, settings], settings);
        }
        if (settings.success) request.done.push(settings.success);
        if (settings.error) request.fail.push(settings.error);
        world.requests.push(request);
        const jqXHR = {
            done: function (fn) { request.done.push(fn); return jqXHR; },
            fail: function (fn) { request.fail.push(fn); return jqXHR; },
        };
        return jqXHR;
    };

    function outcome(request, response) {
        const body = response.body;
        const xhr = {
            status: response.status,
            responseText: typeof body === 'string' ? body : JSON.stringify(body),
        };
        if (response.status === 0) return {ok: false, textStatus: 'error', xhr: xhr};
        if (response.status >= 400) {
            if (request.dataType === 'json') {
                try { xhr.responseJSON = JSON.parse(xhr.responseText); } catch (error) { /* corpo que não é JSON */ }
            }
            return {ok: false, textStatus: 'error', xhr: xhr};
        }
        let data = body;
        if (request.dataType === 'json' && typeof data === 'string') {
            try {
                data = JSON.parse(data);
            } catch (error) {
                return {ok: false, textStatus: 'parsererror', xhr: xhr};
            }
        }
        return {ok: true, data: data, textStatus: 'success', xhr: xhr};
    }

    world.respond = function (request, response) {
        if (request.responded) throw new Error('harness: request already answered: ' + request.url);
        request.responded = true;
        const result = outcome(request, response);
        const callbacks = result.ok ? request.done : request.fail;
        const args = result.ok ? [result.data, result.textStatus, result.xhr] : [result.xhr, result.textStatus, 'error'];
        for (let i = 0; i < callbacks.length; i++) {
            try {
                callbacks[i].apply(request.opts, args);
            } catch (error) {
                world.uncaught.push({source: 'ajax ' + request.type + ' ' + request.url, message: describe(error)});
                break;
            }
        }
    };

    function pendingOf(type) {
        return world.requests.filter(function (request) {
            return !request.responded && /^status\//.test(request.url) && request.type === type;
        });
    }
    world.statusRequests = function () {
        return world.requests.filter(function (request) { return /^status\//.test(request.url) && request.type === 'GET'; });
    };
    world.revokeRequests = function () {
        return world.requests.filter(function (request) { return /^status\//.test(request.url) && request.type === 'POST'; });
    };
    world.answerStatus = function (response, which) {
        const pending = pendingOf('GET');
        if (!pending.length) throw new Error('harness: no status request in flight');
        world.respond(which === 'last' ? pending[pending.length - 1] : pending[0], response);
    };
    world.answerRevoke = function (response) {
        const pending = pendingOf('POST');
        if (!pending.length) throw new Error('harness: no revoke request in flight');
        world.respond(pending[0], response);
    };
    // As consultas de workers (`workers/status/`): os filtros `/^status\//` acima não as veem.
    function pendingWorkers() {
        return world.requests.filter(function (request) {
            return !request.responded && /^workers\/status\//.test(request.url) && request.type === 'GET';
        });
    }
    world.workersRequests = function () {
        return world.requests.filter(function (request) { return /^workers\/status\//.test(request.url); });
    };
    world.answerWorkers = function (response) {
        const pending = pendingWorkers();
        if (!pending.length) throw new Error('harness: no workers request in flight');
        world.respond(pending[0], response);
    };

    // ---------------------------------------------------------------- relógio
    function setTimeoutStub(fn, ms) {
        const timer = {id: world.clock.nextId++, fn: fn, at: world.clock.now + (ms || 0), live: true};
        world.clock.timers.push(timer);
        return timer.id;
    }
    function clearTimeoutStub(id) {
        world.clock.timers.forEach(function (timer) {
            if (timer.id === id) timer.live = false;
        });
    }
    world.liveTimers = function () {
        return world.clock.timers.filter(function (timer) { return timer.live; }).length;
    };
    world.advance = function (ms) {
        const target = world.clock.now + ms;
        for (;;) {
            const due = world.clock.timers
                .filter(function (timer) { return timer.live && timer.at <= target; })
                .sort(function (a, b) { return a.at - b.at || a.id - b.id; })[0];
            if (!due) break;
            world.clock.now = due.at;
            due.live = false;
            runGuarded('timer', due.fn, [], null);
        }
        world.clock.now = target;
    };

    // ---------------------------------------------------------------- janela, console e carga
    const windowStub = {getSelection: function () { return null; }};
    Object.defineProperty(windowStub, 'location', {
        get: function () { return world.locations.length ? world.locations[world.locations.length - 1] : PAGE.indexUrl; },
        set: function (value) { world.locations.push(String(value)); },
    });
    const consoleStub = {
        log: function () {},
        info: function () {},
        warn: function (message) { world.consoleErrors.push(String(message)); },
        error: function (message) { world.consoleErrors.push(String(message)); },
    };
    const context = vm.createContext({
        $: $,
        jQuery: $,
        window: windowStub,
        document: documentStub,
        alert: function (message) { world.alerts.push(String(message)); },
        console: consoleStub,
        setTimeout: setTimeoutStub,
        clearTimeout: clearTimeoutStub,
    });

    world.flush = function () {
        while (world.readyQueue.length) {
            runGuarded('ready', world.readyQueue.shift(), [], null);
        }
    };

    // a ordem do index.html: o handler, o script de workers (só com o setting ligado) e, por último, o da página
    ['cmdhandler.js'].concat(opts.workers ? ['celery.workers.status.js'] : [], ['cmdasync-page.js']).forEach(function (name) {
        vm.runInContext(fs.readFileSync(path.join(JS_DIR, name), 'utf8'), context, {filename: name});
    });
    world.cmdasyncform = context.cmdasyncform;
    // um cmdhandler.js sem o `failure_reason` (um estático sobrescrito, ou o cache do navegador na troca de versão)
    if (opts.noFailureReason) delete world.cmdasyncform.failure_reason;
    // a chamada do script de workers à mão, com ou sem argumento (a página sempre passa `{onLogin}`)
    world.workersCheck = function (options) {
        if (typeof context.workers_check_status !== 'function') throw new Error('harness: the workers script is not loaded');
        return arguments.length ? context.workers_check_status(options) : context.workers_check_status();
    };
    world.flush();

    // ---------------------------------------------------------------- ações do usuário
    world.click = function (selector, index) {
        const node = (registry[selector] || [])[index || 0];
        if (!node) throw new Error('harness: no element for ' + selector);
        if (node.props.disabled) return false; // o navegador não entrega clique a botão desabilitado
        (node.handlers.click || []).slice().forEach(function (handler) {
            runGuarded('click ' + selector, handler, [{}], node);
        });
        return true;
    };
    world.executeStart = function () {
        world.click('#btn-task-submit');
        const proceed = runGuarded('beforeSubmit', world.ajaxForm.beforeSubmit, [[], $('form#taskform'), world.ajaxForm], null);
        return proceed !== false;
    };
    world.formValid = function (taskId) {
        runGuarded('form success', world.ajaxForm.success,
            [{task: {id: taskId, command: {name: 'cmdasync_echo'}}}, 'success', {status: 200}, $('form#taskform')], null);
    };
    world.execute = function (taskId) {
        if (world.executeStart()) world.formValid(taskId || 't1');
    };
    world.formError = function (xhr) {
        runGuarded('form error', world.ajaxForm.error, [xhr], null);
    };

    // ---------------------------------------------------------------- o que o teste lê
    function buttonState(node) {
        return {disabled: !!node.props.disabled, progressVisible: !node.progress.classes.has('d-none')};
    }
    world.state = function () {
        return {
            execute: buttonState(submit),
            cancel: buttonState(cancel),
            revoke: buttonState(revoke),
            alert: alertNode ? {text: alertNode.textValue, hidden: alertNode.classes.has('d-none')} : null,
            output: output.textValue,
            outputNodes: output.appended.slice(),
            copyHidden: copy.classes.has('d-none'),
            locations: world.locations.slice(),
            browserAlerts: world.alerts.slice(),
            uncaught: world.uncaught.slice(),
            consoleErrors: world.consoleErrors.slice(),
            liveTimers: world.liveTimers(),
            statusRequests: world.statusRequests().length,
            pendingStatus: pendingOf('GET').length,
            revokeRequests: world.revokeRequests().length,
            workersRequests: world.workersRequests().length,
            pendingWorkers: pendingWorkers().length,
            // o aviso dos workers: o texto de cada <p> do corpo, o badge e quantas vezes o `toast("show")` foi chamado
            toast: {body: toastBody.childTexts.slice(), badge: toastBadge.textValue, shown: world.toasts.length},
        };
    };
    world.boot = function () {
        const events = world.cmdasyncform ? world.cmdasyncform.events : null;
        return {
            ajaxFormBound: !!world.ajaxForm,
            cancelBound: !!(cancel.handlers.click && cancel.handlers.click.length),
            revokeBound: !!(revoke.handlers.click && revoke.handlers.click.length),
            updateFinishCallbacks: events ? events['update-finish'].length : 0,
            itemsBound: (registry['.dropdown-menu > a'] || []).filter(function (node) {
                return node.handlers.click && node.handlers.click.length;
            }).length,
        };
    };
    world.switchCommand = function (index) {
        return world.click('.dropdown-menu > a', index || 0);
    };

    return world;
}

module.exports = {createWorld, TEXTS, PAGE};
