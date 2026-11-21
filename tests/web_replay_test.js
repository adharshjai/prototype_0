'use strict';
/*
 * The page (pcldb/web/app.js) against replies of the REAL backend, run with Node:
 *
 *     node tests/web_replay_test.js <replies.json>
 *
 * tests/test_web_static.py (TestPageOnTheRealBackend) starts pcldb's server on demo data, calls
 * every endpoint the page uses, writes the replies into a file, and runs this script on it, once
 * per database engine. The page then runs unchanged on the stand-in browser of
 * tests/web_page_test.js, and every reply it gets is one the real server sent. So a field the
 * server renames, leaves out or sends in another form shows up here as a failure.
 *
 * The file: {"code": the one-time sign-in code, "records": [{name, as, method, path, request,
 * session_path, status, body}]}. "as" is who asked: admin, viewer, bad (a token the server does
 * not know) or "*" (anyone). The tokens are never in the file: this script makes its own and
 * maps them to admin and viewer.
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const page = require('./web_page_test.js');
const pure = require(path.join(__dirname, '..', 'pcldb', 'web', 'app.js'));

const open = page.open;
const audit = page.audit;
const seen = page.seen;
const everyElement = page.everyElement;

const ADMIN = 'pcl_' + 'R3pLay0Admin'.repeat(4);
const VIEWER = 'pcl_' + 'R3pLay0Viewr'.repeat(4);
const ARC_USER = 'demo_arc_user';
const HOST = 'datatransfer.arc.vt.edu';

const file = process.argv[2];
if (!file) {
  console.log('Say which file: node tests/web_replay_test.js <replies.json>');
  process.exit(2);
}
const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
const CODE = saved.code;
const RECORDS = saved.records;
const SECRETS = [ADMIN, VIEWER];

function record(name) {
  const found = RECORDS.filter(function (item) { return item.name === name; });
  assert.strictEqual(found.length, 1, 'the file has no single record "' + name + '"');
  return found[0];
}

function records(prefix) {
  return RECORDS.filter(function (item) { return item.name.indexOf(prefix) === 0; });
}

function same(a, b) {
  return JSON.stringify(a === undefined ? null : a) === JSON.stringify(b === undefined ? null : b);
}

// ---- the replaying backend -------------------------------------------------------------------------

// Every request the page makes must have a recorded reply. One that has none is a request this
// test did not foresee: it is answered with a 404 and noted, and the test fails at the end.
function replay(changes) {
  const unmatched = [];
  const extra = changes || {};
  function reply(item) {
    if (item.name === 'signin') {
      const body = JSON.parse(JSON.stringify(item.body));
      body.token = ADMIN;
      return {status: item.status, body: body};
    }
    return {status: item.status, body: JSON.parse(JSON.stringify(item.body))};
  }
  const answer = function (request) {
    const address = request.path.split('?')[0];
    if (request.method === 'POST' && address === '/api/v1/signin') {
      return reply(record(request.body && request.body.code === CODE ? 'signin' : 'signin_again'));
    }
    const bearer = (request.headers.Authorization || '').replace(/^Bearer /, '');
    let who = bearer === ADMIN ? 'admin' : (bearer === VIEWER ? 'viewer' : 'bad');
    if (extra.signedOut && extra.signedOut(request)) who = 'bad';
    if (who === 'bad') return reply(record('me_bad_token'));
    const key = request.method + ' ' + address;
    if (extra[key]) return extra[key](request, who);
    let sessionPath;
    if (address === '/api/v1/session') {
      const query = request.path.split('?')[1] || '';
      assert.ok(query.indexOf('path=') === 0, 'the session is asked for with ?path=');
      sessionPath = decodeURIComponent(query.slice(5));
    }
    const candidates = RECORDS.filter(function (item) {
      if (item.method !== request.method || item.path !== address) return false;
      if (item.as !== '*' && item.as !== who) return false;
      if (sessionPath !== undefined) return item.session_path === sessionPath;
      return request.method !== 'POST' || same(item.request, request.body);
    });
    if (candidates.length === 0 && sessionPath !== undefined) return reply(record('session_missing'));
    if (candidates.length === 0) {
      unmatched.push(who + ' ' + key + ' ' + (request.raw || ''));
      return {status: 404, body: {error: {code: 'not_recorded', message: 'No reply was recorded for this.', details: null}}};
    }
    return reply(candidates[candidates.length - 1]);
  };
  answer.unmatched = unmatched;
  return answer;
}

async function signedIn(options) {
  const o = options || {};
  const backend = replay(o.changes);
  const browser = await open(Object.assign({token: o.who === 'viewer' ? VIEWER : ADMIN, backend: backend}, o));
  browser.backend = backend;
  return browser;
}

function done(browser) {
  assert.deepStrictEqual(browser.backend.unmatched, [], 'requests without a recorded reply');
  audit(browser, SECRETS);
  // The one-time code goes to /signin in the body, and nowhere else.
  browser.log.requests.forEach(function (request) {
    if (request.path !== '/api/v1/signin') assert.ok((request.path + (request.raw || '')).indexOf(CODE) === -1, 'the code was sent');
  });
  browser.log.addresses.forEach(function (entry) { assert.ok(entry.address.indexOf(CODE) === -1, 'the code stayed in the address'); });
  browser.log.texts.forEach(function (text) { assert.ok(text.indexOf(CODE) === -1, 'the code is on the page'); });
  textOnly(browser);
}

// No element holds markup set by the script (no innerHTML, outerHTML), and nothing that runs,
// loads or frames anything is in the page, whatever the records held.
function textOnly(browser) {
  everyElement(browser.root).forEach(function (element) {
    ['innerHTML', 'outerHTML', 'insertAdjacentHTML'].forEach(function (name) {
      assert.ok(!Object.prototype.hasOwnProperty.call(element, name), name + ' was used on a <' + element.tagName + '>');
    });
    if (element.byScript) {
      assert.ok(['script', 'img', 'iframe', 'svg', 'object', 'embed', 'style', 'link', 'form', 'input'].indexOf(element.tagName) === -1,
        'the script made a <' + element.tagName + '>');
    }
  });
}

// The page writes every text through showHidden() and cuts long values; this is what a value
// looks like on the page (spaces folded, as seen() folds them).
function onPage(text) {
  return pure.showHidden(String(text)).replace(/\s+/g, ' ').trim();
}

function includes(haystack, needle, what) {
  assert.ok(haystack.indexOf(needle) !== -1, 'missing on the page: ' + (what ? what + ': ' : '') +
    JSON.stringify(needle.slice(0, 200)) + '\n--- page text: ' + haystack.slice(0, 1500));
}

function buttonsWithClass(browser, id, name) {
  return browser.byId(id).querySelectorAll('button').filter(function (button) { return button.classList.contains(name); });
}

async function ask(browser, question, includeSimulated) {
  await browser.type('ask-question', question);
  browser.byId('ask-simulated').checked = includeSimulated !== false;
  await browser.click(browser.byId('ask-run'));
}

function askRecord(question, includeSimulated, who) {
  const found = RECORDS.filter(function (item) {
    return item.path === '/api/v1/ask' && item.as === (who || 'admin') && same(item.request,
      {question: question, include_simulated: includeSimulated});
  });
  assert.strictEqual(found.length, 1, 'no recorded reply for the question ' + question);
  return found[0].body;
}

function sessionRecord(sessionPath) {
  const found = RECORDS.filter(function (item) { return item.session_path === sessionPath; });
  assert.strictEqual(found.length, 1, 'no recorded session ' + sessionPath);
  return found[0].body;
}

const tests = [];
function test(name, body) {
  tests.push({name: name, body: body});
}

// ---- the fields the page reads ---------------------------------------------------------------------

// What app.js reads from each reply, with the type it expects (section 11's "Exact result
// shapes"). 'str?' and 'num?' may also be null; '*' is anything; [spec] is a list of spec; an
// object lists keys that must be there (more keys are allowed: tests/test_server.py pins the
// exact sets). A renamed or retyped field fails here even where the page would quietly show a gap.
const S = {
  user: {name: 'str', display_name: 'str?', role: 'str'},
  error: {error: {code: 'str', message: 'str', details: '*'}},
  sqlResult: {columns: [{name: 'str', type: 'str'}], rows: [['*']], row_count: 'num', truncated: 'bool', elapsed_ms: 'num'}
};
const SHAPES = {
  signin: {expires_utc: 'utc', user: S.user},
  me: {user: S.user, capabilities: ['str'], engine: {name: 'str', version: 'str'},
    defaults: {include_simulated: 'bool', sql_max_rows: 'num'},
    storage: {ssh_host: 'str', project_path: 'str', collection_id: 'str'}, version: 'str'},
  status: {
    database: {engine: 'str', version: 'str', where: 'str', sessions: 'num', files: 'num', runs: 'num', points: 'num',
      demo: 'bool', instruments: [{instrument: 'str', sessions: 'num'}], newest_started_utc: 'utc?'},
    search: {backend: 'str', index_set: 'bool', signed_in: 'bool',
      entries: {pending: 'num', sent: 'num', synced: 'num', failed: 'num', delete_pending: 'num'}},
    assistant: {enabled: 'bool', key_present: 'bool', model: 'str', state: 'str', note: 'str?'},
    registrations: [{at_utc: 'utc', user_name: 'str?', session_path: 'str', action: 'str', source: 'str'}]},
  ask: {question: 'str', answer: 'str?', answer_by: 'str', planned_by: 'str', interpretation: 'str', request: 'obj',
    backend: 'str', notices: ['str'], total: 'num', count: 'num',
    results: [{subject: 'str', path: 'str', folder: 'str', instrument: 'str', simulated: 'bool', is_copy: 'bool',
      withdrawn: 'bool', corrected: 'bool'}],
    facets: [{name: 'str', field_name: 'str', buckets: [{value: '*', count: 'num'}]}],
    timing_ms: {plan: 'num', search: 'num', answer: 'num'}},
  session: {session: {path: 'str', folder: 'str', storage_path: 'str', globus_url: 'str', web_url: 'str',
    simulated: 'bool', withdrawn: 'bool', corrected: 'bool', is_copy: 'bool', upload_json: '*'},
  files: {total: 'num', shown: 'num', items: [{rel_path: 'str', bytes: 'num', sha256: 'str?', listed: 'bool'}]},
  runs: [{path: 'str', folder: 'str?', kind: 'str?', made_utc: 'utc?', model_name: 'str?', run_by: 'str?', valid: 'bool'}],
  labels: [{label: 'str', count: 'num'}], review_state: 'str', n_needs_review: 'num'},
  sql: S.sqlResult,
  schema: {engine: 'str', schema: 'str', objects: [{name: 'str', kind: 'str', description: 'str?',
    columns: [{name: 'str', type: 'str', nullable: 'bool', description: 'str?'}]}]},
  examples: {engine: 'str', examples: [{title: 'str', sql: 'str'}]},
  history: {items: [{at_utc: 'utc', query_text: 'str', ok: 'bool', n_rows: 'num?', elapsed_ms: 'num?', error_text: 'str?'}]},
  error: S.error
};

function checkShape(value, spec, where, problems) {
  if (spec === '*') return;
  if (typeof spec === 'string') {
    const nullable = spec.charAt(spec.length - 1) === '?';
    const kind = nullable ? spec.slice(0, -1) : spec;
    if (value === null && nullable) return;
    const good = {
      str: typeof value === 'string', num: typeof value === 'number' && isFinite(value), bool: typeof value === 'boolean',
      obj: value !== null && typeof value === 'object' && !Array.isArray(value),
      utc: typeof value === 'string' && pure.parseUtc(value) !== null
    }[kind];
    if (!good) problems.push(where + ': expected ' + spec + ', got ' + JSON.stringify(value === undefined ? 'nothing' : value).slice(0, 80));
    return;
  }
  if (Array.isArray(spec)) {
    if (!Array.isArray(value)) {
      problems.push(where + ': expected a list');
      return;
    }
    value.forEach(function (item, index) { checkShape(item, spec[0], where + '[' + index + ']', problems); });
    return;
  }
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    problems.push(where + ': expected an object');
    return;
  }
  Object.keys(spec).forEach(function (key) {
    if (!Object.prototype.hasOwnProperty.call(value, key)) problems.push(where + '.' + key + ': missing');
    else checkShape(value[key], spec[key], where + '.' + key, problems);
  });
}

function shapeOf(item) {
  if (item.status >= 400) return 'error';
  if (item.path === '/api/v1/signin') return 'signin';
  if (item.path === '/api/v1/me') return 'me';
  if (item.path === '/api/v1/status') return 'status';
  if (item.path === '/api/v1/ask') return 'ask';
  if (item.path === '/api/v1/session') return 'session';
  if (item.path === '/api/v1/sql') return 'sql';
  if (item.path === '/api/v1/sql/schema') return 'schema';
  if (item.path === '/api/v1/sql/examples') return 'examples';
  if (item.path === '/api/v1/sql/history') return 'history';
  return null;
}

test('every field the page reads is in the real replies, with the type the page expects', async function () {
  const problems = [];
  const kinds = {};
  RECORDS.forEach(function (item) {
    const kind = shapeOf(item);
    if (kind === null) return;
    kinds[kind] = (kinds[kind] || 0) + 1;
    checkShape(item.body, SHAPES[kind], item.name, problems);
  });
  assert.deepStrictEqual(problems, [], 'replies that do not fit the page');
  assert.deepStrictEqual(Object.keys(kinds).sort(), Object.keys(SHAPES).sort(), 'a reply of every kind was recorded');
  const status = record('status').body;
  assert.ok(['ready', 'off_vpn', 'bad_key', 'no_key', 'disabled', 'error', 'unknown'].indexOf(status.assistant.state) !== -1);
  RECORDS.filter(function (item) { return shapeOf(item) === 'ask'; }).forEach(function (item) {
    assert.ok(['llm', 'none'].indexOf(item.body.answer_by) !== -1 && ['llm', 'rules'].indexOf(item.body.planned_by) !== -1);
    assert.ok(['local', 'globus'].indexOf(item.body.backend) !== -1);
    item.body.results.forEach(function (result) {
      ['started', 'uploaded', 'verified', 'last_run'].forEach(function (key) {
        if (key in result) assert.ok(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(result[key]), key + ' as 2026-11-17T03:50:15Z');
      });
    });
  });
  RECORDS.filter(function (item) { return shapeOf(item) === 'schema'; }).forEach(function (item) {
    item.body.objects.forEach(function (object) { assert.ok(object.kind === 'table' || object.kind === 'view', object.kind); });
  });
});

// ---- sign-in ---------------------------------------------------------------------------------------

test('a one-time sign-in link: the code is swapped for a token, then /me and /status fill the header', async function () {
  const backend = replay();
  const browser = await open({hash: '#signin=' + CODE, backend: backend});
  browser.backend = backend;
  const me = record('me').body;
  assert.ok(browser.shown('view-app'), 'the app is shown after the sign-in link');
  assert.strictEqual(browser.window.sessionStorage.data['pcldb.token'], ADMIN);
  assert.strictEqual(browser.window.location.hash, '#normal');
  assert.strictEqual(browser.seen('user-name'), onPage(me.user.display_name || me.user.name));
  assert.strictEqual(browser.seen('user-role'), me.user.role);
  assert.strictEqual(browser.seen('user-initial'), pure.initialOf(pure.cut(me.user.display_name || me.user.name, 60)));
  assert.strictEqual(browser.shown('demo-chip'), record('status').body.database.demo === true);
  assert.ok(browser.shown('tab-expert'), 'the admin has "sql"');
  assert.strictEqual(browser.byId('ask-simulated').checked, me.defaults.include_simulated !== false);
  includes(browser.seen('sql-limit'), pure.formatNumber(me.defaults.sql_max_rows) + ' rows');
  includes(browser.seen('app-version'), me.version);
  done(browser);

  const again = await open({hash: '#signin=' + CODE + 'x', backend: replay()});
  assert.ok(again.shown('view-signin'));
  includes(again.seen('signin-note'), 'That sign-in link was already used or is more than five minutes old.');
  assert.strictEqual(record('signin_again').status, 401);
});

// ---- Normal ----------------------------------------------------------------------------------------

function checkResults(browser, reply) {
  const text = browser.seen('ask-results');
  const results = reply.results;
  includes(text, pure.countLine(results.length, reply.total));
  includes(text, onPage(reply.interpretation), 'interpretation');
  includes(text, 'Question read by ' + pure.plannerLabel(reply.planned_by));
  includes(text, 'Searched in ' + pure.backendLabel(reply.backend));
  reply.notices.forEach(function (notice) { includes(text, onPage(notice), 'notice'); });
  if (reply.notices.length === 0) assert.strictEqual(browser.byId('ask-results').querySelectorAll('p').filter(function (p) {
    return p.className === 'banner';
  }).length, 0);
  // The request that ran, in the collapsed block.
  includes(text, onPage(JSON.stringify(reply.request, null, 2)).slice(0, 300), 'the request');
  if (reply.answer_by === 'llm' && reply.answer) includes(text, 'Written by the ARC assistant. Check it against the table.');
  else assert.ok(text.indexOf('Written by the ARC assistant') === -1, 'no assistant label without the assistant');
  const rows = browser.byId('ask-results').querySelectorAll('table').filter(function (table) {
    return table.className === 'sessions';
  });
  assert.strictEqual(rows.length, results.length > 0 ? 1 : 0);
  if (results.length > 0) assert.strictEqual(rows[0].querySelectorAll('tr').length, results.length + 1);
  results.forEach(function (result) {
    includes(text, onPage(pure.cut(result.folder, 120)), 'folder');
    includes(text, onPage(result.instrument), 'instrument');
    if (result.labels || result.label_counts) includes(text, onPage(pure.cut(pure.labelsText(result), 300)), 'labels');
    if (result.completion) includes(text, result.completion);
    assert.ok(pure.parseUtc(result.started), 'started is the time text of section 11: ' + result.started);
  });
  const started = results.filter(function (r) { return r.started; });
  const times = browser.byId('ask-results').querySelectorAll('time');
  assert.strictEqual(times.length, started.length);
  times.forEach(function (time, index) {
    assert.strictEqual(time.getAttribute('title'), started[index].started + ' (UTC)');
  });
  reply.facets.forEach(function (facet) {
    includes(text, 'Sessions by ' + pure.fieldWords(facet.field_name));
    facet.buckets.forEach(function (bucket) {
      includes(text, onPage(pure.bucketWords(bucket.value)) + ' ' + pure.formatNumber(bucket.count), 'facet bucket');
    });
  });
}

test('Normal: every example question on the page finds sessions in the demo data', async function () {
  const browser = await signedIn();
  const buttons = browser.byId('ask-examples').querySelectorAll('button');
  assert.ok(buttons.length >= 3);
  for (const button of buttons) {
    // A chip shows a few words; the question it asks is written in index.html.
    const question = button.getAttribute('data-question');
    assert.ok(question && seen(button).length < question.length, 'a short chip for: ' + question);
    await browser.click(button);
    assert.strictEqual(browser.paths('POST /api/v1/ask').pop().body.question, question);
    const reply = askRecord(question, true);
    assert.ok(reply.results.length > 0, 'the example "' + question + '" finds nothing in the demo data');
    checkResults(browser, reply);
  }
  done(browser);
});

test('Normal: results, notices, counts by a field, the request that ran', async function () {
  const browser = await signedIn();
  for (const question of ['newest sessions', 'How many sessions per instrument?']) {
    await ask(browser, question, true);
    checkResults(browser, askRecord(question, true));
  }
  assert.ok(askRecord('How many sessions per instrument?', true).facets.length > 0, 'a count question gives counts');
  done(browser);
});

test('Normal: nothing matched, with and without simulated sessions', async function () {
  const browser = await signedIn();
  await ask(browser, 'Which sessions have adenine?', false);
  let reply = askRecord('Which sessions have adenine?', false);
  assert.strictEqual(reply.total, 0, 'the demo data is all simulated');
  checkResults(browser, reply);
  includes(browser.seen('ask-results'), 'Simulated sessions were left out. Turn on "Include simulated" to search them.');
  if (reply.answer && reply.answer_by !== 'llm') {
    assert.ok(browser.seen('ask-results').indexOf(onPage(reply.answer)) === -1,
      'a sentence not written by the assistant is not shown as its answer (the page says it in its own words)');
  }
  await ask(browser, 'zzqx unheardofword', true);
  reply = askRecord('zzqx unheardofword', true);
  checkResults(browser, reply);
  includes(browser.seen('ask-results'), 'Try other words, or a wider time span.');
  done(browser);
});

// ---- the detail panel ------------------------------------------------------------------------------

function checkSessionFields(browser, reply) {
  const text = browser.seen('detail-body');
  const session = reply.session;
  const groups = pure.groupSessionFields(session);
  const labels = [];
  groups.forEach(function (group) {
    includes(text, group.title);
    group.rows.forEach(function (row) { labels.push(row.key); });
  });
  assert.deepStrictEqual(labels.slice().sort(), Object.keys(session).sort(), 'every field of the session row is shown once');
  assert.ok(groups.every(function (group) { return group.title !== 'Other fields'; }),
    'every column of pcl.sessions has a place in a named group: ' + JSON.stringify(groups.map(function (g) { return g.title; })));
  Object.keys(session).forEach(function (key) {
    const value = session[key];
    if (typeof value === 'string' && value !== '' && key !== 'notes') includes(text, onPage(pure.cut(value, 4000)), key);
    if (key === 'notes' && value) includes(text, onPage(value.slice(0, 2000)), 'notes');
    if (typeof value === 'number' && key !== 'bytes') includes(text, String(value), key);
    if (Array.isArray(value) && value.length > 0 && value.every(function (v) { return typeof v === 'string'; })) {
      includes(text, onPage(pure.listText(value)), key);
    }
  });
  pure.uploadFacts(session.upload_json).forEach(function (fact) { includes(text, fact[0], 'upload fact'); });
  includes(text, pure.fileCountLine(reply.files.items.length, reply.files.total));
  reply.files.items.forEach(function (item) {
    includes(text, onPage(item.rel_path), 'file');
    if (item.sha256) includes(text, pure.shortHash(item.sha256));
  });
  reply.runs.forEach(function (run) { includes(text, onPage(run.folder || pure.lastSegment(run.path)), 'run'); });
  reply.labels.forEach(function (label) { includes(text, onPage(label.label) + ' ' + pure.formatNumber(label.count), 'label'); });
  const sentence = pure.reviewSentence(reply.review_state, reply.n_needs_review);
  if (sentence) includes(text, sentence);
}

function actionsText(browser) {
  const text = browser.seen('detail-body');
  const end = text.indexOf(' Session Folder ');
  return end === -1 ? text : text.slice(0, end);
}

test('detail panel from Normal: every field, files, runs, labels, and the Globus actions only', async function () {
  const browser = await signedIn({arcUser: ARC_USER});
  await ask(browser, 'newest sessions', true);
  const results = askRecord('newest sessions', true).results;
  let opened = 0;
  for (const result of results) {
    const button = browser.buttons('ask-results', pure.cut(result.folder, 120)).filter(function (b) {
      return b.className === 'rowlink';
    })[0];
    assert.ok(button, 'a button opens ' + result.folder);
    await browser.click(button);
    const asked = browser.paths('GET /api/v1/session').pop();
    assert.strictEqual(asked.path, pure.sessionAddress(result.path));
    const reply = sessionRecord(result.path);
    checkSessionFields(browser, reply);
    const actions = actionsText(browser);
    includes(actions, 'Through Globus');
    includes(actions, 'Copy Globus address');
    ['Over SSH', 'Copy ARC path', 'scp ', 'sftp ', 'rsync '].forEach(function (word) {
      assert.ok(actions.indexOf(word) === -1, 'Normal shows no SSH action: ' + word);
    });
    const links = browser.byId('detail-body').querySelectorAll('a');
    assert.strictEqual(links.length, 1, 'one "Open in Globus" link');
    assert.strictEqual(links[0].href, pure.safeGlobusUrl(reply.session.web_url));
    assert.strictEqual(links[0].href, new URL(reply.session.web_url).href);
    assert.strictEqual(links[0].rel, 'noopener noreferrer');
    assert.strictEqual(links[0].target, '_blank');
    await browser.click(browser.button('detail-body', 'Copy Globus address'));
    assert.strictEqual(browser.log.copied.pop(), reply.session.globus_url);
    await browser.click(browser.byId('detail-close'));
    opened += 1;
  }
  assert.ok(opened > 0);
  done(browser);
});

test('detail panel from Expert: a path cell opens it with the ARC path and the SSH commands only', async function () {
  const browser = await signedIn({hash: '#expert', arcUser: ARC_USER});
  const run = record('sql_small');
  await browser.type('sql-text', run.request.sql, 'input');
  await browser.click(browser.byId('sql-run'));
  const pathColumn = run.body.columns.map(function (c) { return c.name; }).indexOf('path');
  const cells = buttonsWithClass(browser, 'sql-result', 'cellpath');
  assert.strictEqual(cells.length, run.body.rows.length, 'every path cell is a button');
  let unusual = 0;
  let plain = 0;
  for (let index = 0; index < cells.length; index += 1) {
    const sessionPath = run.body.rows[index][pathColumn];
    await browser.click(cells[index]);
    assert.strictEqual(browser.paths('GET /api/v1/session').pop().path, pure.sessionAddress(sessionPath));
    const reply = sessionRecord(sessionPath);
    checkSessionFields(browser, reply);
    const actions = actionsText(browser);
    const storage = reply.session.storage_path;
    includes(actions, 'Over SSH');
    includes(actions, 'Copy ARC path');
    includes(actions, 'ARC path: ' + onPage(storage));
    ['Through Globus', 'Open in Globus', 'Copy Globus address'].forEach(function (word) {
      assert.ok(actions.indexOf(word) === -1, 'Expert shows no Globus action: ' + word);
    });
    assert.strictEqual(browser.byId('detail-body').querySelectorAll('a').length, 0);
    const commands = pure.sshCommands(HOST, ARC_USER, storage);
    if (/^[A-Za-z0-9._\/@+-]+$/.test(storage)) {
      assert.ok(commands, 'a plain path gets commands: ' + storage);
      const target = ARC_USER + '@' + HOST + ':' + storage;
      includes(actions, 'scp -r ' + target + '/ .');
      includes(actions, 'sftp ' + target);
      includes(actions, 'rsync -av ' + target + '/ ./' + pure.lastSegment(storage) + '/');
      includes(actions, 'rsync (macOS and Linux)');
      await browser.click(browser.button('detail-body', 'Copy the scp command'));
      assert.strictEqual(browser.log.copied.pop(), 'scp -r ' + target + '/ .');
      plain += 1;
    } else {
      assert.strictEqual(commands, null);
      includes(actions, 'This path has unusual characters. Copy the path and quote it yourself.');
      ['scp ', 'sftp ', 'rsync '].forEach(function (word) { assert.ok(actions.indexOf(word) === -1, word); });
      unusual += 1;
    }
    await browser.click(browser.button('detail-body', 'Copy ARC path'));
    assert.strictEqual(browser.log.copied.pop(), storage);
    await browser.click(browser.byId('detail-close'));
  }
  assert.ok(plain > 0 && unusual > 0, 'the records hold plain paths and the unusual one: ' + plain + ' / ' + unusual);
  browser.log.requests.forEach(function (request) {
    assert.ok((request.path + (request.raw || '')).indexOf(ARC_USER) === -1, 'the ARC user name was sent');
  });
  done(browser);
});

test('detail panel from Status: Globus for everyone, SSH too for a role with "sql"', async function () {
  for (const who of ['admin', 'viewer']) {
    const browser = await signedIn({who: who, hash: '#status', arcUser: ARC_USER});
    const status = record(who === 'admin' ? 'status' : 'status_viewer').body;
    const first = status.registrations.filter(function (r) { return r.action !== 'rejected'; })[0];
    const button = browser.byId('status-registrations').querySelectorAll('button')[0];
    await browser.click(button);
    assert.strictEqual(browser.paths('GET /api/v1/session').pop().path, pure.sessionAddress(first.session_path));
    const actions = actionsText(browser);
    includes(actions, 'Through Globus');
    assert.strictEqual(actions.indexOf('Over SSH') !== -1, who === 'admin');
    done(browser);
  }
});

// ---- Expert ----------------------------------------------------------------------------------------

test('Expert: the tables, the examples and the history from the real backend', async function () {
  const browser = await signedIn({hash: '#expert'});
  const schema = record('sql_schema').body;
  // The columns of each table are behind a button that shows them.
  for (const toggle of browser.byId('schema-list').querySelectorAll('button').filter(function (b) {
    return b.className === 'disclose';
  })) {
    await browser.click(toggle);
    assert.strictEqual(toggle.getAttribute('aria-expanded'), 'true');
  }
  const list = browser.seen('schema-list');
  // Names only on the page; each description is the tooltip of its name.
  const tooltips = buttonsWithClass(browser, 'schema-list', 'insert').map(function (b) { return b.getAttribute('title'); }).join('\n');
  schema.objects.forEach(function (object) {
    includes(list, onPage(object.name), 'table');
    if (object.description) includes(tooltips, onPage(pure.cut(pure.oneLine(object.description), 400)), 'description');
    object.columns.forEach(function (column) {
      includes(list, onPage(column.name), 'column');
      if (column.description) includes(tooltips, onPage(pure.cut(pure.oneLine(column.description), 400)), 'column description');
    });
  });
  const examples = record('sql_examples').body.examples;
  assert.strictEqual(browser.byId('sql-examples').options.length, examples.length + 1);
  includes(browser.seen('sql-dialect'), pure.engineName(record('me').body.engine.name) + ' SQL');
  // Every example loads into the box.
  for (let index = 0; index < examples.length; index += 1) {
    browser.byId('sql-examples').value = String(index);
    browser.byId('sql-examples').dispatch('change', {});
    await browser.settle();
    assert.strictEqual(browser.byId('sql-text').value, examples[index].sql);
  }
  const history = record('sql_history').body.items;
  assert.ok(history.length > 0);
  const shown = browser.seen('history-list');
  history.forEach(function (item) {
    includes(shown, onPage(pure.clipText(pure.oneLine(item.query_text), 160).text), 'history');
    includes(shown, onPage(pure.historyOutcome(item)), 'history outcome');
  });
  done(browser);
});

test('Expert: a result of 1000 rows by 30 columns, cut by the backend, with path cells', async function () {
  const browser = await signedIn({hash: '#expert'});
  const run = record('sql_big');
  assert.strictEqual(run.body.rows.length, 1000);
  assert.strictEqual(run.body.columns.length, 30);
  assert.strictEqual(run.body.truncated, true);
  await browser.type('sql-text', run.request.sql, 'input');
  const began = Date.now();
  await browser.click(browser.byId('sql-run'));
  const took = Date.now() - began;
  const grid = browser.byId('sql-result').querySelectorAll('table')[0];
  assert.strictEqual(grid.querySelectorAll('tbody')[0].childNodes.length, 1000);
  assert.strictEqual(grid.querySelectorAll('th').length, 30);
  includes(browser.seen('sql-result'), pure.rowsLine(run.body.row_count, run.body.elapsed_ms));
  includes(browser.seen('sql-result'), 'Showing the first 1000 rows (there are more)');
  const cells = buttonsWithClass(browser, 'sql-result', 'cellpath');
  assert.strictEqual(cells.length, 1000, 'the column "path" opens sessions; "<script>..." in session_path does not');
  const text = browser.seen('sql-result');
  includes(text, '<script>alert(1)</script>');
  assert.ok(took < 20000, 'the grid took ' + took + ' ms in the stand-in');
  await browser.click(browser.button('sql-result', 'Download CSV'));
  assert.strictEqual(browser.log.blobs.length, 1);
  done(browser);
});

test('Expert: what the database refuses is shown in its own words', async function () {
  const browser = await signedIn({hash: '#expert'});
  for (const name of ['sql_error', 'sql_refused', 'sql_private']) {
    const run = record(name);
    assert.strictEqual(run.status, 400);
    await browser.type('sql-text', run.request.sql, 'input');
    await browser.click(browser.byId('sql-run'));
    assert.ok(browser.shown('sql-error'));
    const text = browser.seen('sql-error');
    includes(text, pure.sqlFailureTitle(run.body.error.code));
    includes(text, onPage(run.body.error.message));
    const details = run.body.error.details;
    if (details && Object.keys(details).length > 0) includes(text, 'More details');
    assert.strictEqual(browser.byId('sql-result').childNodes.length, 0);
  }
  done(browser);
});

// ---- roles and signing out -------------------------------------------------------------------------

test('a role without "sql": no Expert view; a stale #expert lands on Normal; questions still work', async function () {
  const browser = await signedIn({who: 'viewer', hash: '#expert'});
  assert.strictEqual(record('me_viewer').body.capabilities.indexOf('sql'), -1);
  assert.ok(!browser.shown('tab-expert'));
  assert.ok(browser.shown('panel-normal') && !browser.shown('panel-expert'));
  assert.strictEqual(browser.window.location.hash, '#normal');
  assert.strictEqual(browser.paths('GET /api/v1/sql/schema').length, 0, 'nothing of Expert is loaded');
  await browser.go('#expert');
  assert.strictEqual(browser.window.location.hash, '#normal');
  includes(browser.seen('lanes-you'), 'You are viewer: Normal only');
  await ask(browser, 'Which sessions still need review?', true);
  checkResults(browser, askRecord('Which sessions still need review?', true, 'viewer'));
  done(browser);
});

test('a 403 from the backend: a plain sentence with the backend\'s own words', async function () {
  const forbidden = record('sql_viewer_403');
  assert.strictEqual(forbidden.status, 403);
  // The admin's role was changed on the server after the page loaded.
  const browser = await signedIn({hash: '#expert', changes: {
    'POST /api/v1/sql': function () { return {status: forbidden.status, body: forbidden.body}; }
  }});
  await browser.type('sql-text', 'SELECT 1', 'input');
  await browser.click(browser.byId('sql-run'));
  const text = browser.seen('sql-error');
  includes(text, 'Your role (admin) may not run SQL.');
  includes(text, onPage(forbidden.body.error.message));
  done(browser);
});

test('a 401 on any call returns to the sign-in form and forgets the token', async function () {
  const bad = record('me_bad_token');
  assert.strictEqual(bad.status, 401);
  const stale = await open({token: 'pcl_' + 'Stale0Token0'.repeat(4), backend: replay()});
  assert.ok(stale.shown('view-signin'));
  assert.strictEqual(stale.window.sessionStorage.data['pcldb.token'], undefined);
  includes(stale.seen('signin-note'), 'Your sign-in has ended. Sign in again.');

  let ended = false;
  const browser = await signedIn({changes: {signedOut: function () { return ended; }}});
  assert.ok(browser.shown('view-app'));
  ended = true;
  await ask(browser, 'newest sessions', true);
  assert.ok(browser.shown('view-signin'), 'a 401 from /ask signs the page out');
  assert.strictEqual(browser.window.sessionStorage.data['pcldb.token'], undefined);
  assert.strictEqual(browser.seen('ask-results'), '', 'nothing of the last person stays on the page');
  done(browser);
});

// ---- Status ----------------------------------------------------------------------------------------

test('Status: database, search, assistant and the newest registrations in plain words', async function () {
  const status = record('status').body;
  for (const note of [null, 'A sentence about the limits of expert SQL on this server.']) {
    const changes = note === null ? {} : {'GET /api/v1/status': function () {
      const body = JSON.parse(JSON.stringify(status));
      body.database.expert_note = note;
      return {status: 200, body: body};
    }};
    const browser = await signedIn({hash: '#status', changes: changes});
    const text = browser.seen('status-cards');
    const d = status.database;
    includes(text, 'The session records are kept in ' + pure.engineLabel({name: d.engine, version: d.version}));
    includes(text, onPage(pure.cut(d.where, 200)));
    [['Sessions', d.sessions], ['Files', d.files], ['Analysis runs', d.runs], ['Classified points', d.points]].forEach(function (pair) {
      includes(text, pair[0] + ' ' + pure.formatNumber(pair[1]));
    });
    d.instruments.forEach(function (item) { includes(text, onPage(item.instrument) + ' ' + pure.formatNumber(item.sessions)); });
    if (d.demo) includes(text, 'These are made-up demo sessions, not lab data.');
    if (note) includes(text, note);
    else assert.ok(!('expert_note' in d), 'this server has no note about expert SQL');
    pure.entryStateRows(status.search.entries).forEach(function (pair) { includes(text, pair[0] + ' ' + pure.formatNumber(pair[1])); });
    includes(text, pure.assistantWords(status.assistant).sentence);
    const repeated = ['no_key', 'disabled'].indexOf(pure.assistantWords(status.assistant).state) !== -1;
    if (status.assistant.note && !repeated) includes(text, 'The last check said: ' + onPage(status.assistant.note));
    includes(text, onPage(status.assistant.model));
    const registrations = browser.seen('status-registrations');
    status.registrations.forEach(function (item) {
      includes(registrations, onPage(pure.clipText(item.session_path, 200).text), 'registration');
      includes(registrations, pure.actionWords(item.action) + ' ' + pure.sourceWords(item.source));
    });
    includes(browser.seen('panel-status'), 'Every registered session can be found both ways');
    done(browser);
  }
});

// ---- hostile text ----------------------------------------------------------------------------------

test('hostile text from real records only ever reaches the page as text', async function () {
  const hostile = RECORDS.filter(function (item) { return item.name === 'register_hostile'; })[0];
  assert.ok(hostile && hostile.status === 200, 'the hostile session was registered');
  const sessionPath = hostile.body.path;
  const reply = sessionRecord(sessionPath);
  const card = reply.session.card_json;
  assert.ok(JSON.stringify(card).length > 100000, 'about 100 kB of text in the card');
  const browser = await signedIn({hash: '#expert', arcUser: ARC_USER});
  await browser.type('sql-text', record('sql_small').request.sql, 'input');
  await browser.click(browser.byId('sql-run'));
  const cell = buttonsWithClass(browser, 'sql-result', 'cellpath').filter(function (b) {
    return seen(b) === onPage(pure.clipText(sessionPath, 160).text);
  })[0];
  assert.ok(cell, 'the hostile path is a button in the result');
  await browser.click(cell);
  const text = browser.seen('detail-body');
  ['<img src=x onerror=alert(1)>', '"quoted" & \'single\'', "<script>alert('loc')</script>", '"; DROP TABLE x; --',
    'demo-operator-a"><svg onload=alert(1)>', '<script>alert("notes")</script>', '<U+202E>evil<U+202C>'
  ].forEach(function (needle) {
    includes(text, needle, 'hostile value');
    assert.ok(browser.log.texts.some(function (t) { return pure.showHidden(t).indexOf(needle) !== -1; }),
      'arrived as a text node: ' + needle);
  });
  // The card (with its 100 kB value) is in a code block, cut to size, as text.
  const blocks = browser.byId('detail-body').querySelectorAll('pre');
  assert.ok(blocks.some(function (pre) { return pre.textContent.indexOf('BBBBBBBBBB') !== -1; }));
  assert.ok(blocks.every(function (pre) { return pre.textContent.length <= 100001; }));
  // The Normal view gets the same session from the search, and its panel shows the sample ids as text.
  const normal = await signedIn({arcUser: ARC_USER});
  await ask(normal, 'newest sessions', true);
  const found = askRecord('newest sessions', true).results.map(function (item) { return item.path; }).indexOf(sessionPath);
  assert.ok(found !== -1, 'the hostile session is among the newest');
  await normal.click(buttonsWithClass(normal, 'ask-results', 'rowlink')[found]);
  includes(normal.seen('detail-body'), '<img src=x onerror=alert(1)>');
  done(browser);
  done(normal);
});

// ---- run -------------------------------------------------------------------------------------------

(async function () {
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
