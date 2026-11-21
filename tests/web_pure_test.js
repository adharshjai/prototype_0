'use strict';
/*
 * Tests of the website's pure functions (pcldb/web/app.js, part 1), run with Node:
 *
 *     node tests/web_pure_test.js
 *
 * tests/test_web_static.py runs this file when Node is installed. It needs no package: only
 * Node's own "assert" and "path". Every address below is made up for the test.
 */
const assert = require('assert');
const path = require('path');

const app = require(path.join(__dirname, '..', 'pcldb', 'web', 'app.js'));

const tests = [];
function test(name, body) {
  tests.push({name: name, body: body});
}

// ---- loading the script outside a browser ------------------------------------------------------

test('the script loads without a page and leaks no global', function () {
  assert.strictEqual(typeof app, 'object');
  ['API', 'safeGlobusUrl', 'sshCommands', 'showHidden', 'buildCsv', 'formatBytes', 'explainFailure'].forEach(function (name) {
    assert.ok(name in app, 'missing export: ' + name);
    assert.strictEqual(typeof globalThis[name], 'undefined', 'leaked global: ' + name);
  });
});

// ---- the one outside link ----------------------------------------------------------------------

test('the Globus link check accepts only https addresses on app.globus.org', function () {
  const good = 'https://app.globus.org/file-manager?origin_id=8e804f3a-5bac-4b6d-9836-bcd5b9c133ec' +
    '&origin_path=%2Fpcl_photonics%2Fraw%2Framan-portable%2Fsimulated%2Fdemo_20261116%2F';
  assert.strictEqual(app.safeGlobusUrl(good), good);
  assert.strictEqual(app.safeGlobusUrl('https://app.globus.org/'), 'https://app.globus.org/');

  const refused = [
    'http://app.globus.org/file-manager?origin_id=x',                 // not https
    'https://example.org/file-manager',                               // another host
    'https://globus.org/file-manager',                                // the parent domain
    'https://sub.app.globus.org/file-manager',                        // a sub-domain
    'https://app.globus.org.evil.example/file-manager',               // the name as a prefix
    'https://evil.example/https://app.globus.org/',                   // the name in the path
    'https://evil.example/?next=https://app.globus.org/',             // the name in the query
    'https://app.globus.org@evil.example/file-manager',               // the name as a user name
    'https://user:secret@app.globus.org/file-manager',                // credentials in the address
    'https://user@app.globus.org/file-manager',
    'https://app.globus.org:8443/file-manager',                       // another port
    'https://app.globus.org./file-manager',                           // a trailing dot
    'https://app.globus.org',                                         // no path at all
    'https:app.globus.org/file-manager',                              // no slashes
    'https:/app.globus.org/file-manager',
    'https:\\\\app.globus.org\\file-manager',                         // backslashes
    'https://app.globus.org\\@evil.example/',
    'HTTPS://APP.GLOBUS.ORG/file-manager',                            // not as the backend writes it
    '//app.globus.org/file-manager',                                  // protocol-relative
    '/file-manager',                                                  // relative
    'app.globus.org/file-manager',
    'javascript:alert(1)',
    'javascript://app.globus.org/%0Aalert(1)',
    'data:text/html,<p>x</p>',
    'blob:https://app.globus.org/1234',
    'file:///etc/passwd',
    'globus://8e804f3a-5bac-4b6d-9836-bcd5b9c133ec/pcl_photonics/',
    ' https://app.globus.org/file-manager',                           // spaces and control characters
    'https://app.globus.org/file-manager ',
    'https://app.globus.org/file manager',
    'https://app.globus.org/\nfile-manager',
    'https://app.globus.org/\tfile-manager',
    'ht\ttps://app.globus.org/',
    'https://app.globus.org/' + 'a'.repeat(5000),                     // far too long
    'https://@app.globus.org/file-manager',                           // an empty user name
    'https://:@app.globus.org/file-manager',
    'https://app.globus.org:443/file-manager',                        // the port written out
    'https://app.globus.org:/file-manager',
    'https://app.globus.org:80/file-manager',
    'https://APP.globus.org/file-manager',                            // mixed case
    'https://App.Globus.Org/file-manager',
    'Https://app.globus.org/file-manager',
    'https://app%2Eglobus.org/file-manager',                          // a percent-encoded dot in the host
    'https://app.globus.org%2F@evil.example/',
    'https://app.globus.org%40evil.example/',
    'https://app.globus.org#@evil.example/',                          // no path before the fragment
    'https://app.globus.org?x=1',
    'https:///app.globus.org/file-manager',                           // too many slashes
    'https://app.globus.org\\.evil.example/',
    'https://evil.example\\@app.globus.org/',
    'https://\u{430}pp.globus.org/file-manager',                       // a Cyrillic letter that looks like "a"
    'https://\u{ff41}pp.globus.org/file-manager',                      // a full-width "a" (browsers map it to "a")
    'https://app\u{3002}globus.org/file-manager',                      // an ideographic full stop (mapped to ".")
    'https://app.globus\u{ff0e}org/file-manager',
    'https://xn--pp-7ib.globus.org/file-manager',                     // the punycode of a look-alike name
    'https://app.globus.org/\u{202e}gro.livE',                         // a direction override in the path
    'https://app.globus.org/\u{200b}',                                 // a zero-width space
    'https://app.globus.org/caf\u{e9}',                                // anything that is not ASCII
    'https://app.globus.org/\rfile-manager',
    'https://app.globus.org/\u{0}',
    'https://app.globus.org/\u{7f}',
    'https\n://app.globus.org/',
    'https://app.glo\nbus.org/',
    'https://app.glo\tbus.org/',
    '\thttps://app.globus.org/',
    'https://app.globus.org/\u{a0}',                                   // a no-break space
    'view-source:https://app.globus.org/',
    'intent://app.globus.org/#Intent;scheme=https;end',
    'ws://app.globus.org/', 'wss://app.globus.org/', 'ftp://app.globus.org/',
    'https://[::1]/', 'https://127.0.0.1/', 'https://0x7f.1/',
    'https://app.globus.org.:443/',
    new String('https://app.globus.org/'),                            // not a plain string
    '',
    null,
    undefined,
    42,
    {href: 'https://app.globus.org/'},
    ['https://app.globus.org/']
  ];
  refused.forEach(function (value) {
    assert.strictEqual(app.safeGlobusUrl(value), null, 'should be refused: ' + JSON.stringify(value));
  });
  // What is accepted always points at the Globus web app, whatever the rest of the address holds.
  ['https://app.globus.org/file-manager?origin_id=x&origin_path=%2Fa%2F..%2F%2F@evil.example%2F',
    'https://app.globus.org/../../x', 'https://app.globus.org//evil.example/', 'https://app.globus.org/#frag',
    'https://app.globus.org/?next=https://evil.example/', 'https://app.globus.org/a;b=c?d=[e]&f={g}|h^i`j'
  ].forEach(function (value) {
    const safe = app.safeGlobusUrl(value);
    assert.strictEqual(typeof safe, 'string', 'should be accepted: ' + value);
    const parsed = new URL(safe);
    assert.strictEqual(parsed.origin, 'https://app.globus.org', value);
    assert.strictEqual(parsed.username + parsed.password, '', value);
  });
});

// ---- text that cannot be seen ------------------------------------------------------------------

