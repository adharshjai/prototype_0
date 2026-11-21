'use strict';
/*
 * PCL data: the website of pcldb prototype_0 (section 12 of the specification).
 *
 * One classic script: no framework, no build step, no outside request. The rules it keeps
 * (tests/test_web_static.py checks most of them):
 *
 *   - Text from the backend, from records and from the assistant is untrusted. It reaches the
 *     page only as text nodes and plain attribute values, never as markup, and characters that
 *     cannot be seen (direction overrides, zero-width characters) are shown as codes.
 *   - The only outside link is "Open in Globus", and only when the address passes
 *     safeGlobusUrl(). Anything else is shown as text.
 *   - The access token is kept in sessionStorage and in one variable. It travels only in the
 *     Authorization header: never in an address, in the page, in a message or in the console log.
 *   - The ARC user name a person types is kept in this browser (localStorage) and is put into
 *     the SSH commands on the page. It is never sent anywhere.
 *   - Every request goes through api(), to one of the paths in the table API.
 *
 * The two modes are the two lanes of the specification: Normal (a question in plain English,
 * files through Globus) and Expert (SQL, files over SSH).
 *
 * Part 1 holds small pure functions (no page access). tests/web_pure_test.js runs them with Node.
 * Part 2 is the page.
 */
(function () {

  // =============================================================================================
  // Part 1: pure functions. Nothing here touches the page, the network or storage.
  // =============================================================================================

  // Every backend path this page calls (section 11 of the specification).
  const API = Object.freeze({
    signin: '/api/v1/signin',
    me: '/api/v1/me',
    status: '/api/v1/status',
    ask: '/api/v1/ask',
    session: '/api/v1/session',
    sql: '/api/v1/sql',
    sqlSchema: '/api/v1/sql/schema',
    sqlExamples: '/api/v1/sql/examples',
    sqlHistory: '/api/v1/sql/history'
  });
  const API_PATHS = Object.freeze(Object.keys(API).map(function (name) { return API[name]; }));

  // The three views, as they are kept in the address: #normal, #expert, #status.
  const TABS = Object.freeze(['normal', 'expert', 'status']);
  // Names an early build used; an old link still lands on the right view.
  const OLD_TAB_NAMES = Object.freeze({ask: 'normal', sql: 'expert'});
  const SIGNIN_PREFIX = '#signin=';
  const CODE_SHAPE = /^[A-Za-z0-9_-]{16,128}$/;
  const TOKEN_SHAPE = /^pcl_[A-Za-z0-9_-]{20,200}$/;

  // ---- small value helpers --------------------------------------------------------------------

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function asObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value) ? value : {};
  }

  function own(table, key) {
    return Object.prototype.hasOwnProperty.call(table, key);
  }

  // A value from a fixed table, or the fallback. Keys that come from the backend never reach
  // an object's built-in properties this way.
  function lookup(table, key, fallback) {
    return typeof key === 'string' && own(table, key) ? table[key] : fallback;
  }

  function jsonText(value) {
    try {
      const text = JSON.stringify(value);
      return typeof text === 'string' ? text : String(value);
    } catch (err) {
      return String(value);
    }
  }

  // A JSON value as indented text, for the "request that ran" block and for JSON fields.
  function prettyJson(value) {
    if (value === undefined) return '';
    try {
      const text = JSON.stringify(value, null, 2);
      return typeof text === 'string' ? text : '';
    } catch (err) {
      return '';
    }
  }

  // Any value as display text. Missing values are an empty string.
  function textOf(value) {
    if (value === null || value === undefined) return '';
    if (typeof value === 'string') return value;
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
    return jsonText(value);
  }

  function listText(value) {
    return asArray(value).map(textOf).filter(function (text) { return text !== ''; }).join(', ');
  }

  function lastSegment(path) {
    const parts = textOf(path).split('/').filter(function (part) { return part !== ''; });
    return parts.length > 0 ? parts[parts.length - 1] : '';
  }

  function plural(count, one, many) {
    return count === 1 ? one : many;
  }

  function oneLine(text) {
    return textOf(text).replace(/\s+/g, ' ').trim();
  }

  function firstLine(text) {
    const lines = textOf(text).split(/\r?\n/);
    for (let i = 0; i < lines.length; i += 1) {
      if (lines[i].trim() !== '') return lines[i].trim();
    }
    return '';
  }

  // Cut long text for display. The caller keeps the full text for the title attribute.
  function clipText(text, max) {
    const value = String(text);
    if (value.length <= max) return {text: value, clipped: false};
    let end = max;
    const code = value.charCodeAt(end - 1);
    if (code >= 0xd800 && code <= 0xdbff) end -= 1;   // never cut one character in half
    return {text: value.slice(0, end) + '\u2026', clipped: true};
  }

  // Any value as text of at most max characters, for a place on the page where a very long
  // value (a 100 kB name, say) would make the page hard to use.
  function cut(value, max) {
    return clipText(textOf(value), max).text;
  }

  // Characters a reader cannot see but that change how a line reads: direction overrides and
  // marks (a name can be made to look like another name with them), zero-width characters, the
  // invisible "tag" characters, and control characters other than tab and the line ends.
  const HIDDEN_CHARACTERS =
    /[\u{0}-\u{8}\u{b}\u{c}\u{e}-\u{1f}\u{7f}-\u{9f}\u{ad}\u{61c}\u{180e}\u{200b}-\u{200f}\u{2028}-\u{202e}\u{2060}-\u{206f}\u{feff}\u{fff9}-\u{fffb}\u{e0000}-\u{e007f}]/gu;

  function codePointName(text) {
    const hex = text.codePointAt(0).toString(16).toUpperCase();
    return '<U+' + ('000' + hex).slice(-Math.max(4, hex.length)) + '>';
  }

  // The text with every such character written out as its code, for example <U+202E>. All text
  // that reaches the page goes through this, so what a person reads is what is there.
  function showHidden(value) {
    return textOf(value).replace(HIDDEN_CHARACTERS, codePointName);
  }

  // ---- numbers, sizes, dates ------------------------------------------------------------------

  function formatNumber(value, locale) {
    if (typeof value !== 'number' || !isFinite(value)) return textOf(value);
    try {
      return new Intl.NumberFormat(locale || undefined).format(value);
    } catch (err) {
      return String(value);
    }
  }

  // Sizes in bytes, kB, MB and GB (steps of 1000).
  const SIZE_UNITS = Object.freeze(['kB', 'MB', 'GB']);

  function formatBytes(value) {
    if (typeof value !== 'number' || !isFinite(value) || value < 0) return '';
    if (value < 1000) {
      const whole = Math.round(value);
      return whole + ' ' + plural(whole, 'byte', 'bytes');
    }
    let amount = value / 1000;
    let unit = 0;
    while (amount >= 999.5 && unit < SIZE_UNITS.length - 1) {
      amount /= 1000;
      unit += 1;
    }
    const text = amount < 9.95 ? amount.toFixed(1) : String(Math.round(amount));
    return text + ' ' + SIZE_UNITS[unit];
  }

  // The backend's time text: 2026-11-17T03:50:15Z or 2026-11-17T03:50:15.281Z.
  const UTC_PARTS = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z$/;

  function parseUtc(text) {
    if (typeof text !== 'string') return null;
    const match = UTC_PARTS.exec(text);
    if (!match) return null;
    const month = Number(match[2]);
    const day = Number(match[3]);
    const hour = Number(match[4]);
    const minute = Number(match[5]);
    const second = Number(match[6]);
    if (month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59 || second > 59) {
      return null;
    }
    const millis = match[7] ? Number((match[7] + '00').slice(0, 3)) : 0;
    const time = Date.UTC(Number(match[1]), month - 1, day, hour, minute, second, millis);
    if (isNaN(time)) return null;
    const date = new Date(time);
    // 31 February would quietly become 3 March: a day the month does not have is not a time.
    return date.getUTCDate() === day ? date : null;
  }

  const dateFormatters = {};

  function dateFormatter(seconds, locale, timeZone) {
    const key = (seconds ? 's' : 'm') + '|' + (locale || '') + '|' + (timeZone || '');
    if (!own(dateFormatters, key)) {
      const options = {dateStyle: 'medium', timeStyle: seconds ? 'medium' : 'short'};
      if (timeZone) options.timeZone = timeZone;
      dateFormatters[key] = new Intl.DateTimeFormat(locale || undefined, options);
    }
    return dateFormatters[key];
  }

  // The backend's UTC text as a date and time in the reader's own locale and time zone.
  // options: {seconds, locale, timeZone}; the last two are for tests, the page leaves them out.
  // Returns '' when the text is not a time, so the caller can show the text as it came.
  function formatDateTime(text, options) {
    const date = parseUtc(text);
    if (!date) return '';
    const o = asObject(options);
    try {
      return dateFormatter(Boolean(o.seconds), o.locale, o.timeZone).format(date);
    } catch (err) {
      return '';
    }
  }

  function twoDigits(number) {
    return (number < 10 ? '0' : '') + number;
  }

  function csvFileName(date) {
    const d = date instanceof Date && !isNaN(date.getTime()) ? date : new Date(0);
    return 'pcl_query_' + d.getFullYear() + twoDigits(d.getMonth() + 1) + twoDigits(d.getDate()) +
      '_' + twoDigits(d.getHours()) + twoDigits(d.getMinutes()) + twoDigits(d.getSeconds()) + '.csv';
  }

  // ---- the address bar, the token, the one outside link ---------------------------------------

  // What the part of the address after "#" means: a one-time sign-in code, a tab, or nothing.
  // A sign-in link with a damaged code still counts as a sign-in link (code: null), so the page
  // always takes it out of the address bar.
  function parseHash(hash) {
    const text = typeof hash === 'string' ? hash : '';
    if (text.indexOf(SIGNIN_PREFIX) === 0) {
      const code = text.slice(SIGNIN_PREFIX.length);
      return {kind: 'signin', code: CODE_SHAPE.test(code) ? code : null};
    }
    const name = text.charAt(0) === '#' ? text.slice(1) : text;
    if (TABS.indexOf(name) !== -1) return {kind: 'tab', tab: name};
    if (own(OLD_TAB_NAMES, name)) return {kind: 'tab', tab: OLD_TAB_NAMES[name]};
    return {kind: 'none'};
  }

  // The view to show: the one asked for, or Normal when the person may not use Expert (it needs
  // the capability "sql") or the name is not a view at all.
  function viewFor(tab, capabilities) {
    if (TABS.indexOf(tab) === -1) return 'normal';
    if (tab === 'expert' && asArray(capabilities).indexOf('sql') === -1) return 'normal';
    return tab;
  }

  function looksLikeToken(text) {
    return typeof text === 'string' && TOKEN_SHAPE.test(text);
  }

  function isApiPath(path) {
    return typeof path === 'string' && API_PATHS.indexOf(path.split('?')[0]) !== -1;
  }

  function sessionAddress(path) {
    return API.session + '?path=' + encodeURIComponent(textOf(path));
  }

  // The only outside link this page ever makes is "Open in Globus", and only to the Globus
  // web app: an address that starts with https://app.globus.org/ (protocol and host checked on
  // the parsed address AND on the text as it came, so nothing a browser would quietly rewrite
  // gets through). Returns the address to link to, or null: then the caller shows the value as
  // plain text.
  function safeGlobusUrl(value) {
    if (typeof value !== 'string' || value.length === 0 || value.length > 4096) return null;
    // Only visible ASCII characters, and no backslash. A browser drops tabs and line ends inside
    // an address, reads a backslash as a slash, and maps look-alike letters in a host name to
    // plain ones. An address from the backend is percent-encoded and needs none of these.
    if (!/^[!-\[\]-~]+$/.test(value)) return null;
    let url;
    try {
      url = new URL(value);   // no base address: relative and protocol-relative values fail here
    } catch (err) {
      return null;
    }
    if (url.protocol !== 'https:') return null;
    if (url.host !== 'app.globus.org') return null;   // host holds the port too, so another port fails
    if (url.hostname !== 'app.globus.org' || url.port !== '') return null;
    if (url.username !== '' || url.password !== '') return null;
    const start = url.protocol + '//' + url.host + '/';
    if (value.slice(0, start.length) !== start) return null;
    return url.href;
  }

  // ---- Expert mode: the commands that reach a session's files over SSH --------------------------

  const USER_PLACEHOLDER = 'YOUR_ARC_USERNAME';
  const SHELL_SAFE = /^[A-Za-z0-9._\/@+-]+$/;
  const USER_NAME_SHAPE = /^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$/;

  // True when the text holds only A-Z a-z 0-9 . _ / @ + - (no space, quote or other character
  // that means something to a shell). Section 12 of the specification: only such text may be
  // put into a command that a person will paste into a terminal.
  function shellSafe(text) {
    return typeof text === 'string' && SHELL_SAFE.test(text);
  }

  // What is wrong with a typed ARC user name, as a sentence, or '' when it can be used.
  function userNameProblem(text) {
    if (typeof text !== 'string' || text === '') return '';
    if (USER_NAME_SHAPE.test(text)) return '';
    return 'A user name has only letters, digits, dots, hyphens and underscores, and at most 64 of them.';
  }

  // The commands for one session, or null when they must not be offered.
  //   host: /me storage.ssh_host;  user: the typed ARC user name ('' = the placeholder);
  //   path: the session's storage_path on ARC.
  // They are offered ONLY when path, host and user name all pass shellSafe(). On top of that, a
  // word that starts with "-" would be read as an option, and a path on ARC starts with "/".
  // Returns {user, placeholder, commands: [{id, label, command}]}.
  function sshCommands(host, user, path) {
    const placeholder = user === '' || user === null || user === undefined;
    const name = placeholder ? USER_PLACEHOLDER : user;
    if (!shellSafe(path) || !shellSafe(host) || !shellSafe(name)) return null;
    if (name.charAt(0) === '-' || host.charAt(0) === '-' || path.charAt(0) !== '/') return null;
    const folderPath = path.replace(/\/+$/, '');
    const folder = lastSegment(folderPath);
    if (folder === '' || folder.charAt(0) === '-') return null;
    const target = name + '@' + host + ':' + folderPath;
    return {
      user: name,
      placeholder: placeholder,
      commands: [
        {id: 'scp', label: 'scp: copy the folder to this computer',
          command: 'scp -r ' + target + '/ .'},
        {id: 'sftp', label: 'sftp: look around in the folder',
          command: 'sftp ' + target},
        {id: 'rsync', label: 'rsync (macOS and Linux): copy the folder, or bring your copy up to date',
          command: 'rsync -av ' + target + '/ ./' + folder + '/'}
      ]
    };
  }

  // ---- CSV ------------------------------------------------------------------------------------

  const FORMULA_START = /^[=+\-@\t\r]/;
  const PLAIN_NUMBER = /^-?\d+(\.\d+)?([eE][+-]?\d+)?$/;

  // One CSV field (RFC 4180). NULL is an empty field, an empty text is "" (as PostgreSQL's COPY
  // writes them). A cell that starts with = + - @ (or a tab or return) gets a single quote in
  // front so a spreadsheet shows it instead of running it as a formula; a plain number such as
  // -5 or -1.5e-7 cannot be a formula and is left alone so it stays a number.
  function csvCell(value) {
    if (value === null || value === undefined) return '';
    let text;
    if (typeof value === 'string') text = value;
    else if (typeof value === 'boolean') text = value ? 'true' : 'false';
    else if (typeof value === 'number') text = String(value);
    else text = jsonText(value);
    if (text === '') return '""';
    if (FORMULA_START.test(text) && !PLAIN_NUMBER.test(text)) text = "'" + text;
    if (/[",\r\n]/.test(text)) text = '"' + text.replace(/"/g, '""') + '"';
    return text;
  }

  // The whole file: a UTF-8 byte order mark (so Excel reads it as UTF-8), a header line, one
  // line per row, CRLF line ends. columns: the column names; rows: lists of values.
  function buildCsv(columns, rows) {
    const names = asArray(columns);
    const lines = [names.map(csvCell).join(',')];
    asArray(rows).forEach(function (row) {
      const values = asArray(row);
      const cells = [];
      for (let i = 0; i < names.length; i += 1) cells.push(csvCell(values[i]));
      lines.push(cells.join(','));
    });
    return '\ufeff' + lines.join('\r\n') + '\r\n';
  }

  // ---- words for the Normal view --------------------------------------------------------------

  function plannerLabel(plannedBy) {
    if (plannedBy === 'llm') return 'assistant';
    if (plannedBy === 'rules') return 'keyword rules';
    return textOf(plannedBy) || 'not stated';
  }

  function backendLabel(backend) {
    if (backend === 'globus') return 'Globus Search';
    if (backend === 'local') return "this computer's copy of the index";
    return textOf(backend) || 'not stated';
  }

  // The few words above the results: "4 sessions", "10 of 23 sessions", "No sessions matched".
  function countLine(count, total) {
    const shown = typeof count === 'number' && count > 0 ? count : 0;
    const all = typeof total === 'number' && total > shown ? total : shown;
    if (all === 0) return 'No sessions matched';
    if (shown === 0) return all + ' ' + plural(all, 'session', 'sessions') + ' matched, but none is listed here';
    if (shown < all) return shown + ' of ' + all + ' sessions';
    return all + ' ' + plural(all, 'session', 'sessions');
  }

  // The first letter or digit of a name, for the round mark in the header ('?' when it has none).
  function initialOf(name) {
    const letters = Array.from(textOf(name)).filter(function (character) {
      return /[\p{L}\p{N}]/u.test(character);
    });
    return letters.length > 0 ? letters[0].toUpperCase().slice(0, 2) : '?';
  }

  // One muted line on the Status view: who the person is and which modes they may use.
  function lanesLine(role, canSql) {
    const modes = canSql ? 'Normal and Expert' : 'Normal only';
    return role ? 'You are ' + role + ': ' + modes : 'You can use ' + modes;
  }

  // "adenine (3), r6g (2)" when the entry has counts, else the plain list of labels.
  function labelsText(result) {
    const r = asObject(result);
    const counted = asArray(r.label_counts).map(asObject).filter(function (item) {
      return textOf(item.label) !== '';
    });
    if (counted.length > 0) {
      return counted.map(function (item) {
        return typeof item.count === 'number' ? textOf(item.label) + ' (' + item.count + ')' : textOf(item.label);
      }).join(', ');
    }
    return listText(r.labels);
  }

  function needsReviewCount(result) {
    const count = asObject(result).n_needs_review;
    return typeof count === 'number' && count > 0 ? count : 0;
  }

  // 'ok', 'warn', 'bad' or 'plain': how to mark a completion value.
  function completionKind(completion) {
    return lookup({complete: 'ok', partial: 'warn', failed: 'bad'}, completion, 'plain');
  }

  const FIELD_WORDS = Object.freeze({
    instrument: 'instrument', labels: 'label', 'label_counts.label': 'label', sample_ids: 'sample',
    tags: 'tag', models: 'model', method: 'method', completion: 'completion',
    review_state: 'review state', location: 'location', format: 'format', problems: 'problem',
    simulated: 'simulated or not'
  });

  // A search field name in plain words, for the title of a count table.
  function fieldWords(field) {
    const name = textOf(field);
    return lookup(FIELD_WORDS, name, name.replace(/[_.]+/g, ' ').trim() || 'value');
  }

  // One value of a count table: yes and no for true and false, else the value as text.
  function bucketWords(value) {
    if (value === true) return 'yes';
    if (value === false) return 'no';
    return textOf(value);
  }

  function timingText(timing) {
    const t = asObject(timing);
    const parts = [];
    function known(value) { return typeof value === 'number' && isFinite(value) && value >= 0; }
    if (known(t.plan)) parts.push('reading the question ' + Math.round(t.plan) + ' ms');
    if (known(t.search)) parts.push('searching ' + Math.round(t.search) + ' ms');
    if (known(t.answer)) parts.push('writing the answer ' + Math.round(t.answer) + ' ms');
    return parts.length > 0 ? 'Time taken: ' + parts.join(', ') + '.' : '';
  }

  function reviewSentence(reviewState, needsReview) {
    const count = typeof needsReview === 'number' && needsReview > 0 ? needsReview : 0;
    if (count > 0) return count + ' ' + plural(count, 'point still needs', 'points still need') + ' review.';
    if (reviewState === 'done') return 'The flagged points have been reviewed.';
    if (reviewState === 'pending') return 'Some points still need review.';
    if (reviewState === 'none') return 'No point was flagged for review.';
    return '';
  }

  // ---- words for the header, the Expert view and the Status view --------------------------------

  const ENGINE_NAMES = Object.freeze({postgresql: 'PostgreSQL', sqlite: 'SQLite'});

  function engineName(name) {
    const text = textOf(name);
    return lookup(ENGINE_NAMES, text.toLowerCase(), text);
  }

  // {name: 'postgresql', version: '18.7'} -> 'PostgreSQL 18.7'
  function engineLabel(engine) {
    const e = asObject(engine);
    const name = engineName(e.name);
    const version = textOf(e.version);
    return (name + (name && version ? ' ' + version : '')).trim();
  }

  // A table or column name as it can be typed into a query.
  function sqlIdentifier(name) {
    const text = textOf(name);
    if (/^[a-z_][a-z0-9_]*$/.test(text)) return text;
    return '"' + text.replace(/"/g, '""') + '"';
  }

  // The session path a result cell stands for, or null. A cell in a column named "path" or
  // "session_path" whose text starts with a letter or a digit opens that session (section 12).
  function sessionPathOfCell(columnName, value) {
    if (columnName !== 'path' && columnName !== 'session_path') return null;
    if (typeof value !== 'string' || value.length > 1024) return null;
    return /^[A-Za-z0-9]/.test(value) ? value : null;
  }

  // How one result value is shown in the grid.
  function cellDisplay(value) {
    if (value === null || value === undefined) return {text: 'NULL', kind: 'null'};
    if (typeof value === 'boolean') return {text: value ? 'true' : 'false', kind: 'bool'};
    if (typeof value === 'number') return {text: String(value), kind: 'number'};
    if (typeof value === 'string') return {text: value, kind: 'text'};
    return {text: jsonText(value), kind: 'json'};
  }

  function rowsLine(rowCount, elapsedMs) {
    const count = typeof rowCount === 'number' && rowCount > 0 ? rowCount : 0;
    let line = count + ' ' + plural(count, 'row', 'rows');
    if (typeof elapsedMs === 'number' && elapsedMs >= 0) line += ' in ' + Math.round(elapsedMs) + ' ms';
    return line;
  }

  function truncatedLine(shown) {
    return 'Showing the first ' + shown + ' ' + plural(shown, 'row', 'rows') + ' (there are more)';
  }

  function sqlFailureTitle(code) {
    if (code === 'sql_refused') return 'This query is not allowed';
    if (code === 'sql_timeout') return 'The query took too long and was stopped';
    if (code === 'sql_error') return 'The database could not run this query';
    return 'The query was not run';
  }

  function historyOutcome(item) {
    const i = asObject(item);
    if (i.ok === true) return typeof i.n_rows === 'number' ? rowsLine(i.n_rows, i.elapsed_ms) : 'ran';
    const reason = clipText(firstLine(i.error_text), 140).text;
    return reason ? 'did not run: ' + reason : 'did not run';
  }

  function shortHash(hash) {
    const text = textOf(hash);
    return text.length > 16 ? text.slice(0, 12) + '\u2026' : text;
  }

  function fileCountLine(shown, total) {
    const s = typeof shown === 'number' && shown > 0 ? shown : 0;
    const t = typeof total === 'number' && total > s ? total : s;
    if (t === 0) return 'No files are listed.';
    if (s < t) return 'Showing the first ' + s + ' of ' + t + ' files';
    return t + ' ' + plural(t, 'file', 'files');
  }

  const ACTION_WORDS = Object.freeze({
    created: 'registered', updated: 'replaced', unchanged: 'already registered, nothing changed',
    rejected: 'refused'
  });

  function actionWords(action) {
    return lookup(ACTION_WORDS, action, textOf(action));
  }

  const SOURCE_WORDS = Object.freeze({
    upload: 'an upload', tree: 'a folder tree', index: 'the nightly index', demo: 'demo data'
  });

  // Where a registration came from, in plain words.
  function sourceWords(source) {
    return lookup(SOURCE_WORDS, source, textOf(source));
  }

  // For each state: [kind of the coloured dot, its title, the one plain line of the card, a few
  // words for the list of facts, the whole sentence for "Details"]. Without the assistant,
  // keyword rules read the questions and no written answer is given: each sentence says so.
  const ASSISTANT_STATES = Object.freeze({
    ready: ['ok', 'Ready', 'Reads questions and writes short answers.', 'can be reached',
      'The ARC assistant can be reached. It reads each question and writes the short answer above the table.'],
    off_vpn: ['warn', 'Off the VT network', 'Connect to the VT VPN to use it.',
      'cannot be reached: this computer is off the VT network',
      'The ARC assistant cannot be reached now: this computer is not on the VT campus network or VPN. ' +
      'Connect to the VT VPN to use it. Until then keyword rules read the questions, and no written answer is given.'],
    bad_key: ['bad', 'Key refused', 'Save a new key from llm.arc.vt.edu.', 'the saved key was refused',
      'The ARC assistant refused the key saved on this computer. Make a new key at llm.arc.vt.edu and save it ' +
      'again. Until then keyword rules read the questions, and no written answer is given.'],
    no_key: ['off', 'No key', 'Keyword rules read the questions.', 'no key is saved',
      'No ARC assistant key is saved on this computer. Keyword rules read the questions, and no written ' +
      'answer is given.'],
    disabled: ['off', 'Switched off', 'Keyword rules read the questions.', 'switched off',
      'The assistant is switched off in the settings. Keyword rules read the questions, and no written ' +
      'answer is given.'],
    error: ['warn', 'Not answering', 'Keyword rules read the questions for now.', 'did not answer the last check',
      'The ARC assistant did not answer the last check. While it cannot be reached, keyword rules read the ' +
      'questions and no written answer is given.'],
    unknown: ['off', 'Not checked', 'Keyword rules read the questions until it answers.', 'not checked yet',
      'The ARC assistant has not been checked yet. If it cannot be reached, keyword rules read the ' +
      'questions and no written answer is given.']
  });

  // What the Status view says about the assistant: {state, kind, title, line, short, sentence}.
  // The settings win over the last check: switched off or no key means it is not used whatever
  // the check said.
  function assistantWords(assistant) {
    const a = asObject(assistant);
    let state = lookup(ASSISTANT_STATES, a.state, null) ? a.state : 'unknown';
    if (a.enabled !== true) state = 'disabled';
    else if (a.key_present !== true) state = 'no_key';
    const words = ASSISTANT_STATES[state];
    return {state: state, kind: words[0], title: words[1], line: words[2], short: words[3], sentence: words[4]};
  }

  function countOf(table, key) {
    const value = asObject(table)[key];
    return typeof value === 'number' && isFinite(value) && value > 0 ? value : 0;
  }

  // What the Status view says about the search: {kind, title, line}. The copy on this computer is
  // never called Globus Search.
  function searchState(search) {
    const s = asObject(search);
    if (s.backend !== 'globus') {
      return {kind: 'ok', title: 'Local index', line: "Questions search this computer's copy of the index."};
    }
    const failedCount = countOf(s.entries, 'failed');
    const waiting = countOf(s.entries, 'pending') + countOf(s.entries, 'sent') + countOf(s.entries, 'delete_pending');
    if (failedCount > 0) {
      return {kind: 'bad', title: 'Some not sent', line: failedCount + ' ' + plural(failedCount, 'entry', 'entries') +
        ' could not be sent.'};
    }
    if (waiting > 0) {
      return {kind: 'warn', title: 'Entries waiting', line: waiting + ' ' + plural(waiting, 'entry waits', 'entries wait') +
        ' to be sent.'};
    }
    return {kind: 'ok', title: 'Globus Search', line: 'Questions are searched with Globus Search.'};
  }

  const ENTRY_STATES = Object.freeze([
    ['pending', 'Waiting to be sent to Globus Search'],
    ['sent', 'Sent, being checked'],
    ['synced', 'In Globus Search'],
    ['failed', 'Could not be sent'],
    ['delete_pending', 'Waiting to be removed']
  ]);

  // [[words, count], ...] for the counts of index entries by state: the known states in a fixed
  // order, then any state this page does not know by its own name.
  function entryStateRows(entries) {
    const e = asObject(entries);
    const rows = [];
    const known = [];
    ENTRY_STATES.forEach(function (pair) {
      known.push(pair[0]);
      if (own(e, pair[0])) rows.push([pair[1], e[pair[0]]]);
    });
    Object.keys(e).sort().forEach(function (name) {
      if (known.indexOf(name) === -1) rows.push([name.replace(/_/g, ' '), e[name]]);
    });
    return rows;
  }

  // ---- the session detail: every field, in readable groups ------------------------------------

  const SESSION_GROUPS = Object.freeze([
    {title: 'Session', fields: [
      ['folder', 'Folder'], ['session_name', 'Session name'], ['session_id', 'Session id'],
      ['instrument', 'Instrument'], ['area', 'Area'], ['simulated', 'Simulated'],
      ['method', 'Method'], ['format', 'Format'], ['completion', 'Completion'],
      ['items', 'Items measured'], ['items_planned', 'Items planned'],
      ['is_copy', 'Copy of another session'], ['corrected', 'Corrected after upload'],
      ['withdrawn', 'Withdrawn']]},
    {title: 'Times', fields: [
      ['started_utc', 'Started'], ['ended_utc', 'Ended'], ['uploaded_utc', 'Uploaded'],
      ['registered_utc', 'Registered'], ['updated_utc', 'Record last changed']]},
    {title: 'Sample and notes', fields: [
      ['sample_ids', 'Samples'], ['operator', 'Operator'], ['location', 'Location'],
      ['tags', 'Tags'], ['notes', 'Notes']]},
    {title: 'How it got to ARC', fields: [
      ['upload_json', 'Upload'], ['upload_id', 'Upload id'], ['n_files', 'Number of files'],
      ['bytes', 'Total size'], ['sums_sha256', 'SHA-256 of the SHA256SUMS file'],
      ['verified_utc', 'Files last checked'], ['verify_ok', 'Last check passed']]},
    {title: 'Where the files are', fields: [
      ['storage_path', 'ARC path'], ['data_path', 'Path in the Globus collection'],
      ['collection_id', 'Globus collection id'], ['globus_url', 'Globus address'],
      ['web_url', 'Address in the Globus web app'], ['path', 'Path inside the project folder'],
      ['subject', 'Name of the search entry']]},
    {title: 'Record keeping', fields: [
      ['registered_by', 'Registered by'], ['source', 'Registered from'],
      ['doc_sha256', 'SHA-256 of the registration'], ['has_card', 'Has a session card'],
      ['corrections_json', 'Corrections'], ['card_json', 'Session card as it was written']]}
  ]);

  const TRANSPORT_WORDS = Object.freeze({
    globus: 'Globus Transfer', ssh: 'an upload over SSH', local: 'a copy made on the same computer'
  });
  const CHECK_WORDS = Object.freeze({
    'sha256-on-arc': 'ARC computed the SHA-256 of every file, and each one matched (the strongest check)',
    'globus-checksum': 'Globus gave a checksum for every file, and each one matched',
    'globus-verify': "Globus's own check during the transfer, plus the list of names and sizes (the weakest check)",
    none: 'no check was made'
  });

  // How a session got to ARC, from its upload record (the column upload_json): rows of
  // [label, value, kind] where kind is 'text', 'time' or 'mono'. An empty list when there is no
  // record, or it is not an object.
  function uploadFacts(upload) {
    const u = asObject(upload);
    const rows = [];
    if (own(u, 'transport')) rows.push(['Sent with', lookup(TRANSPORT_WORDS, u.transport, textOf(u.transport)), 'text']);
    if (own(u, 'checked')) {
      rows.push(['Copy on ARC confirmed', u.checked === true ? 'yes'
        : (u.checked === false ? 'no: the newest check did not confirm it' : textOf(u.checked)), 'text']);
    }
    if (own(u, 'check_method')) {
      rows.push(['How it was checked', lookup(CHECK_WORDS, u.check_method, textOf(u.check_method)), 'text']);
    }
    if (own(u, 'checked_utc')) rows.push(['Checked', u.checked_utc, 'time']);
    if (own(u, 'upload_id')) rows.push(['Upload id', textOf(u.upload_id), 'mono']);
    return rows;
  }

  // [{title, rows: [{key, label, value}]}]: every field of the session row exactly once. A field
  // this page does not know yet lands in "Other fields", so nothing is ever hidden.
  function groupSessionFields(session) {
    const s = asObject(session);
    const used = new Set();
    const groups = [];
    SESSION_GROUPS.forEach(function (group) {
      const rows = [];
      group.fields.forEach(function (pair) {
        if (own(s, pair[0])) {
          rows.push({key: pair[0], label: pair[1], value: s[pair[0]]});
          used.add(pair[0]);
        }
      });
      if (rows.length > 0) groups.push({title: group.title, rows: rows});
    });
    const others = Object.keys(s).filter(function (key) { return !used.has(key); }).sort();
    if (others.length > 0) {
      groups.push({title: 'Other fields', rows: others.map(function (key) {
        return {key: key, label: key.replace(/_/g, ' '), value: s[key]};
      })});
    }
    return groups;
  }

  const MONO_FIELDS = Object.freeze([
    'path', 'storage_path', 'data_path', 'globus_url', 'web_url', 'subject', 'collection_id',
    'session_id', 'upload_id', 'sums_sha256', 'doc_sha256', 'folder', 'session_name'
  ]);

  // How to show one field: 'empty' (nothing recorded), 'none' (an empty list), 'bool', 'time',
  // 'bytes', 'list', 'json', 'mono' or 'text'.
  function fieldKind(key, value) {
    if (value === null || value === undefined || value === '') return 'empty';
    if (typeof value === 'boolean') return 'bool';
    if (typeof value === 'string' && /_utc$/.test(key) && parseUtc(value)) return 'time';
    if (key === 'bytes' && typeof value === 'number') return 'bytes';
    if (Array.isArray(value)) {
      if (value.length === 0) return 'none';
      const simple = value.every(function (item) {
        return typeof item === 'string' || typeof item === 'number';
      });
      return simple ? 'list' : 'json';
    }
    if (typeof value === 'object') return 'json';
    return MONO_FIELDS.indexOf(key) !== -1 ? 'mono' : 'text';
  }

  // ---- when a request fails -------------------------------------------------------------------

  function cleanMessage(value) {
    if (typeof value !== 'string') return '';
    const text = value.trim();
    return text.length > 4000 ? text.slice(0, 4000) + '\u2026' : text;
  }

  // One plain sentence (or two) for a failed request.
  // failure: {kind: 'network' | 'timeout' | 'http' | 'cancelled' | 'badreply' | 'signedout',
  //           status, message, details}
  // context: {role, action}: the signed-in role and what was tried ("run SQL"), for a 403.
  function explainFailure(failure, context) {
    const f = asObject(failure);
    const c = asObject(context);
    const said = cleanMessage(f.message);
    const status = typeof f.status === 'number' ? f.status : 0;
    if (f.kind === 'cancelled') return 'Cancelled.';
    if (f.kind === 'network') {
      return 'The backend is not reachable. Check that "pcldb serve" is still running, then try again.';
    }
    if (f.kind === 'timeout') {
      return 'The backend did not answer in time. Check that "pcldb serve" is still running, then try again.';
    }
    if (f.kind === 'badreply') {
      return 'The backend sent a reply this page could not read. Reload the page and try again.';
    }
    if (f.kind === 'signedout' || status === 401) return 'You are signed out. Sign in again.';
    if (status === 403) {
      const who = textOf(c.role) ? 'Your role (' + textOf(c.role) + ')' : 'Your role';
      return who + ' may not ' + (textOf(c.action) || 'do this') + '.' + (said ? ' The backend said: ' + said : '');
    }
    if (status === 413) {
      return 'That is too large for the backend to accept. Make it shorter and try again.' +
        (said ? ' The backend said: ' + said : '');
    }
    if (status === 429) {
      const wait = asObject(f.details).retry_after_s;
      const howLong = typeof wait === 'number' && wait >= 1 && wait <= 3600
        ? 'about ' + Math.ceil(wait) + ' ' + plural(Math.ceil(wait), 'second', 'seconds') : 'a minute';
      return 'Too many requests in a short time. Wait ' + howLong + ', then try again.' +
        (said ? ' The backend said: ' + said : '');
    }
    if (status >= 500) {
      if (said) return 'The backend could not do this. It said: ' + said;
      return 'The backend is not reachable or stopped answering. Check that "pcldb serve" is still running, then try again.';
    }
    if (said) return said;
    return 'The backend refused this request (status ' + status + ').';
  }

  const pure = {
    API: API, API_PATHS: API_PATHS, TABS: TABS, USER_PLACEHOLDER: USER_PLACEHOLDER,
    asArray: asArray, asObject: asObject, lookup: lookup, jsonText: jsonText, prettyJson: prettyJson,
    textOf: textOf, listText: listText, lastSegment: lastSegment, plural: plural, oneLine: oneLine,
    firstLine: firstLine, clipText: clipText, cut: cut, showHidden: showHidden,
    formatNumber: formatNumber, formatBytes: formatBytes,
    parseUtc: parseUtc, formatDateTime: formatDateTime, csvFileName: csvFileName,
    parseHash: parseHash, viewFor: viewFor, looksLikeToken: looksLikeToken, isApiPath: isApiPath,
    sessionAddress: sessionAddress, safeGlobusUrl: safeGlobusUrl, shellSafe: shellSafe,
    userNameProblem: userNameProblem, sshCommands: sshCommands, csvCell: csvCell,
    buildCsv: buildCsv, plannerLabel: plannerLabel, backendLabel: backendLabel,
    countLine: countLine, initialOf: initialOf, lanesLine: lanesLine, searchState: searchState,
    labelsText: labelsText, needsReviewCount: needsReviewCount,
    completionKind: completionKind, fieldWords: fieldWords, bucketWords: bucketWords,
    timingText: timingText, reviewSentence: reviewSentence, engineName: engineName,
    engineLabel: engineLabel, sqlIdentifier: sqlIdentifier, sessionPathOfCell: sessionPathOfCell,
    cellDisplay: cellDisplay, rowsLine: rowsLine,
    truncatedLine: truncatedLine, sqlFailureTitle: sqlFailureTitle, historyOutcome: historyOutcome,
    shortHash: shortHash, fileCountLine: fileCountLine, actionWords: actionWords,
    sourceWords: sourceWords, assistantWords: assistantWords,
    entryStateRows: entryStateRows, groupSessionFields: groupSessionFields, uploadFacts: uploadFacts,
    fieldKind: fieldKind, explainFailure: explainFailure
  };

  // =============================================================================================
  // Part 2: the page.
  // =============================================================================================

  function startPage() {
    const TOKEN_KEY = 'pcldb.token';
    const ARC_USER_KEY = 'pcldb.arc_user';
    const TAB_TITLES = {normal: 'Normal', expert: 'Expert', status: 'Status'};
    const GRID_MAX_ROWS = 2000;    // more rows than this stay out of the page (the CSV has them)
    const CELL_CHARS = 80;
    const REGISTRATIONS_SHOWN = 6;  // the Status view lists this many; "Show ... more" has the rest         // longer cells are cut; the title attribute keeps the full text
    // A soft pill with a coloured dot: green, amber and red only ever mark a state.
    const PILL_CLASSES = {ok: 'pill pill-ok', warn: 'pill pill-warn', bad: 'pill pill-bad', off: 'pill', plain: 'pill'};
    const CELL_CLASSES = {null: 'cell-null', number: 'cell-num'};
    // The icons drawn in index.html (the hidden block #icons) that the script may copy.
    const ICON_NAMES = ['info', 'alert', 'copy', 'external', 'download', 'database', 'search', 'spark', 'empty'];
    // How long the page waits for the backend before it gives up, in milliseconds. A question
    // can wait on the assistant for minutes; everything else is quick.
    const WAIT_MS = {ask: 300000, sql: 120000, other: 30000};

    // The only elements and attributes el() makes. Nothing in these lists can load, run or
    // link to anything: no script, frame, image or form, no address, style or event attribute.
    const EL_TAGS = ['button', 'caption', 'code', 'dd', 'details', 'div', 'dl', 'dt', 'h3', 'h4', 'li',
      'option', 'p', 'pre', 'section', 'span', 'summary', 'table', 'tbody', 'td', 'th', 'thead', 'time',
      'tr', 'ul', 'wbr'];
    const EL_ATTRIBUTES = ['class', 'type', 'title', 'scope', 'colspan', 'datetime', 'tabindex', 'role',
      'hidden', 'value', 'id', 'aria-label', 'aria-live', 'aria-expanded', 'aria-controls', 'open'];

    const state = {
      token: null,          // the access token: kept here and by storeToken(), nowhere else
      me: null,             // the reply of /me while someone is signed in
      tab: 'normal',
      arcUser: '',          // the typed ARC user name: kept here and by keepArcUser(), never in a request
      signedIn: null,       // AbortController for the small background loads of this sign-in
      askRun: null,         // AbortController of the question that is running
      askTimer: null,
      sqlRun: null,         // AbortController of the query that is running
      sqlResult: null,      // {columns, rows} of the grid on the page, for the CSV
      sqlCleanText: '',     // the query text last loaded or run, to notice typing that would be lost
      sqlExamples: [],
      sqlLoaded: {schema: false, examples: false, history: false},
      cellPaths: new WeakMap(),   // a path button in the result grid -> the session path it opens
      detailRun: null,
      detailOpener: null,
      statusRun: null
    };

    // ---- building the page: elements and text nodes only -------------------------------------

    function byId(id) {
      const node = document.getElementById(id);
      if (!node) throw new Error('index.html has no element with the id "' + id + '".');
      return node;
    }

    // The one way text is written into an element that is already on the page.
    function setText(node, text) {
      node.textContent = showHidden(text);
    }

    function append(node, children) {
      if (children === null || children === undefined || children === false) return;
      if (Array.isArray(children)) {
        children.forEach(function (child) { append(node, child); });
      } else if (typeof children === 'string' || typeof children === 'number') {
        node.appendChild(document.createTextNode(showHidden(children)));
      } else {
        node.appendChild(children);
      }
    }

    // el('p', {class: 'muted'}, ['text', otherNode]). Strings always become text nodes. It makes
    // only the elements of EL_TAGS with the attributes of EL_ATTRIBUTES, so it can never set a
    // link target, a source, a style or an event attribute: the two links this page makes are
    // built in outsideLink() and downloadCsv().
    function el(tag, attributes, children) {
      if (EL_TAGS.indexOf(tag) === -1) throw new Error('el() does not make the element "' + tag + '".');
      const node = document.createElement(tag);
      if (attributes) {
        Object.keys(attributes).forEach(function (name) {
          const value = attributes[name];
          if (value === null || value === undefined || value === false) return;
          if (EL_ATTRIBUTES.indexOf(name) === -1) throw new Error('el() does not set the attribute "' + name + '".');
          if (name === 'class') node.className = String(value);
          else if (name === 'title' || name === 'aria-label') node.setAttribute(name, showHidden(value));
          else node.setAttribute(name, value === true ? '' : String(value));
        });
      }
      append(node, children);
      return node;
    }

    function clear(node) {
      node.textContent = '';
    }

    // The nodes with a space between each two, so words that stand next to each other on the
    // page are also apart for a screen reader and in a copy of the text.
    function spaced(nodes) {
      const out = [];
      nodes.forEach(function (node) {
        if (node === null || node === undefined || node === false) return;
        if (out.length > 0) out.push(' ');
        out.push(node);
      });
      return out;
    }

    function show(node, visible) {
      node.hidden = !visible;
    }

    // A status line. kind: 'bad' marks a problem; 'quiet' keeps the text for screen readers
    // only, for when the same words are already on the page next to the result.
    function setLine(id, text, kind) {
      const node = byId(id);
      setText(node, text);
      node.classList.toggle('is-bad', kind === 'bad');
      node.classList.toggle('visually-hidden', kind === 'quiet');
    }

    function none(words) {
      return el('span', {class: 'none'}, words || '\u{2013}');
    }

    // A copy of one of the icons drawn in index.html. Only the fixed names of ICON_NAMES: no
    // drawing is ever made from text.
    function icon(name) {
      if (ICON_NAMES.indexOf(name) === -1) throw new Error('index.html has no icon named "' + name + '".');
      const copy = byId('icon-' + name).cloneNode(true);
      copy.removeAttribute('id');
      return copy;
    }

    // A small soft pill with a coloured dot. kind: 'ok', 'warn', 'bad', 'off' or 'plain'.
    function pill(kind, words, title) {
      return el('span', {class: lookup(PILL_CLASSES, kind, 'pill'), title: title || null},
        [el('span', {class: 'dot'}), words]);
    }

    // A toggle that keeps the longer things out of sight until they are wanted.
    function fold(summary, children, open, extraClass) {
      return el('details', {class: 'fold' + (extraClass ? ' ' + extraClass : ''), open: Boolean(open)},
        [el('summary', null, summary), children]);
    }

    // A long name or path as text that may wrap after each "/" and "_" (and nowhere odd).
    function breakable(text) {
      const value = textOf(text);
      const nodes = [];
      let start = 0;
      for (let i = 0; i < value.length - 1; i += 1) {
        const character = value.charAt(i);
        if (character === '/' || character === '_') {
          nodes.push(value.slice(start, i + 1), el('wbr'));
          start = i + 1;
        }
      }
      nodes.push(value.slice(start));
      return nodes;
    }

    // An element that shows a value which may be very long: cut to max characters, with more
    // of it in the title. attributes: as for el().
    function cutNode(tag, attributes, value, max) {
      const whole = textOf(value);
      const shown = clipText(whole, max);
      const all = {title: shown.clipped ? cut(whole, 2000) : null};
      Object.keys(attributes || {}).forEach(function (name) { all[name] = attributes[name]; });
      return el(tag, all, shown.text);
    }

    // A time from the backend in the reader's own time zone; the UTC text is in the title.
    function timeNode(text, seconds) {
      if (text === null || text === undefined || text === '') return none();
      const local = formatDateTime(text, {seconds: Boolean(seconds)});
      if (!local) return el('span', null, cut(text, 80));
      return el('time', {datetime: text, title: text + ' (UTC)'}, local);
    }

    function table(className, headings, rows, caption) {
      return el('table', {class: className}, [
        caption ? el('caption', null, caption) : null,
        el('thead', null, el('tr', null, headings.map(function (heading) {
          return el('th', {scope: 'col'}, heading);
        }))),
        el('tbody', null, rows)
      ]);
    }

    // A table inside its own scrolling box, so a wide table never widens the page. The box can
    // take the focus, so the keyboard can scroll it.
    function scrollBox(label, content, extraClass) {
      return el('div', {
        class: 'scroll' + (extraClass ? ' ' + extraClass : ''), tabindex: '0', role: 'region', 'aria-label': label
      }, content);
    }

    // The same frame for a small two-column table that wraps instead of scrolling.
    function smallBox(content) {
      return el('div', {class: 'scroll facet-box'}, content);
    }

    function factList(pairs) {
      const list = el('dl', {class: 'facts'});
      pairs.forEach(function (pair) {
        append(list, [el('dt', null, pair[0]), el('dd', null, pair[1])]);
      });
      return list;
    }

    // Text that can be long (JSON, a database message) in a box that scrolls by itself.
    function codeBlock(text, max) {
      return el('pre', {class: 'code', tabindex: '0'}, cut(text, max || 20000));
    }

    function countText(value) {
      return typeof value === 'number' ? formatNumber(value) : cut(value, 30);
    }

    // ---- the token ---------------------------------------------------------------------------

    function readStoredToken() {
      try {
        const stored = window.sessionStorage.getItem(TOKEN_KEY);
        return looksLikeToken(stored) ? stored : null;
      } catch (err) {
        return null;    // storage is switched off: the person pastes the token again
      }
    }

    function storeToken(token) {
      state.token = token;
      try {
        window.sessionStorage.setItem(TOKEN_KEY, token);
      } catch (err) {
        // Storage is switched off: the token stays in memory until the page is closed.
      }
    }

    function forgetToken() {
      state.token = null;
      try {
        window.sessionStorage.removeItem(TOKEN_KEY);
      } catch (err) {
        // Nothing was stored.
      }
    }

    // ---- the ARC user name: kept in this browser only, never sent ------------------------------

    function readArcUser() {
      try {
        const kept = window.localStorage.getItem(ARC_USER_KEY);
        return typeof kept === 'string' && userNameProblem(kept) === '' ? kept : '';
      } catch (err) {
        return '';      // storage is switched off: the name is typed again next time
      }
    }

    // True when this browser kept (or removed) the name.
    function keepArcUser(name) {
      try {
        if (name === '') window.localStorage.removeItem(ARC_USER_KEY);
        else window.localStorage.setItem(ARC_USER_KEY, name);
        return true;
      } catch (err) {
        return false;
      }
    }

    function onArcUserChange() {
      const typed = byId('arc-user').value.trim();
      const problem = userNameProblem(typed);
      if (problem !== '') {
        state.arcUser = '';
        setLine('arc-user-status', problem + ' It was not kept.', 'bad');
        return;
      }
      state.arcUser = typed;
      const kept = keepArcUser(typed);
      if (typed === '') {
        setLine('arc-user-status', 'Removed.');
      } else {
        setLine('arc-user-status', kept ? 'Saved in this browser.'
          : 'Used until you close this page.');
      }
    }

    // ---- talking to the backend --------------------------------------------------------------

    function failed(kind, status, body) {
      const error = asObject(asObject(body).error);
      return {
        isFailure: true,
        kind: kind,
        status: status || 0,
        message: typeof error.message === 'string' ? error.message : '',
        code: typeof error.code === 'string' ? error.code : '',
        details: error.details === undefined ? null : error.details
      };
    }

    function isFailure(err) {
      return Boolean(err && err.isFailure === true);
    }

    // The one place that talks to the backend. path: one of the table API (plus a query for the
    // session). options: {signal, waitMs, anonymous: true for sign-in, token: a token to try
    // before it is stored}. Returns the reply object or throws a failure made by failed().
    async function api(method, path, body, options) {
      const o = options || {};
      if (!isApiPath(path)) throw new Error('This page does not call that address.');
      const token = o.anonymous ? null : (o.token || state.token);
      if (!o.anonymous && !token) throw failed('signedout', 0, null);
      const headers = {'Accept': 'application/json'};
      if (token) headers['Authorization'] = 'Bearer ' + token;
      // One controller for each request: it ends the request when the caller cancels, and when
      // the backend has not answered in time (so nothing on the page waits for ever).
      const control = new AbortController();
      let tooLate = false;
      const timer = window.setTimeout(function () {
        tooLate = true;
        control.abort();
      }, o.waitMs || WAIT_MS.other);
      function onCancel() { control.abort(); }
      if (o.signal) {
        if (o.signal.aborted) control.abort();
        else o.signal.addEventListener('abort', onCancel);
      }
      const request = {
        method: method,
        headers: headers,
        signal: control.signal,
        mode: 'same-origin',
        credentials: 'omit',
        cache: 'no-store',
        redirect: 'error',
        referrerPolicy: 'no-referrer'
      };
      if (body !== undefined) {
        headers['Content-Type'] = 'application/json';
        request.body = JSON.stringify(body);
      }
      let response = null;
      let data = null;
      try {
        response = await fetch(path, request);
        const type = (response.headers.get('Content-Type') || '').toLowerCase();
        if (type.indexOf('application/json') === 0) data = await response.json();
      } catch (err) {
        if (err && err.name === 'AbortError') throw failed(tooLate ? 'timeout' : 'cancelled', 0, null);
        if (response === null) throw failed('network', 0, null);
        data = null;    // the reply came but its body was not readable JSON
      } finally {
        window.clearTimeout(timer);
        if (o.signal) o.signal.removeEventListener('abort', onCancel);
      }
      if (response.status === 401 && !o.anonymous && !o.token) {
        if (token === state.token) signOut('Your sign-in has ended. Sign in again.', true);
        throw failed('signedout', 401, data);
      }
      if (!response.ok) throw failed('http', response.status, data);
      if (data === null || typeof data !== 'object') throw failed('badreply', response.status, null);
      return data;
    }

    function roleName() {
      return cut(asObject(asObject(state.me).user).role, 30);
    }

    function can(capability) {
      return asArray(asObject(state.me).capabilities).indexOf(capability) !== -1;
    }

    // The sentence to show for anything a request threw.
    function sentenceFor(err, action) {
      if (isFailure(err)) return explainFailure(err, {role: roleName(), action: action});
      return 'Something went wrong in this page. Reload it and try again.';
    }

    function backgroundSignal() {
      return state.signedIn ? state.signedIn.signal : undefined;
    }

    // ---- the three screens: starting, sign in, the app ----------------------------------------

    function showView(name) {
      show(byId('view-loading'), name === 'loading');
      show(byId('view-signin'), name === 'signin');
      show(byId('view-app'), name === 'app');
      show(byId('tabs'), name === 'app');
      show(byId('who'), name === 'app');
      if (name !== 'app') show(byId('demo-chip'), false);
    }

    function setBoot(text, canRetry) {
      setText(byId('boot-status'), text);
      show(byId('boot-retry'), Boolean(canRetry));
    }

    // The sign-in form, with a line above it: a calm one, or a marked one when bad is true.
    function showSignin(note, bad) {
      const noteNode = byId('signin-note');
      setText(noteNode, note || '');
      noteNode.classList.toggle('note', !bad);
      noteNode.classList.toggle('banner', Boolean(bad));
      noteNode.classList.toggle('banner-bad', Boolean(bad));
      document.title = 'Sign in - PCL data';
      showView('signin');
      byId('signin-token').focus();
    }

    // Take one person's data off the page, so the next person to sign in starts clean.
    function resetPage() {
      [state.signedIn, state.askRun, state.sqlRun, state.detailRun, state.statusRun].forEach(function (run) {
        if (run) run.abort();
      });
      state.signedIn = null;
      state.askRun = null;
      state.sqlRun = null;
      state.detailRun = null;
      state.statusRun = null;
      state.me = null;
      state.sqlResult = null;
      state.sqlCleanText = '';
      state.sqlExamples = [];
      state.sqlLoaded = {schema: false, examples: false, history: false};
      state.cellPaths = new WeakMap();
      closeDetail();
      setAskBusy(false);
      setSqlBusy(false);
      byId('ask-question').value = '';
      byId('panel-normal').classList.toggle('is-compact', false);
      clear(byId('ask-results'));
      show(byId('ask-hint'), true);
      setLine('ask-status', '');
      byId('sql-text').value = '';
      clear(byId('sql-result'));
      clear(byId('sql-error'));
      show(byId('sql-error'), false);
      setLine('sql-status', '');
      clear(byId('schema-list'));
      setLine('schema-status', '');
      show(byId('schema-retry'), false);
      clear(byId('history-list'));
      setLine('history-status', '');
      resetExamples();
      byId('arc-user').value = '';
      setLine('arc-user-status', '');
      clear(byId('status-cards'));
      clear(byId('status-registrations'));
      setLine('status-line', '');
      ['user-name', 'user-role', 'user-initial', 'sql-dialect', 'app-version', 'lanes-you'].forEach(function (id) {
        clear(byId(id));
      });
    }

    function signOut(note, bad) {
      forgetToken();
      resetPage();
      showSignin(note || 'You are signed out.', Boolean(bad));
    }

    // byPerson: the person just signed in by hand, so the focus moves to the view's heading.
    function enterApp(me, byPerson) {
      state.me = asObject(me);
      state.signedIn = new AbortController();
      const user = asObject(state.me.user);
      const engine = asObject(state.me.engine);
      const role = roleName();
      const shownName = cut(textOf(user.display_name) || textOf(user.name) || 'Signed in', 60);
      setText(byId('user-name'), shownName);
      setText(byId('user-role'), role || 'no role');
      setText(byId('user-initial'), initialOf(shownName));
      setText(byId('sql-dialect'), engineName(engine.name) ? cut(engineName(engine.name), 30) + ' SQL' : '');
      show(byId('sql-dialect'), engineName(engine.name) !== '');
      const maxRows = asObject(state.me.defaults).sql_max_rows;
      setText(byId('sql-limit'), typeof maxRows === 'number'
        ? 'Read-only. Results stop at ' + formatNumber(maxRows) + ' rows.'
        : 'Read-only.');
      show(byId('tab-expert'), can('sql'));
      setText(byId('lanes-you'), lanesLine(role, can('sql')));
      byId('ask-simulated').checked = asObject(state.me.defaults).include_simulated !== false;
      state.arcUser = readArcUser();
      byId('arc-user').value = state.arcUser;
      setText(byId('app-version'), textOf(state.me.version) ? 'pcldb ' + cut(state.me.version, 30) : '');
      showView('app');
      route(Boolean(byPerson));
      if (state.tab !== 'status') loadStatus();   // for the "Demo data" mark in the header
    }

    // Change the address without adding a history entry. False when the browser refuses.
    function replaceAddress(address) {
      try {
        window.history.replaceState(null, '', address);
        return true;
      } catch (err) {
        return false;
      }
    }

    // Read a one-time sign-in code from the address and take it out of the address bar and the
    // history entry at once, before any request is made. Returns undefined when the address
    // holds no sign-in link, null when the link is damaged, else the code.
    function takeSigninCode() {
      const parsed = parseHash(window.location.hash);
      if (parsed.kind !== 'signin') return undefined;
      // The second way also puts the new address in place of the old one in the history.
      if (!replaceAddress(window.location.pathname + window.location.search)) window.location.replace('#');
      return parsed.code;
    }

    async function start(code) {
      resetPage();
      showView('loading');
      setBoot('Starting...', false);
      let note = '';
      if (typeof code === 'string') {
        try {
          const reply = await api('POST', API.signin, {code: code}, {anonymous: true});
          if (!looksLikeToken(reply.token)) throw failed('badreply', 200, null);
          storeToken(reply.token);
        } catch (err) {
          if (!isFailure(err)) throw err;
          const refused = err.kind === 'http' && err.status >= 400 && err.status < 500 && err.status !== 429;
          note = refused ? 'That sign-in link was already used or is more than five minutes old.' : sentenceFor(err, 'sign in');
        }
      } else if (code === null) {
        note = 'That sign-in link is not complete.';
      }
      if (!state.token) {
        showSignin(note ? note + ' Paste your access token below, or start again with: pcldb serve --open' : '', Boolean(note));
        return;
      }
      await loadMe();
    }

    async function loadMe() {
      showView('loading');
      setBoot('Signing in...', false);
      try {
        enterApp(await api('GET', API.me), false);
      } catch (err) {
        if (isFailure(err) && err.kind === 'signedout') return;    // api() already showed the sign-in form
        showView('loading');
        setBoot(sentenceFor(err, 'sign in'), true);
        if (!isFailure(err)) throw err;
      }
    }

    async function onSigninSubmit(event) {
      event.preventDefault();
      const field = byId('signin-token');
      const button = byId('signin-submit');
      const candidate = field.value.trim();
      field.value = '';    // the token does not stay in the page
      if (!looksLikeToken(candidate)) {
        showSignin('That does not look like an access token. A token starts with pcl_ and has no spaces. Paste all of it.', true);
        return;
      }
      button.disabled = true;
      showSignin('Checking the token...', false);
      try {
        const me = await api('GET', API.me, undefined, {token: candidate});
        storeToken(candidate);
        clear(byId('signin-note'));
        enterApp(me, true);
      } catch (err) {
        if (isFailure(err) && err.kind === 'http' && err.status === 401) {
          showSignin('That token was not accepted. Check that you copied all of it and that it is still valid.', true);
        } else {
          showSignin(sentenceFor(err, 'sign in'), true);
        }
        if (!isFailure(err)) throw err;
      } finally {
        button.disabled = false;
      }
    }

    // ---- views: the current one is kept in the address (#normal, #expert, #status) -------------

    function route(byPerson) {
      const parsed = parseHash(window.location.hash);
      const tab = viewFor(parsed.kind === 'tab' ? parsed.tab : state.tab, asObject(state.me).capabilities);
      state.tab = tab;
      if (window.location.hash !== '#' + tab) replaceAddress('#' + tab);
      TABS.forEach(function (name) {
        show(byId('panel-' + name), name === tab);
        const link = byId('tab-' + name);
        if (name === tab) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
      });
      document.title = lookup(TAB_TITLES, tab, 'Normal') + ' - PCL data';
      if (tab === 'expert') loadSqlSide();
      if (tab === 'status') loadStatus();
      if (byPerson) byId(tab + '-title').focus();
    }

    function onHashChange() {
      const code = takeSigninCode();
      if (code !== undefined) {
        start(code);
      } else if (state.me) {
        route(true);
      }
    }

    // ---- the Normal view: a question in plain English ------------------------------------------

    function setAskBusy(busy) {
      byId('ask-run').disabled = busy;
      show(byId('ask-cancel'), busy);
      byId('ask-results').setAttribute('aria-busy', busy ? 'true' : 'false');
      byId('ask-line').classList.toggle('is-busy', busy);
      Array.prototype.forEach.call(byId('ask-examples').querySelectorAll('button'), function (button) {
        button.disabled = busy;
      });
      if (state.askTimer !== null) {
        window.clearInterval(state.askTimer);
        state.askTimer = null;
      }
      const elapsed = byId('ask-elapsed');
      clear(elapsed);
      if (busy) {
        const began = Date.now();
        state.askTimer = window.setInterval(function () {
          setText(elapsed, Math.round((Date.now() - began) / 1000) + ' s');
        }, 1000);
      }
    }

    async function runAsk() {
      const question = byId('ask-question').value.trim();
      if (question === '') {
        setLine('ask-status', 'Type a question first.', 'bad');
        byId('ask-question').focus();
        return;
      }
      if (state.askRun) state.askRun.abort();
      const run = new AbortController();
      state.askRun = run;
      const includeSimulated = byId('ask-simulated').checked;
      setAskBusy(true);
      setLine('ask-status', 'Working on it. This can take a minute.');
      try {
        const reply = await api('POST', API.ask, {question: question, include_simulated: includeSimulated},
          {signal: run.signal, waitMs: WAIT_MS.ask});
        if (state.askRun !== run) return;
        renderAsk(reply, includeSimulated);
      } catch (err) {
        if (state.askRun !== run) return;
        if (isFailure(err) && err.kind === 'cancelled') {
          setLine('ask-status', 'Cancelled.');
        } else {
          clear(byId('ask-results'));
          setLine('ask-status', sentenceFor(err, 'ask questions'), 'bad');
        }
        if (!isFailure(err)) throw err;
      } finally {
        if (state.askRun === run) {
          state.askRun = null;
          setAskBusy(false);
        }
      }
    }

    function cancelAsk() {
      if (state.askRun) state.askRun.abort();
    }

    function renderAsk(reply, includeSimulated) {
      const r = asObject(reply);
      const results = asArray(r.results).map(asObject);
      const total = typeof r.total === 'number' ? r.total : results.length;
      const line = countLine(results.length, total);
      const box = byId('ask-results');
      clear(box);
      // After the first question the search box moves up and the big heading steps aside.
      byId('panel-normal').classList.toggle('is-compact', true);

      // What the backend has to say about this answer: one line each, with a mark in front.
      const notices = asArray(r.notices).map(textOf).filter(function (text) { return text !== ''; }).slice(0, 10);
      if (notices.length > 0) {
        append(box, el('div', {class: 'banners'}, notices.map(function (text) {
          return el('p', {class: 'banner'}, [icon('info'), el('span', null, cut(text, 1000))]);
        })));
      }

      // Only the assistant's own text is shown as an answer. Without it the page says in its
      // own words what was found.
      let answer = null;
      const answerText = typeof r.answer === 'string' ? r.answer.trim() : '';
      if (answerText !== '' && r.answer_by === 'llm') {
        answer = el('section', {class: 'answer'}, [
          el('p', {class: 'answer-label'}, [icon('spark'), 'Written by the ARC assistant. Check it against the table.']),
          el('p', {class: 'answer-text'}, cut(answerText, 6000))
        ]);
      }

      const facets = asArray(r.facets).filter(function (facet) {
        return facet !== null && typeof facet === 'object' && !Array.isArray(facet);
      }).slice(0, 32).map(facetTable);
      const facetBox = facets.length > 0 ? el('div', {class: 'facets'}, facets) : null;
      // The same summary line and "Details" above the list, whether or not anything matched.
      const interpretation = oneLine(r.interpretation);
      const summary = el('div', {class: 'summary'}, [
        el('p', {class: 'summary-line'}, spaced([
          el('span', {class: 'count'}, line),
          interpretation !== '' ? cutNode('span', {class: 'summary-what'}, interpretation, 200) : null
        ])),
        howSearched(r, interpretation !== '' && interpretation === textOf(r.interpretation).trim() &&
          !clipText(interpretation, 200).clipped)
      ]);
      if (results.length === 0) {
        let advice = 'Details shows how the question was read.';
        if (total === 0) {
          advice = includeSimulated
            ? 'Try other words, or a wider time span.'
            : 'Simulated sessions were left out. Turn on "Include simulated" to search them.';
        }
        append(box, [
          answer,
          summary,
          el('div', {class: 'empty'}, [icon('empty'), el('p', {class: 'muted'}, advice)]),
          facetBox
        ]);
      } else {
        append(box, [answer, summary, facetBox, sessionTable(results)]);
      }
      // The hint about opening a session only stands under a list of sessions.
      show(byId('ask-hint'), results.length > 0);
      setLine('ask-status', line, 'quiet');
    }

    // "Details": how the question was read, where it was searched, and the exact request.
    // inSummary: the summary line above already shows the whole reading, so it is not repeated.
    function howSearched(r, inSummary) {
      const timing = timingText(r.timing_ms);
      return fold('Details', [
        el('h3', {class: 'fold-title'}, 'How this was searched'),
        inSummary ? null : el('p', {class: 'interpretation'},
          cut(textOf(r.interpretation).trim(), 1000) || 'The backend did not say how it read the question.'),
        factList([
          ['Question read by', cut(plannerLabel(r.planned_by), 60)],
          ['Searched in', cut(backendLabel(r.backend), 60)]
        ]),
        el('h4', {class: 'fold-title'}, 'Exact request'),
        codeBlock(prettyJson(r.request) || 'The backend did not send the request.'),
        timing ? el('p', {class: 'muted small'}, timing) : null
      ], false, 'fold-small');
    }

    function facetTable(facet) {
      const words = cut(fieldWords(textOf(facet.field_name) || textOf(facet.name)), 60);
      const rows = asArray(facet.buckets).map(asObject).slice(0, 100).map(function (bucket) {
        return el('tr', null, [
          cutNode('th', {scope: 'row'}, bucketWords(bucket.value), 120),
          el('td', {class: 'num'}, countText(bucket.count))
        ]);
      });
      if (rows.length === 0) rows.push(el('tr', null, el('td', {colspan: '2'}, 'No values.')));
      return smallBox(table('facet', [words, 'Sessions'], rows, 'Sessions by ' + words));
    }

    function sessionTable(results) {
      const headings = ['Session', 'Instrument', 'Started', 'Labels', 'State'];
      return scrollBox('Sessions that matched', table('sessions', headings, results.map(sessionRow)), 'card-box');
    }

    function sessionRow(result) {
      const path = typeof result.path === 'string' ? result.path : '';
      const folder = textOf(result.folder) || textOf(result.title) || lastSegment(path) || '(no name)';
      const shownName = clipText(folder, 120);
      let name;
      if (path !== '') {
        name = el('button', {
          type: 'button', class: 'rowlink',
          title: shownName.clipped ? cut(folder, 2000) : 'Show everything about this session'
        }, breakable(shownName.text));
        name.addEventListener('click', function () { openDetail(path, folder, name, 'normal'); });
      } else {
        name = el('span', null, breakable(shownName.text));
      }
      const marks = [];
      const completion = textOf(result.completion);
      if (completion !== '') marks.push(pill(completionKind(completion), cut(completion, 30)));
      const review = needsReviewCount(result);
      if (review > 0) {
        marks.push(pill('warn', 'needs review (' + review + ')', reviewSentence(result.review_state, review)));
      }
      const row = el('tr', {class: path !== '' ? 'clickrow' : null}, [
        el('th', {scope: 'row', class: 'c-name'}, el('div', {class: 'marks'}, spaced([
          name, result.simulated === true ? el('span', {class: 'tag'}, 'simulated') : null
        ]))),
        el('td', {class: 'c-inst'}, cut(result.instrument, 80) || none()),
        el('td', {class: 'c-time', title: 'In your time zone'}, timeNode(result.started)),
        el('td', {class: 'c-labels'}, cut(labelsText(result), 300) || none()),
        el('td', {class: 'c-state'}, marks.length > 0 ? el('div', {class: 'marks'}, spaced(marks)) : none())
      ]);
      if (path !== '') {
        // A click anywhere in the row opens the session, unless the person is selecting text.
        row.addEventListener('click', function (event) {
          if (event.target instanceof Element && event.target.closest('button, a')) return;
          const selection = window.getSelection();
          if (selection && String(selection) !== '') return;
          openDetail(path, folder, name, 'normal');
        });
      }
      return row;
    }

    // ---- the session detail panel --------------------------------------------------------------

    // lane: the view the panel was opened from ('normal', 'expert' or 'status'). It decides
    // which file actions the panel offers: Globus for Normal, SSH for Expert.
    async function openDetail(path, title, opener, lane) {
      const dialog = byId('detail');
      const body = byId('detail-body');
      if (state.detailRun) state.detailRun.abort();
      const run = new AbortController();
      state.detailRun = run;
      state.detailOpener = opener || null;
      clear(byId('detail-title'));
      append(byId('detail-title'), breakable(cut(title || 'Session', 160)));
      clear(body);
      body.setAttribute('aria-busy', 'true');
      append(body, el('p', {class: 'muted loading'}, 'Loading this session...'));
      if (!dialog.open) {
        if (typeof dialog.showModal === 'function') dialog.showModal();
        else dialog.setAttribute('open', '');
      }
      try {
        const reply = await api('GET', sessionAddress(path), undefined, {signal: run.signal});
        if (state.detailRun !== run) return;
        clear(body);
        append(body, detailContent(asObject(reply), lane));
      } catch (err) {
        if (state.detailRun !== run) return;
        if (isFailure(err) && (err.kind === 'cancelled' || err.kind === 'signedout')) return;
        clear(body);
        append(body, el('p', {class: 'banner banner-bad'}, [icon('alert'), el('span', null, sentenceFor(err, 'see this session'))]));
        if (!isFailure(err)) throw err;
      } finally {
        if (state.detailRun === run) {
          state.detailRun = null;
          body.setAttribute('aria-busy', 'false');
        }
      }
    }

    function closeDetail() {
      const dialog = byId('detail');
      if (!dialog.open) return;
      if (typeof dialog.close === 'function') {
        dialog.close();    // the 'close' event then runs afterDetailClosed()
      } else {
        dialog.removeAttribute('open');
        afterDetailClosed();
      }
    }

    // The focus goes back to what opened the panel, or to the view's heading when that has
    // left the page meanwhile.
    function afterDetailClosed() {
      if (state.detailRun) {
        state.detailRun.abort();
        state.detailRun = null;
      }
      clear(byId('detail-body'));
      const opener = state.detailOpener;
      state.detailOpener = null;
      if (opener && document.contains(opener)) opener.focus();
      else if (state.me) byId(state.tab + '-title').focus();
    }

    // The one outside link of the page. Only an address that safeGlobusUrl() accepts becomes a
    // link; it opens in a new tab that gets no handle on this page and no referrer.
    function outsideLink(address, words) {
      const safe = safeGlobusUrl(address);
      if (safe === null) return null;
      const link = document.createElement('a');
      link.className = 'btn btn-primary';
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      append(link, [icon('external'), words, el('span', {class: 'visually-hidden'}, ' (opens the Globus web app in a new tab)')]);
      link.href = safe;
      return link;
    }

    // Copy text. When the browser refuses, the same text on the page is selected instead, so
    // one key press copies it. what: 'path', 'address' or 'command', for the words.
    async function copyText(text, shownNode, statusNode, what) {
      let copied = false;
      try {
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
          await navigator.clipboard.writeText(text);
          copied = true;
        }
      } catch (err) {
        copied = false;
      }
      if (copied) {
        setText(statusNode, 'Copied the ' + what + '.');
        return;
      }
      const selection = window.getSelection();
      if (selection) {
        const range = document.createRange();
        range.selectNodeContents(shownNode);
        selection.removeAllRanges();
        selection.addRange(range);
      }
      setText(statusNode, selection
        ? 'The browser did not allow copying. The ' + what + ' is selected: press Ctrl+C or Cmd+C to copy it.'
        : 'The browser did not allow copying. Select the ' + what + ' and copy it by hand.');
    }

    // words: what the button says; more: extra words for screen readers only.
    function copyButton(words, more, text, shownNode, statusNode, what) {
      const button = el('button', {type: 'button', class: 'btn btn-small'},
        [icon('copy'), words, more ? el('span', {class: 'visually-hidden'}, more) : null]);
      button.addEventListener('click', function () { copyText(text, shownNode, statusNode, what); });
      return button;
    }

    // A long path on one line, with a few muted words in front of it.
    function pathLine(words, shown) {
      return el('p', {class: 'path-line'}, [el('span', {class: 'muted'}, words), shown]);
    }

    // Normal mode: the files are reached through Globus.
    function globusActions(session, statusNode) {
      const webAddress = textOf(session.web_url);
      const globusAddress = textOf(session.globus_url);
      const buttons = [];
      const notes = [];
      const link = outsideLink(session.web_url, 'Open in Globus');
      if (link) {
        buttons.push(link);
      } else if (webAddress !== '') {
        notes.push(el('p', {class: 'muted small'}, [
          'The Globus web app address is not on app.globus.org, so it is shown as text: ',
          el('code', {class: 'path'}, cut(webAddress, 2000))
        ]));
      } else {
        notes.push(el('p', {class: 'muted small'}, 'No Globus web app address is recorded.'));
      }
      if (globusAddress !== '') {
        const shown = el('code', {class: 'path'}, breakable(cut(globusAddress, 2000)));
        buttons.push(copyButton('Copy Globus address', null, globusAddress, shown, statusNode, 'address'));
        notes.push(pathLine('Globus address: ', shown));
      }
      return el('div', {class: 'lane-actions'}, [
        el('h4', {class: 'visually-hidden'}, 'Through Globus'),
        buttons.length > 0 ? el('div', {class: 'action-row'}, buttons) : null,
        notes
      ]);
    }

    // Expert mode: the files are reached over SSH. The commands are only written out when the
    // path, the host and the user name are plain enough to paste into a terminal as they are.
    function sshActions(session, statusNode, lane) {
      const arcPath = textOf(session.storage_path);
      if (arcPath === '') {
        return el('div', {class: 'lane-actions'}, [
          el('h4', {class: 'visually-hidden'}, 'Over SSH'),
          el('p', {class: 'muted small'}, 'No path on ARC is recorded for this session.')
        ]);
      }
      const shown = el('code', {class: 'path'}, breakable(cut(arcPath, 4000)));
      const parts = [
        el('h4', {class: 'visually-hidden'}, 'Over SSH'),
        el('div', {class: 'action-row'}, copyButton('Copy ARC path', null, arcPath, shown, statusNode, 'path')),
        pathLine('ARC path: ', shown)
      ];
      const host = textOf(asObject(asObject(state.me).storage).ssh_host);
      const made = sshCommands(host, state.arcUser, arcPath);
      if (made === null) {
        parts.push(el('p', {class: 'banner'}, [icon('info'), el('span', null, sshCommands('host', '', arcPath) === null
          ? 'This path has unusual characters. Copy the path and quote it yourself.'
          : 'No usable SSH host was named, so there are no commands. Copy the path instead.')]));
        return el('div', {class: 'lane-actions'}, parts);
      }
      // Opened from Expert, a button leads straight to the name field under the results.
      let setName = null;
      if (made.placeholder && lane === 'expert') {
        setName = el('button', {type: 'button', class: 'btn btn-small'}, 'Set user name');
        setName.addEventListener('click', editArcUser);
      }
      parts.push(fold('SSH commands', [
        el('p', {class: 'muted small'}, made.placeholder
          ? 'Put your ARC user name in place of ' + USER_PLACEHOLDER + ', or save it in Expert, SSH settings.'
          : 'The commands use the ARC user name ' + made.user + '.'),
        setName,
        el('ul', {class: 'commands'}, made.commands.map(function (item) {
          const line = el('code', {class: 'path'}, item.command);
          return el('li', {class: 'command'}, [
            el('span', {class: 'command-label'}, item.label),
            el('div', {class: 'command-line'}, [
              line, copyButton('Copy', ' the ' + item.id + ' command', item.command, line, statusNode, 'command')
            ])
          ]);
        }))
      ], lane === 'expert'));
      return el('div', {class: 'lane-actions'}, parts);
    }

    // Close the panel and put the focus in the ARC user name field of the Expert view.
    function editArcUser() {
      byId('ssh-settings').setAttribute('open', '');
      state.detailOpener = byId('arc-user');   // afterDetailClosed() focuses it
      closeDetail();
    }

    // The file actions of the mode the panel was opened from. From the Status view, which is
    // neither mode, it shows what the person may use: Globus, and SSH with the Expert role.
    function fileActions(session, lane) {
      const statusNode = el('p', {class: 'copy-status', role: 'status', 'aria-live': 'polite'});
      const blocks = [];
      if (lane !== 'expert') blocks.push(globusActions(session, statusNode));
      if (lane === 'expert' || (lane === 'status' && can('sql'))) blocks.push(sshActions(session, statusNode, lane));
      return el('section', {class: 'actions'}, [el('h3', {class: 'visually-hidden'}, 'Get to the files'), blocks, statusNode]);
    }

    // How the session got to ARC, in words, with the stored record behind a "Show".
    function uploadValue(value) {
      if (value === null || value === undefined || value === '') return none('not recorded');
      const record = el('details', {class: 'raw'}, [
        el('summary', null, 'Stored record'), codeBlock(prettyJson(value))
      ]);
      const facts = uploadFacts(value);
      if (facts.length === 0) return record;
      return [factList(facts.map(function (row) {
        if (row[2] === 'time') return [row[0], timeNode(row[1], true)];
        if (row[2] === 'mono') return [row[0], el('span', {class: 'mono'}, breakable(cut(row[1], 200)))];
        return [row[0], cut(row[1], 300)];
      })), record];
    }

    // Text of a record that can be very long (notes): past a few lines it gets its own box.
    function longText(value) {
      const text = cut(value, 200000);
      if (text.length <= 1500) return text;
      return el('div', {class: 'longtext', tabindex: '0', role: 'region', 'aria-label': 'Long text'}, text);
    }

    function fieldValue(key, value) {
      if (key === 'upload_json') return uploadValue(value);
      const kind = fieldKind(key, value);
      if (kind === 'empty') return none('not recorded');
      if (kind === 'none') return none('none');
      if (kind === 'bool') return value ? 'yes' : 'no';
      if (kind === 'time') {
        return [timeNode(value, true), ' ', el('span', {class: 'utc'}, value)];
      }
      if (kind === 'bytes') {
        return el('span', {title: formatNumber(value) + ' bytes'}, formatBytes(value) || textOf(value));
      }
      if (kind === 'list') return longText(listText(value));
      if (kind === 'json') {
        return el('details', {class: 'raw'}, [el('summary', null, 'Show'), codeBlock(prettyJson(value), 100000)]);
      }
      if (kind === 'mono') return el('span', {class: 'mono'}, breakable(cut(value, 4000)));
      return longText(value);
    }

    function fieldGroup(group) {
      const list = el('dl', {class: 'fields'});
      group.rows.forEach(function (row) {
        append(list, [el('dt', null, cut(row.label, 80)), el('dd', null, fieldValue(row.key, row.value))]);
      });
      return el('section', {class: 'group'}, [el('h4', null, group.title), list]);
    }

    // The few facts a person looks at first, in a tidy grid. Every field is also in "All fields".
    function keyFacts(session) {
      const s = session;
      const pairs = [];
      if (textOf(s.instrument) !== '') pairs.push(['Instrument', cut(s.instrument, 80)]);
      if (textOf(s.started_utc) !== '') pairs.push(['Started', timeNode(s.started_utc)]);
      if (textOf(s.method) !== '') pairs.push(['Method', cut(s.method, 80)]);
      if (listText(s.sample_ids) !== '') pairs.push(['Samples', cut(listText(s.sample_ids), 200)]);
      if (typeof s.items === 'number') {
        pairs.push(['Items', formatNumber(s.items) + (typeof s.items_planned === 'number' ? ' of ' + formatNumber(s.items_planned) : '')]);
      }
      if (typeof s.bytes === 'number' && formatBytes(s.bytes) !== '') {
        pairs.push(['Size', formatBytes(s.bytes) + (typeof s.n_files === 'number' ? ', ' + fileCountLine(s.n_files, s.n_files) : '')]);
      }
      if (pairs.length === 0) return null;
      const list = el('dl', {class: 'keyfacts'});
      pairs.forEach(function (pair) {
        append(list, el('div', {class: 'keyfact'}, [el('dt', null, pair[0]), el('dd', null, pair[1])]));
      });
      return list;
    }

    function detailLabels(r) {
      const items = asArray(r.labels).map(asObject).slice(0, 500).map(function (item) {
        return el('li', {class: 'label-chip'}, [cutNode('span', null, item.label, 120), ' ', el('span', {class: 'num'}, countText(item.count))]);
      });
      const sentence = reviewSentence(r.review_state, r.n_needs_review);
      return el('section', {class: 'group'}, [
        el('h3', {class: 'group-title'}, 'Labels'),
        items.length > 0
          ? el('ul', {class: 'label-chips', 'aria-label': 'Labels and their points'}, items)
          : el('p', {class: 'muted small'}, 'This session has no labels.'),
        sentence ? el('p', {class: 'muted small'}, sentence) : null
      ]);
    }

    function detailRuns(runs) {
      const rows = asArray(runs).map(asObject).slice(0, 500).map(function (run) {
        return el('tr', null, [
          el('th', {scope: 'row'}, el('span', {class: 'mono', title: cut(run.path, 2000) || null},
            breakable(cut(textOf(run.folder) || lastSegment(run.path), 160)))),
          el('td', null, cut(run.kind, 40) || none()),
          el('td', null, timeNode(run.made_utc)),
          el('td', null, cut(run.model_name, 120) || none()),
          el('td', null, cut(run.run_by, 80) || none()),
          el('td', null, run.valid === true ? 'yes' : (run.valid === false ? 'no' : none()))
        ]);
      });
      const headings = ['Run', 'Kind', 'Made', 'Model', 'Run by', 'Valid'];
      return fold('Runs (' + rows.length + ')', rows.length > 0
        ? scrollBox('Runs of this session', table('plain wide', headings, rows))
        : el('p', {class: 'muted small'}, 'No run was made from this session.'));
    }

    function detailFiles(files) {
      const f = asObject(files);
      const items = asArray(f.items).map(asObject).slice(0, 2000);
      const rows = items.map(function (item) {
        const hash = textOf(item.sha256);
        const size = typeof item.bytes === 'number' && item.bytes >= 0 ? item.bytes : null;
        return el('tr', null, [
          el('th', {scope: 'row'}, el('span', {class: 'mono'}, breakable(cut(item.rel_path, 300)))),
          el('td', {title: size !== null ? formatNumber(size) + ' bytes' : null},
            formatBytes(size) || cut(item.bytes, 30) || none()),
          el('td', null, hash !== '' ? el('span', {class: 'mono', title: cut(hash, 200)}, shortHash(hash)) : none('not listed'))
        ]);
      });
      const total = typeof f.total === 'number' ? f.total : items.length;
      return fold('Files (' + formatNumber(total) + ')', [
        el('p', {class: 'muted small'}, fileCountLine(items.length, total)),
        rows.length > 0 ? scrollBox('Files of this session', table('plain', ['Name', 'Size', 'SHA-256'], rows)) : null
      ]);
    }

    function detailContent(r, lane) {
      const session = asObject(r.session);
      const marks = [];
      const completion = textOf(session.completion);
      if (completion !== '') marks.push(pill(completionKind(completion), cut(completion, 30)));
      if (session.simulated === true) marks.push(el('span', {class: 'tag'}, 'simulated'));
      if (session.withdrawn === true) marks.push(pill('bad', 'withdrawn'));
      if (session.corrected === true) marks.push(el('span', {class: 'tag'}, 'corrected'));
      if (session.is_copy === true) marks.push(el('span', {class: 'tag'}, 'copy'));
      return [
        marks.length > 0 ? el('p', {class: 'marks'}, spaced(marks)) : null,
        keyFacts(session),
        fileActions(session, lane),
        detailLabels(r),
        el('div', {class: 'folds'}, [
          fold('All fields', groupSessionFields(session).map(fieldGroup)),
          detailRuns(r.runs),
          detailFiles(r.files)
        ])
      ];
    }

    // ---- the Expert view: SQL ------------------------------------------------------------------

    function loadSqlSide() {
      if (!state.sqlLoaded.schema) loadSchema();
      if (!state.sqlLoaded.examples) loadExamples();
      if (!state.sqlLoaded.history) loadHistory();
    }

    // What the three small loads of the Expert view do when a request fails. Returns the
    // sentence to show, or '' when there is nothing to say (the person signed out or left).
    function sideFailure(err, name, action) {
      state.sqlLoaded[name] = false;
      if (isFailure(err) && (err.kind === 'cancelled' || err.kind === 'signedout')) return '';
      return sentenceFor(err, action);
    }

    async function loadSchema() {
      state.sqlLoaded.schema = true;
      show(byId('schema-retry'), false);
      setLine('schema-status', 'Loading the list of tables...');
      try {
        const reply = asObject(await api('GET', API.sqlSchema, undefined, {signal: backgroundSignal()}));
        const list = byId('schema-list');
        const objects = asArray(reply.objects).map(asObject).slice(0, 500);
        clear(list);
        objects.forEach(function (object, index) { append(list, schemaItem(object, index)); });
        setLine('schema-status', objects.length > 0 ? '' : 'The backend listed no tables.');
      } catch (err) {
        const sentence = sideFailure(err, 'schema', 'see the tables');
        setLine('schema-status', sentence, 'bad');
        show(byId('schema-retry'), sentence !== '');
        if (!isFailure(err)) throw err;
      }
    }

    // A name that goes into the query where the cursor is. about: its description, as a tooltip.
    function insertButton(name, about, extraClass) {
      const shown = clipText(name, 64);
      const words = cut(oneLine(about), 400);
      const button = el('button', {
        type: 'button', class: 'insert' + (extraClass ? ' ' + extraClass : ''),
        title: shown.clipped ? cut(name, 2000) : (words || 'Put this name into the query')
      }, shown.text);
      button.addEventListener('click', function () { insertIntoEditor(sqlIdentifier(name)); });
      return button;
    }

    // One table: its name (a click puts it into the query), and a toggle that shows its columns.
    function schemaItem(object, index) {
      const name = textOf(object.name);
      const columns = asArray(object.columns).map(asObject).slice(0, 500);
      const listId = 'schema-columns-' + index;
      const columnList = el('ul', {id: listId, class: 'columns', hidden: true}, columns.map(function (column) {
        return el('li', {class: 'column'}, spaced([
          insertButton(textOf(column.name), column.description),
          el('span', {class: 'coltype'}, cut(column.type, 40)),
          column.nullable === true ? el('span', {class: 'colnull', title: 'May be empty'}, 'null') : null
        ]));
      }));
      const toggle = el('button', {type: 'button', class: 'disclose', 'aria-expanded': 'false', 'aria-controls': listId,
        title: 'Show the columns'}, el('span', {class: 'visually-hidden'}, 'Show columns of ' + cut(name, 64)));
      toggle.addEventListener('click', function () {
        const open = toggle.getAttribute('aria-expanded') !== 'true';
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        columnList.hidden = !open;
      });
      return el('li', {class: 'schema-object'}, [
        el('div', {class: 'schema-head'}, spaced([
          toggle,
          insertButton(name, object.description, 'insert-table'),
          object.kind === 'view' ? el('span', {class: 'tag'}, 'view') : null,
          el('span', {class: 'disclose-count', title: columns.length + ' ' + plural(columns.length, 'column', 'columns')},
            String(columns.length))
        ])),
        columnList
      ]);
    }

    // Put text into the query box where the cursor is. The first way keeps the box's own undo
    // history (it types plain text, never markup); the second is the standard fallback.
    function insertIntoEditor(text) {
      const editor = byId('sql-text');
      editor.focus();
      let typed = false;
      try {
        typed = document.execCommand('insertText', false, text);
      } catch (err) {
        typed = false;
      }
      if (!typed) editor.setRangeText(text, editor.selectionStart, editor.selectionEnd, 'end');
    }

    // Load a query into the box. It is never run from here. Asks first when that would throw
    // away something the person typed. Returns true when the text was loaded.
    function loadIntoEditor(sql, said) {
      const editor = byId('sql-text');
      const typed = editor.value;
      if (typed.trim() !== '' && typed !== state.sqlCleanText && typed !== sql) {
        if (!window.confirm('Replace the query in the box? What you typed there will be lost.')) return false;
      }
      editor.value = sql;
      state.sqlCleanText = sql;
      editor.focus();
      setLine('sql-status', said + ' Press Run to run it.');
      return true;
    }

    // Empty the list of examples down to its first line, which says what the list is for.
    function resetExamples(words) {
      const select = byId('sql-examples');
      while (select.options.length > 1) select.remove(select.options.length - 1);
      setText(select.options[0], words || 'Examples');
      select.selectedIndex = 0;
    }

    async function loadExamples() {
      state.sqlLoaded.examples = true;
      try {
        const reply = asObject(await api('GET', API.sqlExamples, undefined, {signal: backgroundSignal()}));
        const select = byId('sql-examples');
        state.sqlExamples = asArray(reply.examples).map(asObject).filter(function (example) {
          return typeof example.sql === 'string' && example.sql !== '';
        }).slice(0, 200);
        resetExamples(state.sqlExamples.length > 0 ? '' : 'No examples');
        state.sqlExamples.forEach(function (example, index) {
          append(select, el('option', {value: String(index)}, cut(oneLine(example.title), 100) || 'Example ' + (index + 1)));
        });
      } catch (err) {
        // The list of tables next to it already says why the backend did not answer.
        if (sideFailure(err, 'examples', 'see the examples')) resetExamples('Examples unavailable');
        if (!isFailure(err)) throw err;
      }
    }

    function onExampleChosen() {
      const select = byId('sql-examples');
      const chosen = select.value;
      select.selectedIndex = 0;    // so the same example can be chosen again later
      if (chosen === '') return;
      const example = state.sqlExamples[Number(chosen)];
      if (!example) return;
      loadIntoEditor(example.sql, 'Loaded the example "' + (cut(oneLine(example.title), 100) || 'without a name') + '".');
    }

    async function loadHistory() {
      state.sqlLoaded.history = true;
      try {
        const reply = asObject(await api('GET', API.sqlHistory, undefined, {signal: backgroundSignal()}));
        const items = asArray(reply.items).map(asObject).slice(0, 50);
        const list = byId('history-list');
        clear(list);
        items.forEach(function (item) { append(list, historyItem(item)); });
        setLine('history-status', items.length > 0 ? '' : 'No queries yet.');
      } catch (err) {
        setLine('history-status', sideFailure(err, 'history', 'see earlier queries'), 'bad');
        if (!isFailure(err)) throw err;
      }
    }

    function historyItem(item) {
      const sql = textOf(item.query_text);
      const shown = clipText(oneLine(sql), 160);
      const button = el('button', {
        type: 'button', class: 'history-query', title: shown.clipped ? cut(sql, 4000) : 'Put this query into the box'
      }, shown.text);
      button.addEventListener('click', function () { loadIntoEditor(sql, 'Loaded an earlier query.'); });
      return el('li', {class: 'history-item'}, [
        button,
        el('p', {class: 'history-meta'}, [
          timeNode(item.at_utc), ' \u{b7} ',
          el('span', {class: item.ok === true ? null : 'is-bad'}, historyOutcome(item))
        ])
      ]);
    }

    function setSqlBusy(busy) {
      byId('sql-run').disabled = busy;
      byId('sql-cancel').disabled = !busy;
      show(byId('sql-cancel'), busy);
      byId('sql-result').setAttribute('aria-busy', busy ? 'true' : 'false');
      byId('sql-line').classList.toggle('is-busy', busy);
    }

    async function runSql() {
      if (state.sqlRun) return;    // one query at a time
      const sql = byId('sql-text').value;
      if (sql.trim() === '') {
        setLine('sql-status', 'Type a query first, or pick an example.', 'bad');
        return;
      }
      const run = new AbortController();
      state.sqlRun = run;
      setSqlBusy(true);
      setLine('sql-status', 'Running the query...');
      const request = {sql: sql};
      const maxRows = asObject(asObject(state.me).defaults).sql_max_rows;
      if (typeof maxRows === 'number' && maxRows >= 1) request.max_rows = Math.floor(maxRows);
      try {
        const reply = await api('POST', API.sql, request, {signal: run.signal, waitMs: WAIT_MS.sql});
        if (state.sqlRun !== run) return;
        state.sqlCleanText = sql;
        renderSqlResult(asObject(reply));
      } catch (err) {
        if (state.sqlRun !== run) return;
        if (isFailure(err) && err.kind === 'cancelled') {
          setLine('sql-status', 'Cancelled.');
        } else {
          // A query the database answered with an error is in "Your last queries" too.
          if (isFailure(err) && err.code.indexOf('sql_') === 0) state.sqlCleanText = sql;
          renderSqlFailure(err);
        }
        if (!isFailure(err)) throw err;
      } finally {
        if (state.sqlRun === run) {
          state.sqlRun = null;
          setSqlBusy(false);
          if (state.me) loadHistory();
        }
      }
    }

    function cancelSql() {
      if (state.sqlRun) state.sqlRun.abort();
    }

    // One cell of the result. A session path (section 12) becomes a button that opens the
    // session; paths remembers which path each button stands for.
    function gridCell(value, columnName, paths) {
      const shown = cellDisplay(value);
      const path = sessionPathOfCell(columnName, value);
      const clipped = clipText(shown.text, path === null ? CELL_CHARS : 160);
      const title = clipped.clipped || /[\r\n]/.test(shown.text) ? cut(shown.text, 2000) : null;
      if (path === null) return el('td', {class: lookup(CELL_CLASSES, shown.kind, null), title: title}, clipped.text);
      const button = el('button', {
        type: 'button', class: 'rowlink cellpath', title: title || 'Show everything about this session'
      }, clipped.text);
      paths.set(button, path);
      return el('td', null, button);
    }

    // The whole grid is built away from the page and put in with one step, so a result of a
    // thousand rows does not make the browser lay the page out again and again.
    function gridTable(columns, rows, paths) {
      const names = columns.map(function (column) { return typeof column.name === 'string' ? column.name : ''; });
      const head = el('tr', null, columns.map(function (column) {
        return el('th', {scope: 'col'}, spaced([
          cutNode('span', {class: 'colname'}, column.name, 60),
          el('span', {class: 'coltype'}, cut(column.type, 40) || '\u{a0}')
        ]));
      }));
      const lines = document.createDocumentFragment();
      rows.forEach(function (row) {
        const values = asArray(row);
        const line = el('tr');
        for (let i = 0; i < names.length; i += 1) line.appendChild(gridCell(values[i], names[i], paths));
        lines.appendChild(line);
      });
      const body = el('tbody');
      body.appendChild(lines);
      return el('table', {class: 'grid'}, [el('thead', null, head), body]);
    }

    function renderSqlResult(r) {
      const columns = asArray(r.columns).map(asObject).slice(0, 2000);
      const rows = asArray(r.rows);
      state.sqlResult = {columns: columns, rows: rows};
      state.cellPaths = new WeakMap();
      const onPage = rows.length > GRID_MAX_ROWS ? rows.slice(0, GRID_MAX_ROWS) : rows;
      const lines = [rowsLine(typeof r.row_count === 'number' ? r.row_count : rows.length, r.elapsed_ms)];
      if (r.truncated === true) lines.push(truncatedLine(rows.length));
      if (onPage.length < rows.length) {
        lines.push('This page shows the first ' + onPage.length + ' of them; Download CSV has all ' + rows.length);
      }
      const line = lines.join('. ') + '.';
      const box = byId('sql-result');
      clear(box);
      clear(byId('sql-error'));
      show(byId('sql-error'), false);
      const download = el('button', {type: 'button', class: 'btn btn-small'}, [icon('download'), 'Download CSV']);
      download.addEventListener('click', downloadCsv);
      append(box, el('div', {class: 'result-card'}, [
        el('div', {class: 'result-head'}, [el('p', {class: 'count'}, line), columns.length > 0 ? download : null]),
        columns.length > 0
          ? scrollBox('Result of the query', gridTable(columns, onPage, state.cellPaths), 'grid-box')
          : el('p', {class: 'muted result-none'}, 'The result has no columns.')
      ]));
      setLine('sql-status', line, 'quiet');
    }

    // A click on a session path in the result opens that session with the SSH file actions.
    function onResultClick(event) {
      const button = event.target instanceof Element ? event.target.closest('button') : null;
      const path = button ? state.cellPaths.get(button) : undefined;
      if (typeof path === 'string') openDetail(path, lastSegment(path), button, 'expert');
    }

    function renderSqlFailure(err) {
      state.sqlResult = null;
      state.cellPaths = new WeakMap();
      clear(byId('sql-result'));
      const box = byId('sql-error');
      clear(box);
      const fromDatabase = isFailure(err) && err.kind === 'http' && err.message !== '' &&
        err.status >= 400 && err.status < 500 && [401, 403, 413, 429].indexOf(err.status) === -1;
      if (fromDatabase) {
        // The database's own words, kept as they came (the line with the caret needs its spaces).
        append(box, [
          el('p', {class: 'error-title'}, [icon('alert'), sqlFailureTitle(err.code)]),
          el('pre', {class: 'error-text', tabindex: '0'}, cut(err.message, 4000))
        ]);
        const details = err.details;
        if (details !== null && typeof details === 'object' && Object.keys(details).length > 0) {
          append(box, el('details', {class: 'raw'}, [
            el('summary', null, 'More details'),
            codeBlock(prettyJson(details))
          ]));
        }
      } else {
        append(box, el('p', {class: 'error-title'}, [icon('alert'), sentenceFor(err, 'run SQL')]));
      }
      show(box, true);
      setLine('sql-status', '');
    }

    // "Download CSV": the file is built here, in the browser, from the rows that came back.
    function downloadCsv() {
      const result = state.sqlResult;
      if (!result) return;
      const names = result.columns.map(function (column) { return textOf(column.name); });
      const blob = new Blob([buildCsv(names, result.rows)], {type: 'text/csv;charset=utf-8'});
      const address = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = address;
      link.download = csvFileName(new Date());
      link.hidden = true;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.setTimeout(function () { URL.revokeObjectURL(address); }, 10000);
      setLine('sql-status', 'Download started: ' + link.download);
    }

    function onEditorKey(event) {
      if (event.isComposing) return;
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        runSql();
      } else if (event.key === 'Tab' && !event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey) {
        event.preventDefault();
        insertIntoEditor('  ');
      } else if (event.key === 'Escape') {
        // Leave the box, so the Tab key moves on again.
        event.preventDefault();
        const next = byId('sql-run').disabled ? byId('sql-cancel') : byId('sql-run');
        next.focus();
      }
    }

    // ---- the Status view -----------------------------------------------------------------------

    async function loadStatus() {
      if (state.statusRun) state.statusRun.abort();
      const run = new AbortController();
      state.statusRun = run;
      byId('status-cards').setAttribute('aria-busy', 'true');
      setLine('status-line', 'Loading...');
      try {
        const reply = await api('GET', API.status, undefined, {signal: run.signal});
        if (state.statusRun !== run) return;
        renderStatus(asObject(reply));
        setLine('status-line', 'Up to date.', 'quiet');
      } catch (err) {
        if (state.statusRun !== run) return;
        if (isFailure(err) && (err.kind === 'cancelled' || err.kind === 'signedout')) return;
        setLine('status-line', sentenceFor(err, 'see the status'), 'bad');
        if (!isFailure(err)) throw err;
      } finally {
        if (state.statusRun === run) {
          state.statusRun = null;
          byId('status-cards').setAttribute('aria-busy', 'false');
        }
      }
    }

    // One compact card: an icon, a title, a coloured dot with the state, one plain line, two or
    // three key facts, and "Details" for everything else.
    function statusCard(iconName, title, condition, line, facts, more) {
      return el('section', {class: 'card status-card'}, [
        el('div', {class: 'card-head'}, [
          el('span', {class: 'card-icon'}, icon(iconName)),
          el('h3', null, title),
          pill(condition.kind, condition.title)
        ]),
        el('p', {class: 'card-line'}, line),
        factList(facts),
        fold('Details', more, false, 'fold-small')
      ]);
    }

    function numberOrNone(value) {
      return typeof value === 'number' ? formatNumber(value) : none();
    }

    function databaseCard(d) {
      const engine = cut(engineLabel({name: d.engine, version: d.version}), 40);
      const where = cut(d.where, 200);
      const instruments = asArray(d.instruments).map(asObject).slice(0, 200).map(function (item) {
        return el('tr', null, [
          cutNode('th', {scope: 'row'}, item.instrument, 80), el('td', {class: 'num'}, numberOrNone(item.sessions))
        ]);
      });
      // The pill says how the database itself is; "Demo data" is in the header and the line.
      const line = (engine || 'The database answers.') + (d.demo === true ? ' \u{b7} demo data' : '');
      return statusCard('database', 'Database', {kind: 'ok', title: 'Running'}, line, [
        ['Sessions', numberOrNone(d.sessions)],
        ['Files', numberOrNone(d.files)],
        ['Newest session', d.newest_started_utc ? timeNode(d.newest_started_utc) : none('no start time')]
      ], [
        el('p', null, 'The session records are kept in ' + (engine || 'a database') + (where ? ' (' + where + ')' : '') + '.'),
        d.demo === true ? el('p', null, 'These are made-up demo sessions, not lab data.') : null,
        // Only an external PostgreSQL whose expert-mode limits are weaker sends this sentence.
        textOf(d.expert_note).trim() !== '' ? el('p', {class: 'banner'}, [icon('alert'), el('span', null, cut(textOf(d.expert_note).trim(), 1000))]) : null,
        factList([
          ['Analysis runs', numberOrNone(d.runs)],
          ['Classified points', numberOrNone(d.points)]
        ]),
        instruments.length > 0
          ? smallBox(table('facet', ['Instrument', 'Sessions'], instruments))
          : null
      ]);
    }

    function searchCard(s) {
      const rows = entryStateRows(s.entries).slice(0, 50).map(function (pair) {
        return el('tr', null, [
          cutNode('th', {scope: 'row'}, pair[0], 80), el('td', {class: 'num'}, numberOrNone(pair[1]))
        ]);
      });
      const condition = searchState(s);
      return statusCard('search', 'Search index', condition, condition.line, [
        ['Globus index', s.index_set === true ? 'set up' : 'not set up yet'],
        ['Globus sign-in', s.signed_in === true ? 'signed in' : 'not signed in']
      ], [
        el('p', null, s.backend === 'globus'
          ? 'Questions in Normal mode are searched with Globus Search, a hosted index that holds one small entry for each session.'
          : "Questions in Normal mode are searched in this computer's copy of the index. Globus Search is not in use yet."),
        el('p', null, 'Each session has one index entry. Entries reach Globus Search only after someone ' +
          'signs in to Globus on this computer and sends them.'),
        rows.length > 0
          ? smallBox(table('facet', ['Index entries', 'Count'], rows))
          : null
      ]);
    }

    function assistantCard(a) {
      const words = assistantWords(a);
      const note = cut(textOf(a.note).trim(), 600);
      return statusCard('spark', 'Assistant', words, words.line, [
        ['Model', cut(a.model, 80) || none()],
        ['Key saved', a.key_present === true ? 'yes' : 'no']
      ], [
        el('p', null, words.sentence),
        el('p', null, 'The assistant works with the personal ARC key of whoever runs pcldb on this computer. ' +
          'A shared server needs an answer from ARC about keys first.'),
        factList([
          ['Right now', words.short],
          ['Switched on', a.enabled === true ? 'yes' : 'no']
        ]),
        // With no key, or switched off, the sentence above says it already: no second time.
        note !== '' && words.state !== 'no_key' && words.state !== 'disabled'
          ? el('p', {class: 'muted'}, ['The last check said: ', note]) : null
      ]);
    }

    // The newest few registrations; the others wait behind "Show ... more".
    function registrationsTable(items) {
      if (items.length === 0) return el('p', {class: 'muted small'}, 'Nothing has been registered yet.');
      const rows = items.map(asObject).slice(0, 200).map(function (item) {
        const path = typeof item.session_path === 'string' ? item.session_path : '';
        const shown = clipText(path, 200);
        let name;
        if (path !== '' && item.action !== 'rejected') {
          name = el('button', {
            type: 'button', class: 'rowlink mono',
            title: shown.clipped ? cut(path, 2000) : 'Show everything about this session'
          }, breakable(shown.text));
          name.addEventListener('click', function () { openDetail(path, lastSegment(path), name, 'status'); });
        } else {
          name = el('span', {class: 'mono'}, path !== '' ? breakable(shown.text) : '\u{2013}');
        }
        return el('tr', null, [
          el('td', null, timeNode(item.at_utc)),
          el('th', {scope: 'row'}, name),
          el('td', null, item.action === 'rejected' ? pill('bad', 'refused') : (cut(actionWords(item.action), 60) || none())),
          el('td', null, cut(sourceWords(item.source), 60) || none()),
          el('td', null, cut(item.user_name, 80) || none())
        ]);
      });
      const headings = ['When', 'Session', 'What happened', 'From', 'By'];
      const first = scrollBox('Newest registrations', table('plain wide regs', headings, rows.slice(0, REGISTRATIONS_SHOWN)), 'card-box');
      if (rows.length <= REGISTRATIONS_SHOWN) return first;
      return [first, fold('Show ' + (rows.length - REGISTRATIONS_SHOWN) + ' more',
        scrollBox('Older registrations', table('plain wide regs', headings, rows.slice(REGISTRATIONS_SHOWN)), 'card-box'))];
    }

    function renderStatus(r) {
      const database = asObject(r.database);
      show(byId('demo-chip'), database.demo === true);
      const cards = byId('status-cards');
      clear(cards);
      append(cards, [databaseCard(database), searchCard(asObject(r.search)), assistantCard(asObject(r.assistant))]);
      const registrations = byId('status-registrations');
      clear(registrations);
      append(registrations, registrationsTable(asArray(r.registrations)));
    }

    // ---- wiring ------------------------------------------------------------------------------

    function wire() {
      byId('signin-form').addEventListener('submit', onSigninSubmit);
      byId('boot-retry').addEventListener('click', function () { loadMe(); });
      byId('signout').addEventListener('click', function () { signOut('You are signed out.', false); });
      window.addEventListener('hashchange', onHashChange);

      byId('ask-form').addEventListener('submit', function (event) {
        event.preventDefault();
        runAsk();
      });
      byId('ask-cancel').addEventListener('click', cancelAsk);
      byId('ask-examples').addEventListener('click', function (event) {
        const button = event.target instanceof Element ? event.target.closest('button') : null;
        // A chip shows a few words and asks the whole question written in index.html.
        const question = button && !button.disabled ? button.getAttribute('data-question') : null;
        if (!question) return;
        byId('ask-question').value = question;
        runAsk();
      });

      // On a wide screen the list of tables is open at the side; on a phone it starts closed.
      if (typeof window.matchMedia === 'function' && window.matchMedia('(min-width: 900px)').matches) {
        byId('schema-panel').setAttribute('open', '');
      }

      byId('arc-user').addEventListener('change', onArcUserChange);
      byId('schema-retry').addEventListener('click', loadSqlSide);
      byId('sql-run').addEventListener('click', function () { runSql(); });
      byId('sql-cancel').addEventListener('click', cancelSql);
      byId('sql-text').addEventListener('keydown', onEditorKey);
      byId('sql-examples').addEventListener('change', onExampleChosen);
      byId('sql-result').addEventListener('click', onResultClick);

      byId('status-refresh').addEventListener('click', function () { loadStatus(); });

      const dialog = byId('detail');
      let pressedOutside = false;
      byId('detail-close').addEventListener('click', closeDetail);
      dialog.addEventListener('close', afterDetailClosed);
      // A click on the dimmed area around the panel closes it (but not a text selection that
      // merely ends out there).
      dialog.addEventListener('mousedown', function (event) { pressedOutside = event.target === dialog; });
      dialog.addEventListener('click', function (event) {
        if (pressedOutside && event.target === dialog) closeDetail();
        pressedOutside = false;
      });
    }

    const code = takeSigninCode();    // first of all: the one-time code leaves the address bar
    state.token = readStoredToken();
    wire();
    start(code);
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = pure;
  }
  if (typeof document !== 'undefined' && typeof window !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', startPage);
    } else {
      startPage();
    }
  }
})();
