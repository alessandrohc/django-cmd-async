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
 *   - o 204 e o 304 chegam ao `.done` como sucesso com `data` INDEFINIDO e `textStatus` 'nocontent' e 'notmodified', com ou sem
 *     corpo e mesmo com `dataType: 'json'` (`jquery-3.7.1.min.js`: `204===e||"HEAD"===v.type?l="nocontent":304===e?l="notmodified":
 *     (l=s.state,o=s.data,i=!(a=s.error))`: o `data` só é atribuído no 3º ramo); o 2xx com corpo que não é JSON segue indo ao `.fail`
 *     como 'parsererror' (#9018; `DoubleAssumptionsTest` confere a fonte do jQuery);
 *   - um erro HTTP chega ao `.fail` com `textStatus` 'error' e o `status` do jqXHR (0 = rede);
 *   - `timeout` maior que zero (`jquery-3.7.1.min.js`: `v.async&&0<v.timeout&&(…setTimeout(function(){T.abort("timeout")},
 *     v.timeout))`) corta o pedido sem resposta quando o tempo passa: o `.fail` recebe `textStatus` 'timeout' e `status` 0;
 *     `0`, `null` e `undefined` não ligam o corte. O corte confere se o pedido já foi respondido (o jQuery limpa o timer);
 *     o timer dele não conta em `liveTimers` (que mede as consultas agendadas pela página);
 *   - uma exceção dentro de um callback para os callbacks seguintes da mesma lista e escapa como erro
 *     não capturado (aqui, registrado em `uncaught`, como o `window.onerror` faria);
 *   - `$(function)` e `$(document).ready` rodam numa macrotarefa depois da carga, e a exceção deles
 *     também escapa (jQuery.readyException): `flush()` os roda e registra o que lançou;
 *   - `attr('disabled')` reflete `prop('disabled')`; `.text(undefined)` não faz nada (o getter-setter
 *     do jQuery trata o `undefined` como "nenhum valor"); botão desabilitado não recebe clique;
 *   - o jQuery é no-op em conjunto vazio: um seletor que não existe na página devolve um conjunto
 *     vazio, sem erro;
 *   - `.on(evento, fn)` registra o evento pelo texto exato: `'input'` e `'change'` são dois `.on`. A lista de eventos do jQuery
 *     (`.on('input change', fn)`) NÃO é modelada, e o script da página usa uma chamada por evento (#9019);
 *   - anexar um elemento que já está anexado o MOVE do pai antigo para o novo (a regra do DOM: `appendChild`; o `append` do jQuery
 *     entregue chama `appendChild`), e `.remove()` tira o elemento do pai (jquery-3.7.1.min.js: `remove` chama a função que faz
 *     `parentNode.removeChild`); `$('.invalid-feedback')` acha os elementos criados em execução e ainda anexados (#9019);
 *   - `.val(x)` só grava o valor: nunca dispara `input` nem `change` (#9019); o que o script grava num campo por `.val(x)` fica em
 *     `scriptWrites` de cada nó (o `world.type` atribui direto, como o navegador ao digitar, e não entra): é o registro de que o
 *     espelho do texto não regrava o campo em que se digita (regravá-lo durante uma composição de IME a quebra) (#9019);
 *   - `markupSinks` lista o que o script entregou ao jQuery como MARCAÇÃO: `.html()`, `append` e `prepend` de string e `$('<…>')`
 *     com texto dentro (o `$('<div class="invalid-feedback"></div>')` vazio não leva dado e não conta) (#9019).
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
    // Os dois textos de falha do #8961 (data-server-error-text e data-network-error-text). Não se chamam `serverErrorText`:
    // esse nome é do texto que o SERVIDOR manda na revogação (`serverErrorText` das constantes do scenarios.js).
    serverFailureText: 'The server could not answer the request, and the last command sent may still be running: check before running it again.',
    networkFailureText: 'The connection to the server failed or took too long, and the last command sent may still be running: check before running it again.',
    // O texto de recusa do #9016 (data-refused-text): o envio recusado com um 4xx; o teste do template confere a mesma frase (AC-9016-2).
    refusedFailureText: 'The server refused the request, and the command was not run. If your session has expired, reload the page; otherwise try again in a moment, or check that you have permission to run commands.',
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
        // os valores que o SCRIPT gravou no campo por `.val(x)`, na ordem (o `world.type` não entra: é o usuário) (#9019)
        this.scriptWrites = [];
        this.handlers = {};
        this.finds = {};
        this.parentNode = null;
        // o que entrou por append/prepend, na ordem: 'text' (nó de texto), 'element' ou 'string'
        this.appended = [];
        // os elementos que entraram por append, na ordem, cada um com a CÓPIA do texto do momento (o `<p>` com `.text()` do aviso dos
        // workers; a mensagem de erro de um campo): é o que um `remove()` ou um novo `append` do mesmo elemento tira daqui (#9019)
        this.childEntries = [];
    }

    // o texto de cada elemento anexado, na ordem: o que o teste lê
    get childTexts() {
        return this.childEntries.map(function (entry) { return entry.text; });
    }
}

// Tira o elemento do pai (o `removeChild` do DOM): o pai deixa de tê-lo e ele deixa de ter pai.
function detach(node) {
    const parent = node.parentNode;
    if (!parent) return;
    parent.childEntries = parent.childEntries.filter(function (entry) { return entry.node !== node; });
    node.parentNode = null;
}

// O `.text(valor)` e o `.empty()` do jQuery descartam os filhos do elemento.
function dropChildren(node) {
    node.childEntries.forEach(function (entry) { entry.node.parentNode = null; });
    node.childEntries = [];
}

function createWorld(options) {
    // workers: false (o script de workers não é carregado), 'page' (carregado e chamado pela página, com o setting
    // ligado) ou 'manual' (carregado, setting desligado: o teste chama `world.workersCheck()`);
    // toast: 'full' (o aviso do pacote), 'nophrase' (sem o data-forbidden-text), 'noplugin' (sem o `$.fn.toast`, como num
    // host sem o bootstrap.bundle) ou 'none' (sem o #popup-msg); noFailureReason: um cmdhandler.js sem o `failure_reason`;
    // runningText: o texto de execução que o template publica em data-running-text (padrão TEXTS.runningText; um outro
    // texto prova que a página escreve o que o template publicou, e não o literal em inglês: a tradução, AC-8967-3);
    // failureTexts: quais dos dois textos de falha do #8961 o painel traz ('both' por padrão, 'none', 'server' ou 'network';
    // `texts: false` tira também os três antigos); refusalText: se o painel traz o texto de recusa do #9016, o data-refused-text
    // (verdadeiro por padrão; `texts: false` o tira também: um host sem os textos não o publica); queryTimeout: o valor que um script do host atribui a
    // `cmdasyncform.query_timeout` na carga, depois do cmdhandler.js e antes do DOM ready (AC-8961-5); argsVariants: quais variantes
    // dos campos de argumentos a página tem (#9019): 'both' (o template do pacote: a larga, `#task-args` e `#task-kwargs`, e a estreita,
    // `#task-args-md` e `#task-kwargs-md`), 'wide' ou 'narrow' (um host de template próprio com uma variante só) ou 'none' (um host sem
    // nenhum campo de argumentos: o erro do 400 não tem onde ser posto, #9016).
    const opts = Object.assign({commandsList: false, alertSpace: true, texts: true, workers: false, toast: 'full', noFailureReason: false,
        failureTexts: 'both', refusalText: true, queryTimeout: undefined, argsVariants: 'both'}, options || {});
    if (['both', 'wide', 'narrow', 'none'].indexOf(opts.argsVariants) < 0) throw new Error('harness: unknown argsVariants ' + opts.argsVariants);
    const world = {
        opts: opts,
        markupSinks: [],
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
    // os elementos que o script cria em execução (`$('<div …>')` e `.clone()`): o `$('.invalid-feedback')` os acha enquanto anexados
    const createdNodes = [];
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
        'running-text': opts.runningText || TEXTS.runningText,
    };
    if (opts.texts) {
        panelData['revoke-failed-text'] = TEXTS.revokeFailedText;
        panelData['forbidden-text'] = TEXTS.forbiddenText;
        panelData['not-found-text'] = TEXTS.notFoundText;
        if (opts.failureTexts === 'both' || opts.failureTexts === 'server') panelData['server-error-text'] = TEXTS.serverFailureText;
        if (opts.failureTexts === 'both' || opts.failureTexts === 'network') panelData['network-error-text'] = TEXTS.networkFailureText;
        if (opts.refusalText) panelData['refused-text'] = TEXTS.refusedFailureText;
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
    // Os campos do formulário e o nó pai de cada um: o erro de campo do 400 vai ao lado do campo (`$input.parent().append(...)`,
    // cmdasync-page.js:185-203), então o pai é estável para o teste ler o que foi posto (`fieldErrors` no estado). O dublê tem um nó
    // por seletor. As duas variantes dos argumentos (#9019) têm cada uma o seu nó, o seu pai e o seu estado: a larga é a de sempre
    // (`fieldParents`, `fieldErrors`, `invalidFields`) e a estreita leva `Narrow` no nome (`narrowParents`, `fieldErrorsNarrow`...);
    // `argsVariants` omite uma delas (o host de uma variante só).
    const fieldParents = {app_command: new Node('app command parent'), args: new Node('args parent'), kwargs: new Node('kwargs parent')};
    const narrowParents = {args: new Node('args parent (narrow)'), kwargs: new Node('kwargs parent (narrow)')};
    const wideFields = opts.argsVariants === 'narrow' || opts.argsVariants === 'none' ? null
        : {args: add('#task-args', new Node('args')), kwargs: add('#task-kwargs', new Node('kwargs'))};
    const narrowFields = opts.argsVariants === 'wide' || opts.argsVariants === 'none' ? null
        : {args: add('#task-args-md', new Node('args (narrow)')), kwargs: add('#task-kwargs-md', new Node('kwargs (narrow)'))};
    if (wideFields) {
        wideFields.args.parentNode = fieldParents.args;
        wideFields.kwargs.parentNode = fieldParents.kwargs;
    }
    if (narrowFields) {
        narrowFields.args.parentNode = narrowParents.args;
        narrowFields.kwargs.parentNode = narrowParents.kwargs;
    }
    // os campos ocultos do formulário (`forms.py`: `HiddenInput`): o que o `change` preenche é o que vai com o comando
    const hiddenFields = {args: add('#id_args', new Node('hidden args')), kwargs: add('#id_kwargs', new Node('hidden kwargs'))};
    const hiddenCommand = add('input#id_app_command', new Node('hidden command'));
    add('#form-app_name', new Node('app name'));
    const appCommand = add('#task-app-command', new Node('app command'));
    appCommand.parentNode = fieldParents.app_command;
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
            this.nodes.forEach(function (node) {
                node.valueStr = String(value);
                node.scriptWrites.push(String(value));
            });
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
                dropChildren(node);
            });
            return this;
        }
        empty() {
            this.nodes.forEach(function (node) {
                node.textValue = '';
                node.appended = [];
                dropChildren(node);
            });
            return this;
        }
        // Com um conjunto de VÁRIOS pais, o jQuery clona o elemento para todos menos o último; o dublê não o modela (o script anexa
        // a um pai por vez, e a mensagem de cada campo tem o seu `.clone()`).
        append(child) {
            this.nodes.forEach(function (node) {
                if (child && child.isTextNode) {
                    node.textValue += child.text;
                    node.appended.push('text');
                } else if (child && child.isJQ) {
                    child.nodes.forEach(function (inner) {
                        detach(inner); // anexar um elemento já anexado o MOVE: ele sai do pai antigo (a regra do DOM)
                        inner.parentNode = node;
                        node.childEntries.push({node: inner, text: inner.textValue});
                    });
                    node.appended.push('element');
                } else {
                    // uma string chegaria ao jQuery como marcador (HTML): é o que o DomSinkTest proíbe
                    node.appended.push('string');
                    world.markupSinks.push({sink: 'append(string)', value: String(child)});
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
                    if (!(child && child.isJQ)) world.markupSinks.push({sink: 'prepend(string)', value: String(child)});
                }
            });
            return this;
        }
        remove() {
            this.nodes.forEach(detach);
            return this;
        }
        // O `.html()` entrega ao jQuery o que ele LÊ COMO MARCAÇÃO: o dublê não o executa, só o registra (a página não o usa).
        html(value) {
            world.markupSinks.push({sink: 'html', value: value === undefined ? null : String(value)});
            return this;
        }
        parent() {
            return new JQ(this.nodes.map(function (node) { return node.parentNode || new Node('parent'); }));
        }
        clone() {
            return new JQ(this.nodes.map(function (node) {
                const copy = new Node(node.label + ' (clone)');
                node.classes.forEach(function (name) { copy.classes.add(name); });
                createdNodes.push(copy);
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
                // marcação SEM texto dentro (o `<div class="invalid-feedback"></div>`) não leva dado; com texto, o dado entrou como marcação
                if (arg.replace(/<[^>]*>/g, '') !== '') world.markupSinks.push({sink: '$(markup)', value: arg});
                const created = new Node(arg);
                const klass = /class="([^"]*)"/.exec(arg);
                if (klass) klass[1].split(/\s+/).filter(Boolean).forEach(function (name) { created.classes.add(name); });
                createdNodes.push(created);
                return new JQ([created]);
            }
            // a mensagem de erro de um campo: os elementos criados em execução com essa classe que ainda estão anexados
            if (arg === '.invalid-feedback') {
                return new JQ(createdNodes.filter(function (node) { return node.parentNode && node.classes.has('invalid-feedback'); }));
            }
            return new JQ((registry[arg] || []).slice());
        }
        return new JQ([]);
    }

    // Só percorre lista: um texto, um número, um booleano ou um objeto lançam (`forEach` indefinido). No jQuery real um texto, um número e um
    // booleano também lançam (`isArrayLike` faz `"length" in e`), mas um OBJETO é percorrido por `for…in`: o `workers` objeto da sonda (#9106) é
    // vermelho aqui pelo dublê, e o que o prova no verde é o guarda da sonda, que o recusa antes de chegar ao `$.each`.
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
        // o corte por tempo do jQuery: só com `timeout` maior que zero; o pedido já respondido não é cortado
        if (settings.timeout > 0) {
            request.timeoutTimer = scheduleTimer(function () {
                if (!request.responded) world.respond(request, {status: 0, timedOut: true});
            }, settings.timeout, 'ajax-timeout');
        }
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
        // o corte por tempo: `abort("timeout")` rejeita com o texto «timeout» e o status 0 do jqXHR abortado
        if (response.timedOut) return {ok: false, textStatus: 'timeout', xhr: xhr};
        if (response.status === 0) return {ok: false, textStatus: 'error', xhr: xhr};
        if (response.status >= 400) {
            if (request.dataType === 'json') {
                try { xhr.responseJSON = JSON.parse(xhr.responseText); } catch (error) { /* corpo que não é JSON */ }
            }
            return {ok: false, textStatus: 'error', xhr: xhr};
        }
        // o 204 e o 304: sucesso sem dado, qualquer que seja o corpo e o `dataType` (a regra do jQuery citada no cabeçalho; #9018)
        if (response.status === 204 || response.status === 304) {
            return {ok: true, data: undefined, textStatus: response.status === 204 ? 'nocontent' : 'notmodified', xhr: xhr};
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
        if (request.timeoutTimer) clearTimeoutStub(request.timeoutTimer); // o jQuery limpa o timer quando o pedido termina
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
    // `kind`: 'page' (o `setTimeout` da página) ou 'ajax-timeout' (o corte por tempo de um `$.ajax`, que não conta em `liveTimers`)
    function scheduleTimer(fn, ms, kind) {
        const timer = {id: world.clock.nextId++, fn: fn, at: world.clock.now + (ms || 0), live: true, kind: kind};
        world.clock.timers.push(timer);
        return timer.id;
    }
    function setTimeoutStub(fn, ms) {
        return scheduleTimer(fn, ms, 'page');
    }
    function clearTimeoutStub(id) {
        world.clock.timers.forEach(function (timer) {
            if (timer.id === id) timer.live = false;
        });
    }
    world.liveTimers = function () {
        return world.clock.timers.filter(function (timer) { return timer.live && timer.kind !== 'ajax-timeout'; }).length;
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
    // um script do host que atribui o tempo limite na carga: depois do cmdhandler.js e antes do DOM ready (o `flush()` abaixo)
    if (opts.queryTimeout !== undefined) world.cmdasyncform.query_timeout = opts.queryTimeout;
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
    // O usuário muda o texto de um campo (#9019): o valor novo entra no campo e, em seguida, o navegador dispara cada evento da lista
    // (`input` a cada tecla ou ao colar; `change` ao sair do campo), com `this` = o campo. O padrão são os dois, na ordem do navegador.
    world.type = function (selector, text, events, index) {
        const node = (registry[selector] || [])[index || 0];
        if (!node) throw new Error('harness: no element for ' + selector);
        node.valueStr = String(text);
        (events || ['input', 'change']).forEach(function (event) {
            (node.handlers[event] || []).slice().forEach(function (handler) {
                runGuarded(event + ' ' + selector, handler, [{}], node);
            });
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
    // #9106: o servidor responde o envio com `response` (`{status, body}`, a forma das respostas dos outros pontos). O `success` do `ajaxForm`
    // recebe o que o jQuery entregaria a um SUCESSO com `dataType: 'json'` (o `ajax()` do handler o fixa): `(data, textStatus, xhr, $form)`,
    // o `data` indefinido no 204 e no 304 (a regra de `outcome`, acima) e o corpo já convertido num JSON que o é. Uma resposta que o jQuery mandaria
    // ao `error` (um 4xx, um corpo que não é JSON) não passa por aqui: o harness acusa, em vez de fingir um sucesso.
    world.formAnswer = function (response) {
        const result = outcome({dataType: 'json'}, response);
        if (!result.ok) throw new Error('harness: jQuery would not call success for this answer: ' + JSON.stringify(response));
        runGuarded('form success', world.ajaxForm.success, [result.data, result.textStatus, result.xhr, $('form#taskform')], null);
    };
    // #9106: um ouvinte de host do `form-valid` entra À FRENTE da lista (`events` é pública, `cmdhandler.js` `init`): roda antes do
    // `on_form_valid`, que lança com uma resposta sem tarefa e pularia um ouvinte registrado depois dele (o `exc_event_callbacks` não tem
    // `try/catch`). É o que mostra se o `form-valid` foi avisado.
    world.hostFormValidFirst = function (fn) {
        world.cmdasyncform.events['form-valid'].unshift(fn);
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
    function timeoutOf(request) {
        return request.opts.timeout === undefined ? null : request.opts.timeout;
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
            pendingRevoke: pendingOf('POST').length,
            // a opção `timeout` que cada pedido levou ao `$.ajax` (null: nenhuma); o `ajaxForm` do envio, idem
            statusTimeouts: world.statusRequests().map(timeoutOf),
            revokeTimeouts: world.revokeRequests().map(timeoutOf),
            workersTimeouts: world.workersRequests().map(timeoutOf),
            formTimeout: world.ajaxForm && world.ajaxForm.timeout !== undefined ? world.ajaxForm.timeout : null,
            // o que o erro do 400 pôs ao lado de cada campo, e se o campo foi marcado `is-invalid`
            fieldErrors: {
                app_command: fieldParents.app_command.childTexts.slice(),
                args: fieldParents.args.childTexts.slice(),
                kwargs: fieldParents.kwargs.childTexts.slice(),
            },
            invalidFields: {
                app_command: appCommand.classes.has('is-invalid'),
                args: wideFields ? wideFields.args.classes.has('is-invalid') : false,
                kwargs: wideFields ? wideFields.kwargs.classes.has('is-invalid') : false,
            },
            // #9019: a variante estreita dos argumentos (null quando a página não a tem) e o que os campos guardam
            fieldErrorsNarrow: narrowFields ? {args: narrowParents.args.childTexts, kwargs: narrowParents.kwargs.childTexts} : null,
            invalidFieldsNarrow: narrowFields
                ? {args: narrowFields.args.classes.has('is-invalid'), kwargs: narrowFields.kwargs.classes.has('is-invalid')} : null,
            argumentFields: {
                wide: wideFields ? {args: wideFields.args.valueStr, kwargs: wideFields.kwargs.valueStr} : null,
                narrow: narrowFields ? {args: narrowFields.args.valueStr, kwargs: narrowFields.kwargs.valueStr} : null,
            },
            hiddenFields: {args: hiddenFields.args.valueStr, kwargs: hiddenFields.kwargs.valueStr},
            // o campo oculto do comando: a troca de comando na lista o grava (cmdasync-page.js, o clique em `.dropdown-menu > a`), então é o que
            // mostra, sem olhar o código, que a troca teve efeito (#9017: a precondição do contexto `switch` do AC-9017-1)
            appCommandValue: hiddenCommand.valueStr,
            // o que o SCRIPT gravou em cada campo de argumentos por `.val(x)`, na ordem (null: a página não tem a variante)
            argumentWrites: {
                wide: wideFields ? {args: wideFields.args.scriptWrites.slice(), kwargs: wideFields.kwargs.scriptWrites.slice()} : null,
                narrow: narrowFields ? {args: narrowFields.args.scriptWrites.slice(), kwargs: narrowFields.kwargs.scriptWrites.slice()} : null,
            },
            // o que entrou em cada pai por `append` ('element', 'text' ou 'string'), e o que o script entregou como marcação
            fieldAppended: {
                app_command: fieldParents.app_command.appended.slice(),
                args: fieldParents.args.appended.slice(),
                kwargs: fieldParents.kwargs.appended.slice(),
                argsNarrow: narrowParents.args.appended.slice(),
                kwargsNarrow: narrowParents.kwargs.appended.slice(),
            },
            markupSinks: world.markupSinks.slice(),
            feedbackAttached: createdNodes.filter(function (node) { return node.parentNode && node.classes.has('invalid-feedback'); }).length,
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