test('characters that cannot be seen are shown as codes', function () {
  assert.strictEqual(app.showHidden('plain text, 100%'), 'plain text, 100%');
  assert.strictEqual(app.showHidden('evil_\u{202e}gpj.exe'), 'evil_<U+202E>gpj.exe');     // right-to-left override
  assert.strictEqual(app.showHidden('a\u{202a}b\u{202b}c\u{202c}d\u{202d}e'), 'a<U+202A>b<U+202B>c<U+202C>d<U+202D>e');
  assert.strictEqual(app.showHidden('a\u{2066}b\u{2067}c\u{2068}d\u{2069}e'), 'a<U+2066>b<U+2067>c<U+2068>d<U+2069>e');
  assert.strictEqual(app.showHidden('a\u{200e}b\u{200f}c\u{61c}d'), 'a<U+200E>b<U+200F>c<U+061C>d');   // direction marks
  assert.strictEqual(app.showHidden('a\u{200b}b\u{200c}c\u{200d}d\u{2060}e\u{feff}f\u{ad}g'),
    'a<U+200B>b<U+200C>c<U+200D>d<U+2060>e<U+FEFF>f<U+00AD>g');                                  // zero width
  assert.strictEqual(app.showHidden('a\u{0}b\u{8}c\u{1b}[31md\u{7f}e\u{85}f'), 'a<U+0000>b<U+0008>c<U+001B>[31md<U+007F>e<U+0085>f');
  assert.strictEqual(app.showHidden('a\u{2028}b\u{2029}c'), 'a<U+2028>b<U+2029>c');            // line and paragraph separators
  assert.strictEqual(app.showHidden('a\u{e0041}\u{e007f}b'), 'a<U+E0041><U+E007F>b');          // invisible tag characters
  // Tabs and line ends stay (a database message needs them), and so does every visible character.
  assert.strictEqual(app.showHidden('line one\n\tline two\r\n'), 'line one\n\tline two\r\n');
  assert.strictEqual(app.showHidden('caf\u{e9} \u{3bc}m \u{5e9}\u{5dc}\u{5d5}\u{5dd} \u{1f600} \u{2026} \u{2013} \u{a0}'),
    'caf\u{e9} \u{3bc}m \u{5e9}\u{5dc}\u{5d5}\u{5dd} \u{1f600} \u{2026} \u{2013} \u{a0}');
  assert.strictEqual(app.showHidden('<img src=x onerror=alert(1)>'), '<img src=x onerror=alert(1)>');
  assert.strictEqual(app.showHidden(null), '');
  assert.strictEqual(app.showHidden(12), '12');
  assert.strictEqual(app.showHidden({a: '\u{202e}'}), '{"a":"<U+202E>"}');
  // 100 kB of it is handled at once.
  assert.strictEqual(app.showHidden('\u{202e}'.repeat(100000)).length, 800000);
  assert.strictEqual(app.cut('abcdef', 3), 'abc\u{2026}');
  assert.strictEqual(app.cut(null, 3), '');
  assert.strictEqual(app.cut({a: 1}, 100), '{"a":1}');
  assert.strictEqual(app.cut('x'.repeat(100000), 120).length, 121);
});

// ---- Expert mode: the SSH commands ------------------------------------------------------------

test('the SSH commands for a plain path, host and user name', function () {
  const made = app.sshCommands('datatransfer.arc.vt.edu', 'demo-user', '/projects/pcl_photonics/raw/raman-portable/1d_y_scan_20261116_225015_281');
  assert.strictEqual(made.user, 'demo-user');
  assert.strictEqual(made.placeholder, false);
  assert.deepStrictEqual(made.commands.map(function (item) { return item.command; }), [
    'scp -r demo-user@datatransfer.arc.vt.edu:/projects/pcl_photonics/raw/raman-portable/1d_y_scan_20261116_225015_281/ .',
    'sftp demo-user@datatransfer.arc.vt.edu:/projects/pcl_photonics/raw/raman-portable/1d_y_scan_20261116_225015_281',
    'rsync -av demo-user@datatransfer.arc.vt.edu:/projects/pcl_photonics/raw/raman-portable/1d_y_scan_20261116_225015_281/ ./1d_y_scan_20261116_225015_281/'
  ]);
  assert.deepStrictEqual(made.commands.map(function (item) { return item.id; }), ['scp', 'sftp', 'rsync']);
  assert.ok(/macOS and Linux/.test(made.commands[2].label), 'the rsync command says where it works');
  assert.ok(!/macOS/.test(made.commands[0].label + made.commands[1].label));
  // No user name typed: the placeholder stands in for it.
  assert.strictEqual(app.USER_PLACEHOLDER, 'YOUR_ARC_USERNAME');
  ['', null, undefined].forEach(function (user) {
    const blank = app.sshCommands('datatransfer.arc.vt.edu', user, '/projects/pcl_photonics/raw/x');
    assert.strictEqual(blank.placeholder, true);
    assert.strictEqual(blank.commands[0].command, 'scp -r YOUR_ARC_USERNAME@datatransfer.arc.vt.edu:/projects/pcl_photonics/raw/x/ .');
    assert.strictEqual(blank.commands[1].command, 'sftp YOUR_ARC_USERNAME@datatransfer.arc.vt.edu:/projects/pcl_photonics/raw/x');
    assert.strictEqual(blank.commands[2].command, 'rsync -av YOUR_ARC_USERNAME@datatransfer.arc.vt.edu:/projects/pcl_photonics/raw/x/ ./x/');
  });
  // A path that already ends with a slash gives the same commands.
  assert.deepStrictEqual(app.sshCommands('h.example', 'u', '/p/x/').commands, app.sshCommands('h.example', 'u', '/p/x').commands);
  // Every character of the rule is allowed.
  assert.ok(app.sshCommands('Host-1.example', 'A.b_c-9', '/p/A-Z_a.z@0+9/x') !== null);
});

