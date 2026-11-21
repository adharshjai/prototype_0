"""People and permissions (specification section 10).

Who may do what:

    role       ask and search   expert SQL   register   manage
    viewer     yes              no           no         no
    analyst    yes              yes          no         no
    uploader   yes              no           yes        no
    admin      yes              yes          yes        yes

The four capabilities are named `ask`, `sql`, `register` and `manage`.

A token is the text `pcl_` followed by 43 characters. It is shown or saved exactly once, when it
is made. The database keeps only its SHA-256, so nobody who reads the database can sign in with
what is there. Nothing in this module prints, logs or returns a token after `issue_token`.

Every function that takes `db` works inside the caller's transaction (`with store.app() as db`)
and behaves the same on both engines. Times come from Python, never from the database.

The commands `pcldb user ...` and `pcldb token ...` are at the end (`cmd_user`, `cmd_token`).
"""

from __future__ import annotations

import datetime
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

from . import util
from .errors import Conflict, Forbidden, NotFound, PclError, Unauthorized

ROLES = ("viewer", "analyst", "uploader", "admin")
CAPABILITIES = ("ask", "sql", "register", "manage")
ROLE_CAPABILITIES = {
    "viewer": ("ask",),
    "analyst": ("ask", "sql"),
    "uploader": ("ask", "register"),
    "admin": ("ask", "sql", "register", "manage"),
}

TOKEN_PREFIX = "pcl_"
TOKEN_LENGTH = len(TOKEN_PREFIX) + 43
MAX_TOKEN_DAYS = 3650
LAST_USED_EVERY_SECONDS = 60
MAX_DISPLAY_NAME = 200
MAX_LABEL = 100

_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_TOKEN = re.compile(r"pcl_[A-Za-z0-9_-]{43}")
_TOKEN_ID = re.compile(r"[0-9a-f]{32}")

_CAPABILITY_WORDS = {
    "ask": "ask questions and search",
    "sql": "run expert SQL",
    "register": "register sessions",
    "manage": "manage users and settings",
}


# ---------------------------------------------------------------------- the user


@dataclass(frozen=True)
class User:
    """One person (or tool) that may use pcldb."""

    user_id: str
    name: str
    display_name: str | None
    role: str
    disabled: bool
    created_utc: str

    @property
    def capabilities(self):
        """What this user may do, in the fixed order ask, sql, register, manage. A disabled
        user may do nothing."""
        if self.disabled:
            return ()
        return ROLE_CAPABILITIES.get(self.role, ())

    def can(self, capability):
        return capability in self.capabilities

    def public(self):
        """{"name", "display_name", "role"}: what the API shows of a user."""
        return {"name": self.name, "display_name": self.display_name, "role": self.role}


def capabilities(role):
    """The capabilities of a role, in the fixed order. An unknown role has none."""
    return ROLE_CAPABILITIES.get(role, ())


def require(user, capability):
    """Return `user` when it has the capability. `Forbidden` when it has not; `Unauthorized`
    when `user` is None (nobody is signed in)."""
    if capability not in CAPABILITIES:
        raise ValueError("unknown capability %r (known: %s)" % (capability,
                                                               ", ".join(CAPABILITIES)))
    if user is None:
        raise Unauthorized("Sign in first: this needs a token that pcldb accepts.")
    if not user.can(capability):
        if user.disabled:
            raise Forbidden("The user %s is switched off. An admin can switch it on again with: "
                            "pcldb user enable %s" % (user.name, user.name))
        allowed = [role for role in ROLES if capability in ROLE_CAPABILITIES[role]]
        raise Forbidden(
            "The user %s has the role %s, which may not %s. That needs the role %s. An admin "
            "can change it with: pcldb user role %s --role %s"
            % (user.name, user.role, _CAPABILITY_WORDS[capability], " or ".join(allowed),
               user.name, allowed[0]), details={"capability": capability, "role": user.role})
    return user


# ---------------------------------------------------------------------- checking what is given


def _has_control(text):
    return any(char < " " or char == "\x7f" for char in text)