test('the SSH commands are offered ONLY for A-Z a-z 0-9 . _ / @ + - (the rule of section 12)', function () {
  const host = 'datatransfer.arc.vt.edu';
  const good = '/projects/pcl_photonics/raw/x';
  assert.ok(app.sshCommands(host, 'demo-user', good) !== null);
  // Every other printable ASCII character, and a few that are not ASCII, in each of the three places.
  const allowed = /[A-Za-z0-9._\/@+-]/;
  const others = [];
  for (let code = 32; code < 127; code += 1) {
    const character = String.fromCharCode(code);
    if (!allowed.test(character)) others.push(character);
  }
  assert.strictEqual(others.length, 95 - 62 - 6, 'the rule allows 62 letters and digits and 6 other characters');
  others.concat(['\n', '\r', '\t', '\u{0}', '\u{7f}', '\u{a0}', '\u{e9}', '\u{202e}', '\u{200b}', '\u{ff0f}', '\u{1f600}']).forEach(function (character) {
    const shown = JSON.stringify(character);
    assert.strictEqual(app.shellSafe('a' + character + 'b'), false, shown);
    assert.strictEqual(app.sshCommands(host, 'demo-user', good + character + 'y'), null, 'in the path: ' + shown);
    assert.strictEqual(app.sshCommands(host, 'demo-user', good + '/' + character), null, 'in the path: ' + shown);
    assert.strictEqual(app.sshCommands('arc' + character + 'vt.edu', 'demo-user', good), null, 'in the host: ' + shown);
    assert.strictEqual(app.sshCommands(host, 'demo' + character + 'user', good), null, 'in the user name: ' + shown);
  });
  // Whole values that must never reach a terminal.
  ['/projects/it\'s here/x', '/projects/$(reboot)/x', '/projects/`id`/x', '/projects/a;rm -rf ~/x', '/projects/a b/x',
    '/projects/a|b', '/projects/a&b', '/projects/a>b', '/projects/a<b', '/projects/a*b', '/projects/a?b', '/projects/~root',
    '/projects/a\\b', '/projects/a"b', '/projects/a#b', '/projects/a!b', '/projects/a%20b', '/projects/a=b',
    '/projects/a:b', '/projects/{a,b}', '/projects/[ab]', '/projects/a\nb', '', ' ', null, undefined, 42, ['/p/x'], {}
  ].forEach(function (path) {
    assert.strictEqual(app.sshCommands(host, 'demo-user', path), null, JSON.stringify(path));
  });
  ['', ' ', 'arc vt.edu', 'arc.vt.edu;id', 'arc.vt.edu:22', '[::1]', null, undefined, 7].forEach(function (bad) {
    assert.strictEqual(app.sshCommands(bad, 'demo-user', good), null, 'host ' + JSON.stringify(bad));
  });
  ['demo user', 'demo;id', 'demo$USER', 'a\'b', 'a"b', 7, {}, ['demo-user']].forEach(function (bad) {
    assert.strictEqual(app.sshCommands(host, bad, good), null, 'user ' + JSON.stringify(bad));
  });
  // Stricter than the rule: a word that starts with "-" would be read as an option, and a path
  // on ARC starts with "/".
  assert.strictEqual(app.sshCommands(host, '-oProxyCommand', good), null);
  assert.strictEqual(app.sshCommands('-oProxyCommand', 'demo-user', good), null);
  assert.strictEqual(app.sshCommands(host, 'demo-user', 'projects/pcl_photonics/raw/x'), null);
  assert.strictEqual(app.sshCommands(host, 'demo-user', '-rf'), null);
  assert.strictEqual(app.sshCommands(host, 'demo-user', '/projects/-rf'), null);
  assert.strictEqual(app.sshCommands(host, 'demo-user', '/'), null);
  assert.strictEqual(app.sshCommands(host, 'demo-user', '//'), null);
  // No command ever holds a character outside the rule, apart from the spaces between its words.
  const made = app.sshCommands('Host-1.example', 'A.b_c-9', '/p/A-Z_a.z@0+9/x');
  made.commands.forEach(function (item) {
    item.command.split(' ').forEach(function (word) {
      assert.ok(/^[A-Za-z0-9._\/@+:-]+$/.test(word), word);     // ":" only between host and path
    });
    assert.strictEqual(item.command.split(':').length, 2, item.command);
  });
});

test('the ARC user name a person types', function () {
  ['demo-user', 'demo_user', 'a', 'A1', 'abc.def', '_svc', 'a'.repeat(64)].forEach(function (name) {
    assert.strictEqual(app.userNameProblem(name), '', name);
  });
  assert.strictEqual(app.userNameProblem(''), '', 'an empty box is not a problem: the placeholder is used');
  ['demo user', '-demo', '.demo', 'demo@vt.edu', 'demo/user', 'demo+x', 'a'.repeat(65), 'demo;id', '$(id)', 'd\u{e9}mo',
    'demo\u{202e}', 'demo\n'].forEach(function (name) {
    assert.ok(app.userNameProblem(name).length > 20, JSON.stringify(name));
    assert.ok(/letters, digits/.test(app.userNameProblem(name)));
  });
  // Whatever passes here also passes the rule for the commands.
  assert.ok(app.sshCommands('h.example', 'a'.repeat(64), '/p/x') !== null);
});


// ---- CSV ---------------------------------------------------------------------------------------

test('CSV cells: quotes, commas, newlines, NULL and empty text', function () {
  assert.strictEqual(app.csvCell('plain'), 'plain');
  assert.strictEqual(app.csvCell('a,b'), '"a,b"');
  assert.strictEqual(app.csvCell('say "hi"'), '"say ""hi"""');
  assert.strictEqual(app.csvCell('line one\nline two'), '"line one\nline two"');
  assert.strictEqual(app.csvCell('line one\r\nline two'), '"line one\r\nline two"');
  assert.strictEqual(app.csvCell(null), '');
  assert.strictEqual(app.csvCell(undefined), '');
  assert.strictEqual(app.csvCell(''), '""');
  assert.strictEqual(app.csvCell(true), 'true');
  assert.strictEqual(app.csvCell(false), 'false');
  assert.strictEqual(app.csvCell(0), '0');
  assert.strictEqual(app.csvCell(12.5), '12.5');
  assert.strictEqual(app.csvCell(['130', '131']), '"[""130"",""131""]"');
  assert.strictEqual(app.csvCell({label: 'adenine', count: 3}), '"{""label"":""adenine"",""count"":3}"');
  assert.strictEqual(app.csvCell('caf\u00e9 \u03bcm'), 'caf\u00e9 \u03bcm');
});

test('CSV cells that a spreadsheet would run as a formula get a single quote in front', function () {
  assert.strictEqual(app.csvCell('=1+1'), "'=1+1");
  assert.strictEqual(app.csvCell('+1+1'), "'+1+1");
  assert.strictEqual(app.csvCell('-1+1'), "'-1+1");
  assert.strictEqual(app.csvCell('@SUM(A1:A2)'), "'@SUM(A1:A2)");
  assert.strictEqual(app.csvCell('=HYPERLINK("x","y")'), '"\'=HYPERLINK(""x"",""y"")"');
  assert.strictEqual(app.csvCell('=cmd|\' /C calc\'!A0'), "'=cmd|' /C calc'!A0");
  assert.strictEqual(app.csvCell('\t=1+1'), "'\t=1+1");
  assert.strictEqual(app.csvCell('\r=1+1'), '"\'\r=1+1"');
  assert.strictEqual(app.csvCell('-'), "'-");
  assert.strictEqual(app.csvCell('+5'), "'+5");
  assert.strictEqual(app.csvCell('-5a'), "'-5a");
  // Text that merely holds such a character later is left alone.
  assert.strictEqual(app.csvCell('a=b'), 'a=b');
  assert.strictEqual(app.csvCell('demo-operator-a'), 'demo-operator-a');
  assert.strictEqual(app.csvCell('x@example.org'), 'x@example.org');
  // A plain number cannot be a formula, so a negative number stays a number.
  assert.strictEqual(app.csvCell(-5), '-5');
  assert.strictEqual(app.csvCell(-0.25), '-0.25');
  assert.strictEqual(app.csvCell(-1.5e-7), '-1.5e-7');
  assert.strictEqual(app.csvCell('-5'), '-5');
  assert.strictEqual(app.csvCell('-9007199254740993'), '-9007199254740993');
});

test('a whole CSV file: byte order mark, header, CRLF lines, short rows', function () {
  const text = app.buildCsv(['path', 'n', 'note'], [
    ['raw/a', 1, null],
    ['raw/b,c', -2, '=SUM(A1)'],
    ['raw/d']
  ]);
  assert.strictEqual(text, '\ufeffpath,n,note\r\nraw/a,1,\r\n"raw/b,c",-2,\'=SUM(A1)\r\nraw/d,,\r\n');
  assert.strictEqual(app.buildCsv(['only'], []), '\ufeffonly\r\n');
  // A header is escaped like any other cell.
  assert.strictEqual(app.buildCsv(['=x', 'a"b'], [[1, 2]]), '\ufeff\'=x,"a""b"\r\n1,2\r\n');
  // Bad input does not throw.
  assert.strictEqual(app.buildCsv(null, null), '\ufeff\r\n');
  assert.strictEqual(app.buildCsv(['a'], ['not a row']), '\ufeffa\r\n\r\n');
  // The file name is safe for every system.
  assert.ok(/^pcl_query_\d{8}_\d{6}\.csv$/.test(app.csvFileName(new Date())));
  assert.ok(/^pcl_query_\d{8}_\d{6}\.csv$/.test(app.csvFileName('not a date')));
});

// ---- sizes, numbers, dates ---------------------------------------------------------------------

test('sizes in bytes, kB, MB and GB', function () {
  assert.strictEqual(app.formatBytes(0), '0 bytes');
  assert.strictEqual(app.formatBytes(1), '1 byte');
  assert.strictEqual(app.formatBytes(999), '999 bytes');
  assert.strictEqual(app.formatBytes(1000), '1.0 kB');
  assert.strictEqual(app.formatBytes(1234), '1.2 kB');
  assert.strictEqual(app.formatBytes(9949), '9.9 kB');
  assert.strictEqual(app.formatBytes(9950), '10 kB');
  assert.strictEqual(app.formatBytes(15360), '15 kB');
  assert.strictEqual(app.formatBytes(999499), '999 kB');
  assert.strictEqual(app.formatBytes(999500), '1.0 MB');
  assert.strictEqual(app.formatBytes(2500000), '2.5 MB');
  assert.strictEqual(app.formatBytes(123456789), '123 MB');
  assert.strictEqual(app.formatBytes(3200000000), '3.2 GB');
  assert.strictEqual(app.formatBytes(5e12), '5000 GB');
  ['12', null, undefined, -1, NaN, Infinity, {}].forEach(function (value) {
    assert.strictEqual(app.formatBytes(value), '');
  });
});

test('the backend time text is read strictly and shown in the reader\'s time zone', function () {
  assert.strictEqual(app.parseUtc('2026-11-17T03:50:15Z').toISOString(), '2026-11-17T03:50:15.000Z');
  assert.strictEqual(app.parseUtc('2026-11-17T03:50:15.281Z').toISOString(), '2026-11-17T03:50:15.281Z');
  assert.strictEqual(app.parseUtc('2026-11-17T03:50:15.2Z').toISOString(), '2026-11-17T03:50:15.200Z');
  assert.strictEqual(app.parseUtc('2026-11-17T03:50:15.281999999Z').toISOString(), '2026-11-17T03:50:15.281Z');
  assert.strictEqual(app.parseUtc('2024-02-29T23:59:59Z').toISOString(), '2024-02-29T23:59:59.000Z');   // a leap day
  ['', '2026-11-17', '2026-11-17 03:50:15', '2026-11-17T03:50:15', '2026-11-17T03:50:15+01:00',
    '2026-13-17T03:50:15Z', '2026-11-17T24:00:00Z', '2026-11-17T03:61:15Z', '2026-02-31T03:50:15Z',
    '2026-04-31T00:00:00Z', '2025-02-29T00:00:00Z', '2026-11-00T00:00:00Z', 'yesterday', null, 5]
    .forEach(function (value) {
      assert.strictEqual(app.parseUtc(value), null, 'should not be a time: ' + value);
      assert.strictEqual(app.formatDateTime(value, {locale: 'en-US', timeZone: 'UTC'}), '');
    });

  const clean = function (text) { return text.replace(/\s+/g, ' '); };
  const utc = clean(app.formatDateTime('2026-11-17T03:50:15Z', {locale: 'en-US', timeZone: 'UTC'}));
  assert.ok(utc.indexOf('Nov 17, 2026') !== -1 && utc.indexOf('3:50') !== -1, utc);
  const eastern = clean(app.formatDateTime('2026-11-17T03:50:15Z', {locale: 'en-US', timeZone: 'America/New_York'}));
  assert.ok(eastern.indexOf('Nov 16, 2026') !== -1 && eastern.indexOf('10:50') !== -1, eastern);
  const seconds = clean(app.formatDateTime('2026-11-17T03:50:15Z', {locale: 'en-US', timeZone: 'UTC', seconds: true}));
  assert.ok(seconds.indexOf('3:50:15') !== -1, seconds);
  // With no locale and zone given it uses the computer's own, and still gives text.
  assert.ok(app.formatDateTime('2026-11-17T03:50:15Z').length > 0);
  // An unknown time zone gives '' (the page then shows the UTC text) instead of throwing.
  assert.strictEqual(app.formatDateTime('2026-11-17T03:50:15Z', {timeZone: 'Not/AZone'}), '');
});

test('numbers and small text helpers', function () {
  assert.strictEqual(app.formatNumber(1234567, 'en-US'), '1,234,567');
  assert.strictEqual(app.formatNumber(16, 'en-US'), '16');
  assert.strictEqual(app.formatNumber(null), '');
  assert.strictEqual(app.formatNumber('12'), '12');
  assert.strictEqual(app.textOf(null), '');
  assert.strictEqual(app.textOf(undefined), '');
  assert.strictEqual(app.textOf('<b>x</b>'), '<b>x</b>');
  assert.strictEqual(app.textOf(0), '0');
  assert.strictEqual(app.textOf(false), 'false');
  assert.strictEqual(app.textOf({a: 1}), '{"a":1}');
  assert.strictEqual(app.listText(['130', '', null, 131]), '130, 131');
  assert.strictEqual(app.listText('130'), '');
  assert.strictEqual(app.lastSegment('raw/raman-portable/simulated/demo_1/'), 'demo_1');
  assert.strictEqual(app.lastSegment(''), '');
  assert.strictEqual(app.oneLine('select *\n  from sessions\t where 1 = 1 '), 'select * from sessions where 1 = 1');
  assert.strictEqual(app.firstLine('\n  first\nsecond'), 'first');
  assert.deepStrictEqual(app.clipText('short', 10), {text: 'short', clipped: false});
  assert.deepStrictEqual(app.clipText('abcdefghij', 4), {text: 'abcd\u2026', clipped: true});
  // A character made of two code units is never cut in half.
  assert.deepStrictEqual(app.clipText('ab\ud83d\ude00cd', 3), {text: 'ab\u2026', clipped: true});
  assert.strictEqual(app.shortHash('a'.repeat(64)), 'a'.repeat(12) + '\u2026');
  assert.strictEqual(app.shortHash('abc'), 'abc');
  assert.strictEqual(app.shortHash(null), '');
  assert.deepStrictEqual(app.asArray('x'), []);
  assert.deepStrictEqual(app.asObject(['x']), {});
  assert.deepStrictEqual(app.asObject(null), {});
  // A key from the backend never reaches a built-in property of a table.
  assert.strictEqual(app.lookup({a: 1}, 'constructor', 'fallback'), 'fallback');
  assert.strictEqual(app.lookup({a: 1}, '__proto__', 'fallback'), 'fallback');
  assert.strictEqual(app.lookup({a: 1}, 'a', 'fallback'), 1);
});

// ---- the address bar, the token, the backend paths ---------------------------------------------