def check_name(name):
    """The user name when it is usable, else a PclError that says the rule."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise PclError(
            "%s is not a usable user name. A user name is 1 to 64 characters: lower-case "
            "letters a to z, digits, and . _ - ; it starts with a letter or a digit. "
            "Example: demo-operator-a" % util._show(name), code="bad_user_name")
    return name


def check_role(role):
    if not isinstance(role, str) or role not in ROLES:
        raise PclError("%s is not a role. The roles are: %s."
                       % (util._show(role), ", ".join(ROLES)), code="bad_role")
    return role


def _check_line(value, what, limit, code):
    """An optional one-line text: None stays None; an empty text becomes None."""
    if value is None:
        return None
    if not isinstance(value, str) or _has_control(value):
        raise PclError("The %s must be one line of plain text." % what, code=code)
    value = value.strip()
    if len(value) > limit:
        raise PclError("The %s is %d characters long; at most %d are allowed."
                       % (what, len(value), limit), code=code)
    return value or None


def _now_text(now):
    return util.now_utc_text() if now is None else util.utc_text(now)


def token_sha256(token):
    """What the database keeps of a token: the SHA-256 of its UTF-8 text, as 64 hex digits."""
    return util.sha256_hex(token.encode("utf-8"))


# ---------------------------------------------------------------------- users


_USER_COLUMNS = "user_id, name, display_name, role, disabled, created_utc"


def _user(row):
    return User(user_id=row["user_id"], name=row["name"], display_name=row["display_name"],
                role=row["role"], disabled=bool(row["disabled"]), created_utc=row["created_utc"])


def find_user(db, name):
    """The user with this name, or None."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return None
    row = db.one("SELECT " + _USER_COLUMNS + " FROM {app}.users WHERE name = ?", [name])
    return _user(row) if row else None


def get_user(db, name):
    """The user with this name; `NotFound` when there is none."""
    user = find_user(db, name)
    if user is None:
        raise NotFound("There is no user named %s. See who exists with: pcldb user list"
                       % util._show(name), code="no_such_user")
    return user


def count_users(db):
    return int(db.scalar("SELECT COUNT(*) FROM {app}.users") or 0)


def list_users(db):
    """Every user, ordered by name."""
    rows = db.query("SELECT " + _USER_COLUMNS + " FROM {app}.users")
    return sorted((_user(row) for row in rows), key=lambda user: user.name)


def create_user(db, name, role, display_name=None, *, now=None):
    """Add a user. `Conflict` when the name is taken."""
    check_name(name)
    check_role(role)
    display_name = _check_line(display_name, "display name", MAX_DISPLAY_NAME, "bad_display_name")
    if find_user(db, name) is not None:
        raise Conflict("A user named %s exists already. To change its role: pcldb user role %s "
                       "--role <role>" % (name, name), code="user_exists")
    db.insert("app.users", {
        "user_id": util.new_id(), "name": name, "display_name": display_name, "role": role,
        "disabled": False, "created_utc": _now_text(now)})
    return get_user(db, name)


def _other_working_admins(db, user):
    rows = db.query("SELECT user_id, disabled FROM {app}.users WHERE role = ?", ["admin"])
    return sum(1 for row in rows if not row["disabled"] and row["user_id"] != user.user_id)


def _keep_one_admin(db, user, doing):
    """Refuse a change that would leave nobody who can manage users."""
    if user.role == "admin" and not user.disabled and _other_working_admins(db, user) == 0:
        raise Conflict(
            "%s is the only admin that is switched on, so %s would leave nobody who can manage "
            "users. Make another admin first: pcldb user add <name> --role admin"
            % (user.name, doing), code="last_admin")


def set_role(db, name, role):
    """Give a user another role. Returns the user as it is now."""
    check_role(role)
    user = get_user(db, name)
    if user.role == role:
        return user
    if role != "admin":
        _keep_one_admin(db, user, "changing its role")
    db.execute("UPDATE {app}.users SET role = ? WHERE user_id = ?", [role, user.user_id])
    return get_user(db, name)


def set_disabled(db, name, disabled=True):
    """Switch a user off (its tokens stop working) or on again. Returns the user as it is now."""
    if not isinstance(disabled, bool):
        raise TypeError("disabled must be True or False")
    user = get_user(db, name)
    if user.disabled == disabled:
        return user
    if disabled:
        _keep_one_admin(db, user, "switching it off")
    db.execute("UPDATE {app}.users SET disabled = ? WHERE user_id = ?", [disabled, user.user_id])
    return get_user(db, name)


# ---------------------------------------------------------------------- tokens