test('the part after # is a sign-in code, a view, or nothing', function () {
  const code = 'Zm9vYmFyYmF6cXV4LV8wMTIzNDU2Nzg5';   // 32 url-safe characters, made up
  assert.deepStrictEqual(app.parseHash('#signin=' + code), {kind: 'signin', code: code});
  assert.deepStrictEqual(app.TABS, ['normal', 'expert', 'status']);
  assert.deepStrictEqual(app.parseHash('#normal'), {kind: 'tab', tab: 'normal'});
  assert.deepStrictEqual(app.parseHash('#expert'), {kind: 'tab', tab: 'expert'});
  assert.deepStrictEqual(app.parseHash('#status'), {kind: 'tab', tab: 'status'});
  // The names of an early build still land on the right view.
  assert.deepStrictEqual(app.parseHash('#ask'), {kind: 'tab', tab: 'normal'});
  assert.deepStrictEqual(app.parseHash('#sql'), {kind: 'tab', tab: 'expert'});
  // A damaged sign-in link is still a sign-in link, so the page removes it from the address.
  ['#signin=', '#signin=short', '#signin=' + code + '&next=x', '#signin=' + code + ' ',
    '#signin=%3Cscript%3E' + code, '#signin=' + 'a'.repeat(500)].forEach(function (hash) {
    assert.deepStrictEqual(app.parseHash(hash), {kind: 'signin', code: null}, hash);
  });
  // Nothing else is a tab or a code.
  ['', '#', '#main', '#Normal', '#EXPERT', '#normal?x=1', '#expert/', '#signin', '#sign=' + code, 'signin=' + code,
    '#normal#signin=' + code, '#constructor', '#__proto__', '#toString', '#hasOwnProperty', null, undefined, 7
  ].forEach(function (hash) {
    assert.deepStrictEqual(app.parseHash(hash), {kind: 'none'}, String(hash));
  });
});

test('Expert is only shown to a person who may run SQL', function () {
  const admin = ['ask', 'sql', 'register', 'manage'];
  const viewer = ['ask'];
  assert.strictEqual(app.viewFor('normal', admin), 'normal');
  assert.strictEqual(app.viewFor('expert', admin), 'expert');
  assert.strictEqual(app.viewFor('status', admin), 'status');
  assert.strictEqual(app.viewFor('expert', ['ask', 'sql']), 'expert');
  // A stale #expert (or the old #sql) falls back to Normal for a role without "sql".
  assert.strictEqual(app.viewFor('expert', viewer), 'normal');
  assert.strictEqual(app.viewFor(app.parseHash('#sql').tab, viewer), 'normal');
  assert.strictEqual(app.viewFor(app.parseHash('#expert').tab, ['ask', 'register']), 'normal');
  assert.strictEqual(app.viewFor('status', viewer), 'status');
  assert.strictEqual(app.viewFor('normal', viewer), 'normal');
  ['expert'].forEach(function (tab) {
    [undefined, null, [], 'sql', {sql: true}, ['SQL'], [['sql']]].forEach(function (capabilities) {
      assert.strictEqual(app.viewFor(tab, capabilities), 'normal', JSON.stringify(capabilities));
    });
  });
  [undefined, null, '', 'sql', 'ask', 'constructor', 7].forEach(function (tab) {
    assert.strictEqual(app.viewFor(tab, admin), 'normal', String(tab));
  });
});

test('what counts as an access token', function () {
  assert.ok(app.looksLikeToken('pcl_' + 'A'.repeat(43)));
  assert.ok(app.looksLikeToken('pcl_' + 'aZ09_-'.repeat(7) + 'x'));
  ['', 'pcl_', 'pcl_short', 'PCL_' + 'A'.repeat(43), 'pcl_' + 'A'.repeat(42) + ' ', ' pcl_' + 'A'.repeat(43),
    'pcl_' + 'A'.repeat(20) + '\r\nX-Injected: 1', 'pcl_' + 'A'.repeat(20) + '"', 'pcl_' + 'A'.repeat(300),
    'Bearer pcl_' + 'A'.repeat(43), null, undefined, 42].forEach(function (value) {
    assert.strictEqual(app.looksLikeToken(value), false, 'not a token: ' + JSON.stringify(value));
  });
});

test('the page only calls backend paths of the specification', function () {
  const known = ['/api/v1/health', '/api/v1/signin', '/api/v1/me', '/api/v1/status', '/api/v1/ask',
    '/api/v1/search', '/api/v1/session', '/api/v1/sql', '/api/v1/sql/schema', '/api/v1/sql/examples',
    '/api/v1/sql/history', '/api/v1/registrations'];
  app.API_PATHS.forEach(function (item) {
    assert.ok(known.indexOf(item) !== -1, 'not in section 11: ' + item);
    assert.ok(app.isApiPath(item));
  });
  assert.ok(Object.isFrozen(app.API) && Object.isFrozen(app.API_PATHS));
  assert.ok(app.isApiPath('/api/v1/session?path=raw%2Fx'));
  ['/api/v1/', '/api/v1/sql/../../x', '/api/v2/me', 'https://evil.example/api/v1/me', '//evil.example/api/v1/me',
    '/api/v1/me/', 'api/v1/me', '', null].forEach(function (item) {
    assert.strictEqual(app.isApiPath(item), false, 'should not be called: ' + item);
  });
  // The session path is one query value: nothing in it can add a parameter or leave the path.
  assert.strictEqual(app.sessionAddress('raw/raman-portable/simulated/demo_1'),
    '/api/v1/session?path=raw%2Framan-portable%2Fsimulated%2Fdemo_1');
  assert.strictEqual(app.sessionAddress('a b&token=x#y?z=\u00e9'), '/api/v1/session?path=a%20b%26token%3Dx%23y%3Fz%3D%C3%A9');
  assert.ok(app.isApiPath(app.sessionAddress('../../etc')));
});

// ---- words for the Normal view -----------------------------------------------------------------

test('who read the question, where it was searched, how many matched', function () {
  assert.strictEqual(app.plannerLabel('llm'), 'assistant');
  assert.strictEqual(app.plannerLabel('rules'), 'keyword rules');
  assert.strictEqual(app.plannerLabel('other'), 'other');
  assert.strictEqual(app.plannerLabel(null), 'not stated');
  assert.strictEqual(app.backendLabel('globus'), 'Globus Search');
  assert.strictEqual(app.backendLabel('local'), "this computer's copy of the index");
  assert.strictEqual(app.backendLabel(undefined), 'not stated');
  assert.strictEqual(app.countLine(10, 12), 'Showing 10 of 12 sessions');
  assert.strictEqual(app.countLine(12, 12), 'Showing 12 of 12 sessions');
  assert.strictEqual(app.countLine(1, 1), 'Showing 1 of 1 session');
  assert.strictEqual(app.countLine(3, undefined), 'Showing 3 of 3 sessions');
  assert.strictEqual(app.countLine(3, 2), 'Showing 3 of 3 sessions');
  assert.strictEqual(app.countLine(0, 0), 'No sessions matched.');
  // A count without a list is not "nothing matched".
  assert.strictEqual(app.countLine(0, 5), '5 sessions matched, but none is listed here.');
  assert.strictEqual(app.countLine(0, 1), '1 session matched, but none is listed here.');
  assert.strictEqual(app.countLine(null, null), 'No sessions matched.');
  assert.strictEqual(app.timingText({plan: 12.4, search: 3, answer: 0}),
    'Time taken: reading the question 12 ms, searching 3 ms, writing the answer 0 ms.');
  assert.strictEqual(app.timingText({search: 3}), 'Time taken: searching 3 ms.');
  assert.strictEqual(app.timingText(null), '');
  assert.strictEqual(app.timingText({plan: 'fast', search: 3, answer: -1}), 'Time taken: searching 3 ms.');
  assert.strictEqual(app.timingText({plan: NaN, search: Infinity}), '');
  assert.strictEqual(app.bucketWords(true), 'yes');
  assert.strictEqual(app.bucketWords(false), 'no');
  assert.strictEqual(app.bucketWords('raman-portable'), 'raman-portable');
  assert.strictEqual(app.bucketWords(null), '');
  assert.strictEqual(app.bucketWords(3), '3');
  assert.strictEqual(app.fieldWords('instrument'), 'instrument');
  assert.strictEqual(app.fieldWords('sample_ids'), 'sample');
  assert.strictEqual(app.fieldWords('label_counts.label'), 'label');
  assert.strictEqual(app.fieldWords('items_planned'), 'items planned');
  assert.strictEqual(app.fieldWords('constructor'), 'constructor');
  assert.strictEqual(app.fieldWords(''), 'value');
});

test('labels, review marks and completion of one result', function () {
  const entry = {
    labels: ['UNKNOWN', 'adenine'],
    label_counts: [{label: 'UNKNOWN', count: 4}, {label: 'adenine', count: 3}],
    n_needs_review: 2
  };
  assert.strictEqual(app.labelsText(entry), 'UNKNOWN (4), adenine (3)');
  assert.strictEqual(app.labelsText({labels: ['r6g', 'adenine']}), 'r6g, adenine');
  assert.strictEqual(app.labelsText({label_counts: [{label: 'r6g'}, {count: 2}, null]}), 'r6g');
  assert.strictEqual(app.labelsText({}), '');
  assert.strictEqual(app.labelsText(null), '');
  assert.strictEqual(app.needsReviewCount(entry), 2);
  assert.strictEqual(app.needsReviewCount({n_needs_review: 0}), 0);
  assert.strictEqual(app.needsReviewCount({n_needs_review: '3'}), 0);
  assert.strictEqual(app.needsReviewCount(null), 0);
  assert.strictEqual(app.reviewSentence('pending', 1), '1 point still needs review.');
  assert.strictEqual(app.reviewSentence('pending', 3), '3 points still need review.');
  assert.strictEqual(app.reviewSentence('done', 0), 'The flagged points have been reviewed.');
  assert.strictEqual(app.reviewSentence('none', 0), 'No point was flagged for review.');
  assert.strictEqual(app.reviewSentence(undefined, undefined), '');
  assert.strictEqual(app.completionKind('complete'), 'ok');
  assert.strictEqual(app.completionKind('partial'), 'warn');
  assert.strictEqual(app.completionKind('failed'), 'bad');
  assert.strictEqual(app.completionKind('unknown'), 'plain');
  assert.strictEqual(app.completionKind('toString'), 'plain');
});

test('the request that ran is shown as indented JSON text', function () {
  const request = {q: 'adenine', limit: 10, filters: [{type: 'match_any', field_name: 'labels', values: ['adenine']}]};
  assert.strictEqual(app.prettyJson(request), JSON.stringify(request, null, 2));
  assert.ok(app.prettyJson(request).indexOf('\n  "limit": 10') !== -1);
  assert.strictEqual(app.prettyJson({note: '</pre><script>x</script>'}), '{\n  "note": "</pre><script>x</script>"\n}');
  assert.strictEqual(app.prettyJson(undefined), '');
  assert.strictEqual(app.prettyJson(null), 'null');
});

// ---- words for the header, the Expert view and the Status view ---------------------------------

test('the engine chip', function () {
  assert.strictEqual(app.engineLabel({name: 'postgresql', version: '18.7'}), 'PostgreSQL 18.7');
  assert.strictEqual(app.engineLabel({name: 'sqlite', version: '3.45'}), 'SQLite 3.45');
  assert.strictEqual(app.engineLabel({name: 'duckdb', version: '1.0'}), 'duckdb 1.0');
  assert.strictEqual(app.engineLabel({name: 'sqlite'}), 'SQLite');
  assert.strictEqual(app.engineLabel({version: '1'}), '');
  assert.strictEqual(app.engineLabel(null), '');
  assert.strictEqual(app.engineName('PostgreSQL'), 'PostgreSQL');
  assert.strictEqual(app.engineName('constructor'), 'constructor');
});

test('names put into a query, grid cells, result lines', function () {
  assert.strictEqual(app.sqlIdentifier('sessions'), 'sessions');
  assert.strictEqual(app.sqlIdentifier('started_utc'), 'started_utc');
  assert.strictEqual(app.sqlIdentifier('Mixed'), '"Mixed"');
  assert.strictEqual(app.sqlIdentifier('two words'), '"two words"');
  assert.strictEqual(app.sqlIdentifier('1st'), '"1st"');
  assert.strictEqual(app.sqlIdentifier('a"b; drop'), '"a""b; drop"');
  assert.deepStrictEqual(app.cellDisplay(null), {text: 'NULL', kind: 'null'});
  assert.deepStrictEqual(app.cellDisplay(undefined), {text: 'NULL', kind: 'null'});
  assert.deepStrictEqual(app.cellDisplay('NULL'), {text: 'NULL', kind: 'text'});
  assert.deepStrictEqual(app.cellDisplay(true), {text: 'true', kind: 'bool'});
  assert.deepStrictEqual(app.cellDisplay(false), {text: 'false', kind: 'bool'});
  assert.deepStrictEqual(app.cellDisplay(0), {text: '0', kind: 'number'});
  assert.deepStrictEqual(app.cellDisplay(0.7195311299559257), {text: '0.7195311299559257', kind: 'number'});
  assert.deepStrictEqual(app.cellDisplay(''), {text: '', kind: 'text'});
  assert.deepStrictEqual(app.cellDisplay(['a', 'b']), {text: '["a","b"]', kind: 'json'});
  assert.deepStrictEqual(app.cellDisplay({a: null}), {text: '{"a":null}', kind: 'json'});
  assert.strictEqual(app.rowsLine(3, 12), '3 rows in 12 ms');
  assert.strictEqual(app.rowsLine(1, 0.4), '1 row in 0 ms');
  assert.strictEqual(app.rowsLine(0, 5), '0 rows in 5 ms');
  assert.strictEqual(app.rowsLine(7), '7 rows');
  assert.strictEqual(app.truncatedLine(1000), 'Showing the first 1000 rows (there are more)');
  assert.strictEqual(app.truncatedLine(1), 'Showing the first 1 row (there are more)');
  assert.strictEqual(app.sqlFailureTitle('sql_refused'), 'This query is not allowed');
  assert.strictEqual(app.sqlFailureTitle('sql_error'), 'The database could not run this query');
  assert.strictEqual(app.sqlFailureTitle('sql_timeout'), 'The query took too long and was stopped');
  assert.strictEqual(app.sqlFailureTitle('anything'), 'The query was not run');
  assert.strictEqual(app.historyOutcome({ok: true, n_rows: 3, elapsed_ms: 12}), '3 rows in 12 ms');
  assert.strictEqual(app.historyOutcome({ok: true, n_rows: null}), 'ran');
  assert.strictEqual(app.historyOutcome({ok: false, error_text: 'no such table: nothing\nmore'}),
    'did not run: no such table: nothing');
  assert.strictEqual(app.historyOutcome({ok: false, error_text: null}), 'did not run');
  assert.strictEqual(app.fileCountLine(23, 23), '23 files');
  assert.strictEqual(app.fileCountLine(1, 1), '1 file');
  assert.strictEqual(app.fileCountLine(500, 1234), 'Showing the first 500 of 1234 files');
  assert.strictEqual(app.fileCountLine(0, 0), 'No files are listed.');
});