def new_token_text():
    """A new token: `pcl_` and 43 url-safe characters (256 random bits)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _check_days(days):
    if days is None:
        return None
    if isinstance(days, bool) or not isinstance(days, (int, float)) \
            or not days == days or not 0 < days <= MAX_TOKEN_DAYS:
        raise PclError("The number of days a token stays valid must be above 0 and at most %d "
                       "(0.5 is twelve hours). Leave it out for a token that does not expire."
                       % MAX_TOKEN_DAYS, code="bad_days")
    return days


def issue_token_info(db, user_name, *, label=None, days=None, now=None):
    """Make a token for a user and return everything about it, the text included:
    `{"token", "token_id", "user_name", "label", "created_utc", "expires_utc"}`.

    The text exists only in the returned value: hand it to the person once, never log it.
    `days` None means the token does not expire.
    """
    label = _check_line(label, "label", MAX_LABEL, "bad_label")
    days = _check_days(days)
    user = get_user(db, user_name)
    if user.disabled:
        raise Conflict("The user %s is switched off, so a token would not work. Switch it on "
                       "first: pcldb user enable %s" % (user.name, user.name),
                       code="user_disabled")
    created = _now_text(now)
    expires = None
    if days is not None:
        try:
            expires = util.utc_text(util.parse_utc(created) + datetime.timedelta(days=days))
        except OverflowError:
            raise PclError("That many days is past the last year a date can hold.",
                           code="bad_days") from None
    token = new_token_text()
    token_id = util.new_id()
    db.insert("app.tokens", {
        "token_id": token_id, "user_id": user.user_id, "token_sha256": token_sha256(token),
        "label": label, "created_utc": created, "expires_utc": expires, "revoked_utc": None,
        "last_used_utc": None})
    return {"token": token, "token_id": token_id, "user_name": user.name, "label": label,
            "created_utc": created, "expires_utc": expires}


def issue_token(db, user_name, *, label=None, days=None, now=None):
    """Make a token for a user and return its text (`pcl_` + 43 characters). Only its SHA-256 is
    stored, so this is the one moment the text exists."""
    return issue_token_info(db, user_name, label=label, days=days, now=now)["token"]


def _token_state(row, now_text):
    if row["revoked_utc"] is not None:
        return "revoked"
    if row["expires_utc"] is not None and row["expires_utc"] <= now_text:
        return "expired"
    return "active"


def list_tokens(db, user_name=None, *, now=None):
    """The tokens (of one user, or of everyone), oldest first. Never the token text and never
    its hash: `{"token_id", "user_name", "label", "created_utc", "expires_utc", "revoked_utc",
    "last_used_utc", "state"}` with state `active`, `expired` or `revoked`."""
    sql = ("SELECT t.token_id, u.name AS user_name, t.label, t.created_utc, t.expires_utc, "
           "t.revoked_utc, t.last_used_utc FROM {app}.tokens t "
           "JOIN {app}.users u ON u.user_id = t.user_id")
    params = []
    if user_name is not None:
        sql += " WHERE t.user_id = ?"
        params.append(get_user(db, user_name).user_id)
    now_text = _now_text(now)
    rows = db.query(sql, params)
    for row in rows:
        row["state"] = _token_state(row, now_text)
    return sorted(rows, key=lambda row: (row["created_utc"], row["token_id"]))


def revoke_token(db, token_id, *, now=None):
    """Stop a token from working, for good. True when it was revoked now, False when it was
    revoked before. `NotFound` when there is no token with this id."""
    if not isinstance(token_id, str) or not _TOKEN_ID.fullmatch(token_id):
        raise NotFound("%s is not a token id. A token id is 32 characters (0-9, a-f); see them "
                       "with: pcldb token list" % util._show(token_id), code="no_such_token")
    row = db.one("SELECT token_id, revoked_utc FROM {app}.tokens WHERE token_id = ?", [token_id])
    if row is None:
        raise NotFound("There is no token with the id %s. See the ids with: pcldb token list"
                       % token_id, code="no_such_token")
    if row["revoked_utc"] is not None:
        return False
    db.execute("UPDATE {app}.tokens SET revoked_utc = ? WHERE token_id = ?",
               [db.param("utc", _now_text(now)), token_id])
    return True


def _lookup(db, token, now):
    """(user, token_id, the time to write as last used or None) for a token text; (None, None,
    None) when the token is not accepted."""
    nobody = (None, None, None)
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return nobody
    row = db.one(
        "SELECT t.token_id, t.expires_utc, t.revoked_utc, t.last_used_utc, u.user_id, u.name, "
        "u.display_name, u.role, u.disabled, u.created_utc FROM {app}.tokens t "
        "JOIN {app}.users u ON u.user_id = t.user_id WHERE t.token_sha256 = ?",
        [token_sha256(token)])
    if row is None or row["revoked_utc"] is not None or row["disabled"] \
            or row["role"] not in ROLES:
        return nobody
    now_text = _now_text(now)
    if row["expires_utc"] is not None and row["expires_utc"] <= now_text:
        return nobody
    touch = now_text
    if row["last_used_utc"] is not None:
        since = util.parse_utc(now_text) - util.parse_utc(row["last_used_utc"])
        if since.total_seconds() < LAST_USED_EVERY_SECONDS:
            touch = None        # written less than a minute ago (or the clock went back)
    return _user(row), row["token_id"], touch


def _write_last_used(db, token_id, now_text):
    db.execute("UPDATE {app}.tokens SET last_used_utc = ? WHERE token_id = ?",
               [db.param("utc", now_text), token_id])


def authenticate(db, token, *, now=None):
    """The user a token belongs to, or None: an unknown, revoked or expired token and a token of
    a user that is switched off all give None (the caller cannot tell which, on purpose).

    Also notes when the token was last used: at most once a minute per token, and a failure to
    write that is ignored. A server should call `authenticate_store`, which writes the note in
    a transaction of its own so that a failed note can never undo anything else.
    """
    user, token_id, touch = _lookup(db, token, now)
    if user is not None and touch is not None:
        try:
            _write_last_used(db, token_id, touch)
        except Exception:
            pass    # "last used" is a courtesy; signing in must not depend on it
    return user


def authenticate_store(store, token, *, now=None):
    """`authenticate` for code that has a store and no open transaction: one short transaction
    to look the token up, and a second one, whose failure is ignored, for "last used"."""
    with store.app() as db:
        user, token_id, touch = _lookup(db, token, now)
    if user is not None and touch is not None:
        try:
            with store.app_only() as db:
                _write_last_used(db, token_id, touch)
        except Exception:
            pass
    return user


# ---------------------------------------------------------------------- token files


TOKEN_FOLDER = "tokens"


def save_token_file(path, token):
    """Write a token as the one line of a private file (mode 0600 where the system has modes)."""
    util.write_text_atomic(path, token + "\n", mode=0o600)
    return Path(path)


def token_file_path(settings, info):
    """Where `pcldb token new` saves a token: var/tokens/<user>_<first 8 of the id>.txt."""
    return settings.var_dir / TOKEN_FOLDER / ("%s_%s.txt" % (info["user_name"],
                                                            info["token_id"][:8]))


# ---------------------------------------------------------------------- the command line


def _plain(text):
    """Text for the console: ASCII only, no control characters."""
    text = "".join(char if " " <= char != "\x7f" else " " for char in str(text))
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _say(text="", stream=None):
    print(_plain(text), file=stream or sys.stdout)


def _table(header, rows):
    """Lines of an aligned table: the header, then the rows."""
    cells = [header] + [["-" if cell is None or cell == "" else _plain(cell) for cell in row]
                        for row in rows]
    widths = [max(len(line[index]) for line in cells) for index in range(len(header))]
    return ["  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip()
            for line in cells]


def _open(settings):
    from .store import open_store
    return open_store(settings)


def _needs(value, sentence):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise PclError(sentence, code="usage")
    return value


def _user_line(user):
    return "%s (role %s, %s)" % (user.name, user.role,
                                 "switched off" if user.disabled else "switched on")


def cmd_user(args, settings):
    """`pcldb user add|list|role|disable|enable [NAME] [--role R] [--display-name TEXT]`."""
    action = getattr(args, "action", None)
    name = getattr(args, "name", None)
    role = getattr(args, "role", None)
    display_name = getattr(args, "display_name", None)
    store = _open(settings)

    if action == "list":
        with store.app() as db:
            users = [get_user(db, name)] if name else list_users(db)
        if not users:
            _say("There is no user yet. Run: pcldb setup")
            return 0
        for line in _table(
                ["NAME", "ROLE", "STATE", "DISPLAY NAME", "MADE (UTC)"],
                [[user.name, user.role, "off" if user.disabled else "on", user.display_name,
                  user.created_utc] for user in users]):
            _say(line)
        return 0

    if action == "add":
        _needs(name, "Give the name of the new user: pcldb user add <name> --role <role>")
        role = role or "viewer"
        with store.app() as db:
            user = create_user(db, name, role, display_name)
        _say("Added the user %s." % _user_line(user))
        _say("It may: %s." % ", ".join(_CAPABILITY_WORDS[item] for item in user.capabilities))
        _say("Make a token for it with: pcldb token new --user %s" % user.name)
        return 0

    if action == "role":
        _needs(name, "Give the user's name: pcldb user role <name> --role <role>")
        _needs(role, "Say which role %s gets: pcldb user role %s --role <role>   The roles "
                     "are: %s." % (name, name, ", ".join(ROLES)))
        with store.app() as db:
            before = get_user(db, name)
            user = set_role(db, name, role)
        if before.role == user.role:
            _say("The user %s has the role %s already. Nothing was changed."
                 % (user.name, user.role))
        else:
            _say("The user %s now has the role %s (it was %s)." % (user.name, user.role,
                                                                   before.role))
        return 0

    if action in ("disable", "enable"):
        _needs(name, "Give the user's name: pcldb user %s <name>" % action)
        off = action == "disable"
        with store.app() as db:
            before = get_user(db, name)
            user = set_disabled(db, name, off)
        if before.disabled == user.disabled:
            _say("The user %s is switched %s already. Nothing was changed."
                 % (user.name, "off" if off else "on"))
        elif off:
            _say("The user %s is switched off. Its tokens do not work until it is switched on "
                 "again: pcldb user enable %s" % (user.name, user.name))
        else:
            _say("The user %s is switched on again. Its tokens work again." % user.name)
        return 0

    raise PclError("pcldb user does not know the action %r. The actions are: add, list, role, "
                   "disable, enable." % (action,), code="usage")


def cmd_token(args, settings):
    """`pcldb token new|list|revoke [--user NAME] [--label TEXT] [--days N] [--id ID]`.

    `new` never prints the token: it saves it into a private file and says where.
    """
    action = getattr(args, "action", None)
    user_name = getattr(args, "user", None)
    store = _open(settings)

    if action == "new":
        _needs(user_name, "Say whose token this is: pcldb token new --user <name>")
        path = None
        try:
            with store.app() as db:
                info = issue_token_info(db, user_name, label=getattr(args, "label", None),
                                        days=getattr(args, "days", None))
                path = save_token_file(token_file_path(settings, info), info["token"])
        except BaseException:
            if path is not None:        # the token was not committed: its file is worth nothing
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        _say("Made a token for the user %s (token id %s)." % (info["user_name"],
                                                              info["token_id"]))
        _say("It is saved in: %s" % path)
        _say("The token is not shown here. Hand that file's one line to the person or tool "
             "that needs it (PCLDB_TOKEN, or --token-file), then delete the file.")
        if info["expires_utc"]:
            _say("It stops working at %s (UTC)." % info["expires_utc"])
        else:
            _say("It does not expire. To stop it: pcldb token revoke --id %s"
                 % info["token_id"])
        return 0

    if action == "list":
        with store.app() as db:
            tokens = list_tokens(db, user_name or None)
        if not tokens:
            _say("There is no token%s." % (" of the user %s" % user_name if user_name else ""))
            return 0
        for line in _table(
                ["TOKEN ID", "USER", "STATE", "LABEL", "MADE (UTC)", "EXPIRES (UTC)",
                 "LAST USED (UTC)"],
                [[row["token_id"], row["user_name"], row["state"], row["label"],
                  row["created_utc"], row["expires_utc"] or "never", row["last_used_utc"]]
                 for row in tokens]):
            _say(line)
        return 0

    if action == "revoke":
        token_id = _needs(getattr(args, "token_id", None),
                          "Say which token: pcldb token revoke --id <token id>   See the ids "
                          "with: pcldb token list")
        with store.app() as db:
            done = revoke_token(db, token_id.strip().lower())
        if done:
            _say("The token %s is revoked. It does not work any more." % token_id.strip().lower())
        else:
            _say("The token %s was revoked before. Nothing was changed."
                 % token_id.strip().lower())
        return 0

    raise PclError("pcldb token does not know the action %r. The actions are: new, list, "
                   "revoke." % (action,), code="usage")