test('a result cell that opens a session', function () {
  // Only in a column named path or session_path, and only text that starts with a letter or digit.
  assert.strictEqual(app.sessionPathOfCell('path', 'raw/raman-portable/x'), 'raw/raman-portable/x');
  assert.strictEqual(app.sessionPathOfCell('session_path', 'raw/raman-portable/x'), 'raw/raman-portable/x');
  assert.strictEqual(app.sessionPathOfCell('path', '1d_scan'), '1d_scan');
  assert.strictEqual(app.sessionPathOfCell('path', 'Z'), 'Z');
  ['run_path', 'Path', 'PATH', 'session_path ', 'storage_path', 'data_path', 'rel_path', 'paths', '', null, undefined,
    'constructor', 0].forEach(function (name) {
    assert.strictEqual(app.sessionPathOfCell(name, 'raw/raman-portable/x'), null, String(name));
  });
  ['/projects/x', './x', '../x', '-rf', ' raw/x', '_raw/x', '', '<img>', '\u{202e}raw/x', '\u{e9}t\u{e9}/x', '\nraw/x',
    null, undefined, 5, true, ['raw/x'], {path: 'raw/x'}, 'a'.repeat(1025)].forEach(function (value) {
    assert.strictEqual(app.sessionPathOfCell('path', value), null, JSON.stringify(value));
  });
  assert.strictEqual(app.sessionPathOfCell('path', 'a'.repeat(1024)).length, 1024);
});

test('registrations and index entries in plain words', function () {
  assert.strictEqual(app.actionWords('created'), 'registered');
  assert.strictEqual(app.actionWords('updated'), 'replaced');
  assert.strictEqual(app.actionWords('unchanged'), 'already registered, nothing changed');
  assert.strictEqual(app.actionWords('rejected'), 'refused');
  assert.strictEqual(app.actionWords('other'), 'other');
  assert.strictEqual(app.actionWords(null), '');
  assert.strictEqual(app.actionWords('constructor'), 'constructor');
  assert.strictEqual(app.sourceWords('upload'), 'an upload');
  assert.strictEqual(app.sourceWords('tree'), 'a folder tree');
  assert.strictEqual(app.sourceWords('index'), 'the nightly index');
  assert.strictEqual(app.sourceWords('demo'), 'demo data');
  assert.strictEqual(app.sourceWords('other'), 'other');
  assert.strictEqual(app.sourceWords(null), '');
  assert.deepStrictEqual(
    app.entryStateRows({synced: 2, pending: 16, sent: 0, failed: 0, delete_pending: 1, held_back: 4}),
    [['Waiting to be sent to Globus Search', 16], ['Sent, being checked', 0], ['In Globus Search', 2],
      ['Could not be sent', 0], ['Waiting to be removed', 1], ['held back', 4]]);
  assert.deepStrictEqual(app.entryStateRows(null), []);
});

test('what the Status view says about the assistant', function () {
  const on = {enabled: true, key_present: true, model: 'gpt-oss-120b'};
  function words(state) { return app.assistantWords(Object.assign({}, on, {state: state})); }
  assert.strictEqual(words('ready').state, 'ready');
  assert.ok(/can be reached/.test(words('ready').sentence));
  assert.ok(/writes the short answer/.test(words('ready').sentence));
  // Off the VT network: say to connect to the VPN, never to make a new key.
  assert.ok(/not on the VT campus network or VPN/.test(words('off_vpn').sentence));
  assert.ok(/Connect to the VT VPN/.test(words('off_vpn').sentence));
  assert.ok(!/new key/.test(words('off_vpn').sentence));
  assert.ok(/refused the key/.test(words('bad_key').sentence));
  assert.ok(/llm\.arc\.vt\.edu/.test(words('bad_key').sentence));
  assert.ok(/did not answer the last check/.test(words('error').sentence));
  assert.ok(/not been checked yet/.test(words('unknown').sentence));
  // A state this page does not know, or none at all, is "not checked yet".
  ['surprise', 'constructor', '__proto__', null, undefined, 7, {}].forEach(function (state) {
    assert.strictEqual(words(state).state, 'unknown', String(state));
  });
  // Every state that means "no assistant" says what happens instead.
  ['off_vpn', 'bad_key', 'error', 'unknown'].forEach(function (state) {
    assert.ok(/keyword rules read the questions/i.test(words(state).sentence), state);
    assert.ok(/no written answer is given/.test(words(state).sentence), state);
  });
  // The settings win over the last check.
  assert.strictEqual(app.assistantWords({enabled: false, key_present: true, state: 'ready'}).state, 'disabled');
  assert.ok(/switched off in the settings/.test(app.assistantWords({enabled: false, state: 'ready'}).sentence));
  assert.strictEqual(app.assistantWords({enabled: true, key_present: false, state: 'ready'}).state, 'no_key');
  assert.ok(/No ARC assistant key is saved/.test(app.assistantWords({enabled: true, key_present: false}).sentence));
  assert.strictEqual(app.assistantWords(null).state, 'disabled');
  // Each state has a few words for the list of facts, and they never claim it works when it does not.
  ['off_vpn', 'bad_key', 'no_key', 'disabled', 'error', 'unknown'].forEach(function (state) {
    const short = state === 'no_key' ? app.assistantWords({enabled: true, key_present: false}).short
      : (state === 'disabled' ? app.assistantWords({enabled: false}).short : words(state).short);
    assert.ok(short.length > 5 && !/^can be reached/.test(short), state + ': ' + short);
  });
  assert.strictEqual(words('ready').short, 'can be reached');
});

// ---- the session detail ------------------------------------------------------------------------

test('every field of a session lands in exactly one group', function () {
  const session = {
    path: 'raw/raman-portable/simulated/demo_1', instrument: 'raman-portable', area: 'simulated',
    folder: 'demo_1', session_name: 'demo_1', session_id: null, is_copy: false,
    started_utc: '2026-11-17T03:50:15.281Z', ended_utc: null, simulated: true, sample_ids: ['130'],
    operator: 'demo-operator-a', location: null, method: '1d_y_scan', items: 13, items_planned: 13,
    completion: 'complete', notes: '', tags: ['demo'], format: 'raman-session/1 folder',
    uploaded_utc: null, upload_id: null, has_card: true, card_json: {schema: 'pcl-session/1'},
    corrections_json: [], corrected: false, withdrawn: false, bytes: 123456, n_files: 23,
    sums_sha256: 'a'.repeat(64), verified_utc: null, verify_ok: null, upload_json: null,
    collection_id: '8e804f3a-5bac-4b6d-9836-bcd5b9c133ec', data_path: '/pcl_photonics/raw/x',
    storage_path: '/projects/pcl_photonics/raw/x', globus_url: 'globus://x/', web_url: 'https://app.globus.org/',
    subject: 'urn:pcl:dataset:1', registered_utc: '2026-11-17T04:00:00.000Z', registered_by: 'admin',
    updated_utc: '2026-11-17T04:00:00.000Z', source: 'demo', doc_sha256: null,
    a_new_column: 'kept', __proto_like: 1
  };
  const groups = app.groupSessionFields(session);
  const seen = [];
  groups.forEach(function (group) {
    assert.ok(typeof group.title === 'string' && group.title.length > 0);
    assert.ok(group.rows.length > 0);
    group.rows.forEach(function (row) {
      assert.ok(typeof row.label === 'string' && row.label.length > 0);
      assert.strictEqual(row.value, session[row.key]);
      seen.push(row.key);
    });
  });
  assert.deepStrictEqual(seen.slice().sort(), Object.keys(session).sort());
  assert.strictEqual(new Set(seen).size, seen.length);
  const other = groups[groups.length - 1];
  assert.strictEqual(other.title, 'Other fields');
  assert.deepStrictEqual(other.rows.map(function (row) { return row.key; }), ['__proto_like', 'a_new_column']);
  assert.deepStrictEqual(app.groupSessionFields(null), []);
  assert.deepStrictEqual(app.groupSessionFields({}), []);
});

test('how the session got to ARC, in words', function () {
  const upload = {transport: 'globus', upload_id: 'u000000000001', checked: true, check_method: 'globus-checksum',
    checked_utc: '2026-11-17T04:00:00.000Z'};
  assert.deepStrictEqual(app.uploadFacts(upload), [
    ['Sent with', 'Globus Transfer', 'text'],
    ['Copy on ARC confirmed', 'yes', 'text'],
    ['How it was checked', 'Globus gave a checksum for every file, and each one matched', 'text'],
    ['Checked', '2026-11-17T04:00:00.000Z', 'time'],
    ['Upload id', 'u000000000001', 'mono']
  ]);
  const ssh = app.uploadFacts({transport: 'ssh', checked: false, check_method: 'sha256-on-arc'});
  assert.deepStrictEqual(ssh.map(function (row) { return row[1]; }), [
    'an upload over SSH', 'no: the newest check did not confirm it',
    'ARC computed the SHA-256 of every file, and each one matched (the strongest check)']);
  assert.ok(/weakest check/.test(app.uploadFacts({check_method: 'globus-verify'})[0][1]));
  assert.strictEqual(app.uploadFacts({check_method: 'none'})[0][1], 'no check was made');
  assert.strictEqual(app.uploadFacts({transport: 'local'})[0][1], 'a copy made on the same computer');
  // Values this page does not know are shown as they are, and never looked up in a built-in.
  assert.deepStrictEqual(app.uploadFacts({transport: 'pigeon', checked: 'maybe', check_method: 'constructor'}), [
    ['Sent with', 'pigeon', 'text'], ['Copy on ARC confirmed', 'maybe', 'text'], ['How it was checked', 'constructor', 'text']]);
  // No record, or something that is not a record: nothing to say.
  [null, undefined, '', 'globus', 7, [], ['globus'], {}].forEach(function (value) {
    assert.deepStrictEqual(app.uploadFacts(value), [], JSON.stringify(value));
  });
});

test('how one field of a session is shown', function () {
  assert.strictEqual(app.fieldKind('ended_utc', null), 'empty');
  assert.strictEqual(app.fieldKind('notes', ''), 'empty');
  assert.strictEqual(app.fieldKind('tags', []), 'none');
  assert.strictEqual(app.fieldKind('simulated', false), 'bool');
  assert.strictEqual(app.fieldKind('started_utc', '2026-11-17T03:50:15.281Z'), 'time');
  assert.strictEqual(app.fieldKind('started_utc', 'not a time'), 'text');
  assert.strictEqual(app.fieldKind('started_utc', '2026-02-31T25:61:61.000Z'), 'text');
  assert.strictEqual(app.fieldKind('notes', '2026-11-17T03:50:15.281Z'), 'text');
  assert.strictEqual(app.fieldKind('bytes', 123456), 'bytes');
  assert.strictEqual(app.fieldKind('items', 13), 'text');
  assert.strictEqual(app.fieldKind('sample_ids', ['130', '131']), 'list');
  assert.strictEqual(app.fieldKind('corrections_json', [{run: 'x'}]), 'json');
  assert.strictEqual(app.fieldKind('card_json', {schema: 'pcl-session/1'}), 'json');
  assert.strictEqual(app.fieldKind('storage_path', '/projects/pcl_photonics/raw/x'), 'mono');
  assert.strictEqual(app.fieldKind('operator', 'demo-operator-a'), 'text');
});

// ---- when a request fails ----------------------------------------------------------------------

test('a failed request becomes a plain sentence', function () {
  const viewer = {role: 'viewer', action: 'run SQL'};
  assert.strictEqual(app.explainFailure({kind: 'cancelled'}), 'Cancelled.');
  assert.ok(/not reachable/.test(app.explainFailure({kind: 'network'})));
  assert.ok(/pcldb serve/.test(app.explainFailure({kind: 'network'})));
  assert.ok(/could not read/.test(app.explainFailure({kind: 'badreply', status: 200})));
  assert.strictEqual(app.explainFailure({kind: 'http', status: 401, message: 'ignored'}), 'You are signed out. Sign in again.');
  assert.strictEqual(app.explainFailure({kind: 'signedout'}), 'You are signed out. Sign in again.');
  assert.strictEqual(app.explainFailure({kind: 'http', status: 403}, viewer), 'Your role (viewer) may not run SQL.');
  assert.strictEqual(app.explainFailure({kind: 'http', status: 403, message: 'Expert SQL needs the analyst or admin role.'}, viewer),
    'Your role (viewer) may not run SQL. The backend said: Expert SQL needs the analyst or admin role.');
  assert.strictEqual(app.explainFailure({kind: 'http', status: 403}), 'Your role may not do this.');
  assert.ok(/too large/.test(app.explainFailure({kind: 'http', status: 413})));
  assert.ok(/shorter/.test(app.explainFailure({kind: 'http', status: 413})));
  assert.ok(/Wait a minute/.test(app.explainFailure({kind: 'http', status: 429})));
  assert.ok(/Wait about 37 seconds/.test(app.explainFailure({kind: 'http', status: 429, details: {retry_after_s: 37}})));
  assert.ok(/Wait about 1 second,/.test(app.explainFailure({kind: 'http', status: 429, details: {retry_after_s: 0.2 + 0.8}})));
  ['soon', -5, 0, 1e9, null, {}, NaN].forEach(function (wait) {
    assert.ok(/Wait a minute/.test(app.explainFailure({kind: 'http', status: 429, details: {retry_after_s: wait}})), String(wait));
  });
  assert.ok(/did not answer in time/.test(app.explainFailure({kind: 'timeout'})));
  assert.ok(/pcldb serve/.test(app.explainFailure({kind: 'timeout'})));
  // The page may be opened from another computer, so it never claims where the backend runs.
  assert.ok(!/this computer/.test(app.explainFailure({kind: 'network'}) + app.explainFailure({kind: 'timeout'})));
  assert.ok(/Wait a minute/.test(app.explainFailure({kind: 'http', status: 429, message: 'Twenty questions a minute at most.'})));
  assert.ok(/Twenty questions/.test(app.explainFailure({kind: 'http', status: 429, message: 'Twenty questions a minute at most.'})));
  assert.ok(/not reachable/.test(app.explainFailure({kind: 'http', status: 502})));
  assert.strictEqual(app.explainFailure({kind: 'http', status: 503, message: 'Globus Search could not be reached.'}),
    'The backend could not do this. It said: Globus Search could not be reached.');
  assert.strictEqual(app.explainFailure({kind: 'http', status: 400, message: '  The question must have 1 to 500 characters.  '}),
    'The question must have 1 to 500 characters.');
  assert.strictEqual(app.explainFailure({kind: 'http', status: 404}), 'The backend refused this request (status 404).');
  // A message that is not text is ignored; a very long one is cut.
  assert.strictEqual(app.explainFailure({kind: 'http', status: 400, message: {x: 1}}), 'The backend refused this request (status 400).');
  assert.ok(app.explainFailure({kind: 'http', status: 400, message: 'x'.repeat(9000)}).length < 4100);
  assert.strictEqual(typeof app.explainFailure(null), 'string');
});

// ---- run ---------------------------------------------------------------------------------------

let failures = 0;
tests.forEach(function (item) {
  try {
    item.body();
    console.log('ok      ' + item.name);
  } catch (err) {
    failures += 1;
    console.log('FAILED  ' + item.name);
    console.log(String(err && err.stack ? err.stack : err).split('\n').map(function (line) {
      return '        ' + line;
    }).join('\n'));
  }
});
console.log(tests.length - failures + ' of ' + tests.length + ' passed');
process.exit(failures === 0 ? 0 : 1);
