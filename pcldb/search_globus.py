"""Globus Search, the real service: sign-in, the index, sending entries, searching.

Globus Search never reads files on ARC. It is a hosted index of the small entries pcldb sends
(`search_sync.push`), and it answers the same requests the stand-in answers (`search_local`).
This module is the only place that touches the Globus SDK (`globus-sdk` 4), and it imports it
only when something here is really used: the rest of pcldb runs without the package.

How a person signs in: `GlobusSearch.login()` opens the browser (or, with `no_browser`, prints
an address and reads a code). The SDK keeps the tokens in the per-user folder, in
`globus_tokens.json`, and renews them by itself. They are never printed, logged or put into a
message here. Everything is asked for at the first sign-in (the Search scope `all`), so a later
`push` or `create-index` needs no second one.

Rules of this module:

- One lock around every SDK call: the SDK's app object is not safe to use from two threads.
- `signed_in()` never starts a sign-in and never uses the network.
- Every SDK or network failure becomes a `PclError` in plain words with the next step. No
  message holds a token, a header or an address with a secret in it. A request the service
  refuses (HTTP 400) keeps the service's own code and message.
- Entries are never public: `ingest` refuses `public` and `all_authenticated_users`.

Tests replace the SDK with `tests/fake_globus.py` (the `sdk=` argument), so no test signs in or
uses the network.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

from . import search as _search
from . import util
from .errors import Forbidden, NotFound, PclError, TooLarge, Unavailable

# The Globus SDK's tutorial client: a public "native app" id that needs no secret. The Raman
# controller edition uses the same one. The setting search.client_id replaces it.
DEFAULT_CLIENT_ID = "61338d24-54d5-408f-a10d-66c06b59f6d2"
APP_NAME = "pcldb"
IDENTITY_PREFIX = "urn:globus:auth:identity:"
GROUP_PREFIX = "urn:globus:groups:id:"
NEVER_VISIBLE_TO = ("public", "all_authenticated_users")

MAX_INGEST_BYTES = 9_000_000        # the service takes 10 MB per request; stay below it
MAX_SUBJECT_BYTES = 500
MAX_VISIBLE_TO = 100
MAX_NAME_CHARS = 128
MAX_DESCRIPTION_CHARS = 1000

HTTP_TIMEOUT_SECONDS = 30
HTTP_RETRIES = 2
PLAIN_TIMEOUT_SECONDS = 10          # for the index facts `pcldb search status` shows

LOGIN_STEP = "Run: pcldb search login"
EXPIRED = "Your Globus sign-in has expired. " + LOGIN_STEP
INSTALL_STEP = ("Install it with: %s --globus (or: pip install -r "
                "requirements-globus.txt)" % util.INSTALL_PY)

FINISHED_STATES = ("SUCCESS", "FAILED", "CANCELLED")


def load_sdk():
    """The names this module uses from the installed Globus SDK, gathered in one place.

    `tests/fake_globus.py` offers an object with the same attributes.
    """
    try:
        import globus_sdk
        from globus_sdk import GlobusAppConfig, SearchClient, UserApp
        from globus_sdk.login_flows import CommandLineLoginFlowEOFError, LocalServerLoginError
        from globus_sdk.scopes import SearchScopes
        from globus_sdk.token_storage import JSONTokenStorage, TokenValidationError
    except ImportError:
        raise Unavailable("The Globus SDK (the Python package globus-sdk, version 4 or newer) "
                          "is not installed. " + INSTALL_STEP, code="globus_sdk_missing") from None
    return SimpleNamespace(
        UserApp=UserApp,
        GlobusAppConfig=GlobusAppConfig,
        SearchClient=SearchClient,
        JSONTokenStorage=JSONTokenStorage,
        search_scope=SearchScopes.all,
        ApiError=globus_sdk.GlobusAPIError,
        AuthApiError=globus_sdk.AuthAPIError,
        NetworkError=globus_sdk.NetworkError,
        TokenError=TokenValidationError,
        LoginError=(LocalServerLoginError, CommandLineLoginFlowEOFError),
        SdkError=globus_sdk.GlobusError,
    )


def _short(value, limit=400):
    """One line of text for a message, cut to a sensible length."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _data(response):
    """The JSON body of an SDK response as a plain dict."""
    data = getattr(response, "data", response)
    return dict(data) if isinstance(data, dict) else {}


class GlobusSearch:
    """One person's connection to one Globus Search index.

    Built from the settings: `search.client_id` (the native app id), `search.index_id` and the
    per-user token file. Nothing is imported or opened until a method needs it.
    """

    def __init__(self, settings, *, sdk=None):
        self._settings = settings
        self._sdk = sdk
        self._lock = threading.RLock()
        self._app = None
        self._client = None
        self._storage = None
        self._flow = None
        self._plain_client = None        # without a sign-in: for what Globus tells everybody

    # ------------------------------------------------------------------ what it is set up with

    @property
    def index_id(self):
        """The id of the index in the settings, or None when none is set yet."""
        return self._settings.search.index_id or None

    @property
    def client_id(self):
        return getattr(self._settings.search, "client_id", None) or DEFAULT_CLIENT_ID

    @property
    def token_path(self):
        """U/globus_tokens.json: where the SDK keeps this person's sign-in."""
        return Path(self._settings.globus_tokens_path)

    def _need_index(self):
        if not self.index_id:
            raise PclError("No Globus Search index is set yet (the setting search.index_id is "
                           "empty). Run: pcldb search create-index", code="no_search_index",
                           status=409)
        return self.index_id

    # ------------------------------------------------------------------ the SDK objects

    def _names(self):
        if self._sdk is None:
            self._sdk = load_sdk()
        return self._sdk

    def _open(self, flow=None):
        """Build the SDK's app and client once (again when another sign-in way is asked for).

        Building them uses no network and starts no sign-in.
        """
        sdk = self._names()
        if self._app is not None and (flow is None or flow == self._flow):
            return
        self._close()
        flow = flow or "local-server"
        storage = sdk.JSONTokenStorage(self.token_path)
        config = sdk.GlobusAppConfig(
            request_refresh_tokens=True,
            login_flow_manager=flow,
            token_storage=storage,
            token_validation_error_handler=None,     # never start a sign-in inside a request
        )
        app = sdk.UserApp(APP_NAME, client_id=self.client_id, config=config)
        try:
            client = sdk.SearchClient(app=app, app_scopes=[sdk.search_scope])
        except BaseException:
            app.close()
            raise
        self._tune(client)
        self._app, self._client, self._storage, self._flow = app, client, storage, flow

    @staticmethod
    def _tune(client, retries=None, timeout=None):
        """Shorter patience than the SDK's own (5 retries, 60 s): a person is waiting."""
        retry_config = getattr(client, "retry_config", None)
        if retry_config is not None and hasattr(retry_config, "max_retries"):
            retry_config.max_retries = HTTP_RETRIES if retries is None else retries
        transport = getattr(client, "transport", None)
        if transport is not None and hasattr(transport, "http_timeout"):
            transport.http_timeout = HTTP_TIMEOUT_SECONDS if timeout is None else timeout

    def _close(self):
        for thing in (self._client, self._app, self._storage, self._plain_client):
            close = getattr(thing, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:       # closing is best effort; nothing to tell anybody
                    pass
        self._app = self._client = self._storage = self._flow = self._plain_client = None

    def close(self):
        """Give back what the SDK holds open (network sessions). The sign-in stays."""
        with self._lock:
            self._close()

    @contextlib.contextmanager
    def _errors(self, what):
        """Turn every SDK failure inside the block into a PclError in plain words."""
        sdk = self._names()
        try:
            yield
        except PclError:
            raise
        except sdk.TokenError:
            raise Unavailable("You are not signed in to Globus, or the sign-in has run out. "
                              + LOGIN_STEP, code="globus_sign_in") from None
        except sdk.AuthApiError:
            raise Unavailable(EXPIRED, code="globus_sign_in") from None
        except sdk.ApiError as error:
            raise self._api_error(what, error) from None
        except sdk.NetworkError:
            raise Unavailable("Globus could not be reached while %s. Check the network "
                              "connection and try again." % what,
                              code="globus_unreachable") from None
        except sdk.LoginError:
            raise PclError("The sign-in to Globus did not finish. If this computer cannot "
                           "open a browser (for example over SSH), run: pcldb search login "
                           "--no-browser", code="globus_sign_in", status=503) from None
        except sdk.SdkError as error:
            raise PclError("The Globus SDK reported a problem while %s (%s)."
                           % (what, type(error).__name__), code="globus_error",
                           status=502) from None
        except (OSError, ValueError) as error:
            raise PclError(
                "The saved Globus sign-in (%s) could not be read or written while %s (%s). "
                "Run: pcldb search logout, then: pcldb search login"
                % (self.token_path.name, what, type(error).__name__),
                code="globus_tokens", status=503) from None

    def _guard(self, what, action, flow=None):
        """Run `action(client, app)` under the lock, as the signed-in person."""
        with self._lock, self._errors(what):
            self._open(flow)
            return action(self._client, self._app)

    def _guard_plain(self, what, action):
        """Run `action(client)` under the lock with a client that carries no sign-in at all:
        only for what Globus Search tells everybody (the facts about an index)."""
        with self._lock, self._errors(what):
            if self._plain_client is None:
                client = self._names().SearchClient()
                self._tune(client, retries=0, timeout=PLAIN_TIMEOUT_SECONDS)
                self._plain_client = client
            return action(self._plain_client)

    @staticmethod
    def _api_error(what, error):
        """A PclError for an HTTP error answer of the service. Only the service's own code and
        message are passed on; never a header or the request."""
        status = getattr(error, "http_status", None)
        code = getattr(error, "code", None)
        code = code if isinstance(code, str) and code else None
        message = _short(getattr(error, "message", None) or "no message")
        shown = "%s (%s)" % (message, code) if code else message
        if status == 400:
            details = {"error_data": getattr(error, "error_data", None)}
            try:
                json.dumps(details)
            except (TypeError, ValueError):
                details = None
            return PclError("Globus Search did not accept the request while %s: %s"
                            % (what, shown), code=code or "globus_bad_request", status=400,
                            details=details)
        if status == 401:
            # The service never falls back to "nobody is signed in": a token it does not
            # accept is an error, also for things everybody may read.
            return Unavailable(EXPIRED, code="globus_sign_in")
        if status == 403:
            return Forbidden("Globus Search says this Globus account may not do that (%s; while "
                             "%s). The owner of the index can give the account the writer role. "
                             "If the sign-in is old, run: pcldb search login" % (shown, what),
                             code=code or "globus_forbidden")
        if status == 404:
            return NotFound("Globus Search does not know what was asked for while %s: %s. A "
                            "trial index is deleted after some weeks; make a new one with: "
                            "pcldb search create-index" % (what, shown),
                            code=code or "globus_not_found")
        if status == 429 or (isinstance(status, int) and status >= 500):
            return Unavailable("Globus Search is busy or has a problem right now (HTTP %s while "
                               "%s). Try again in a few minutes." % (status, what),
                               code="globus_unavailable")
        return PclError("Globus Search answered with an error while %s: HTTP %s, %s"
                        % (what, status, shown), code=code or "globus_error", status=502)

    # ------------------------------------------------------------------ signing in and out

    def signed_in(self):
        """True when a usable sign-in is saved. Never starts a sign-in, never uses the network,
        never raises."""
        try:
            if not self.token_path.is_file():
                return False
            with self._lock:
                self._open()
                return not self._app.login_required()
        except Exception:               # any doubt means: not signed in
            return False

    def login(self, no_browser=False):
        """Sign in to Globus (once; later calls find the saved sign-in). Returns the identity
        address `urn:globus:auth:identity:<id>` of the person who signed in.

        Opens the browser; with `no_browser` it prints an address and asks for the code the
        page shows (for a computer reached over SSH).
        """
        folder = self.token_path.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(folder, 0o700)
        except OSError as error:
            raise PclError("The folder for the Globus sign-in could not be made: %s (%s)."
                           % (folder, error.strerror or type(error).__name__),
                           code="globus_tokens", status=503) from None
        flow = "command-line" if no_browser else "local-server"
        self._guard("signing in", lambda client, app: app.login(), flow=flow)
        if os.name != "nt":
            try:
                os.chmod(self.token_path, 0o600)
            except OSError:
                pass
        return self.identity_urn()

    def logout(self):
        """Sign out: cancel the tokens at Globus and remove them from this computer.

        Returns True when Globus confirmed, False when it could not be reached (the tokens are
        removed from this computer all the same) or when nobody was signed in.
        """
        if not self.token_path.is_file():
            return False
        confirmed = True
        try:
            self._guard("signing out", lambda client, app: app.logout())
        except PclError:
            confirmed = False
        self.close()
        try:
            self.token_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise PclError("The saved Globus sign-in could not be removed: %s (%s). Delete the "
                           "file by hand." % (self.token_path, error.strerror
                                              or type(error).__name__),
                           code="globus_tokens", status=503) from None
        return confirmed

    def identity_urn(self):
        """`urn:globus:auth:identity:<id>` of the signed-in person. Unavailable when nobody is."""
        def read(client, app):
            return getattr(app.token_storage, "identity_id", None)

        identity = self._guard("reading the sign-in", read) if self.token_path.is_file() else None
        if not identity:
            raise Unavailable("You are not signed in to Globus. " + LOGIN_STEP,
                              code="globus_sign_in")
        return IDENTITY_PREFIX + str(identity)

    # ------------------------------------------------------------------ the index

    def create_index(self, display_name, description):
        """Make a new (trial) index and return its id. Anyone who knows an index id can read
        its name and description, so keep both bland."""
        for what, value, most in (("name", display_name, MAX_NAME_CHARS),
                                  ("description", description, MAX_DESCRIPTION_CHARS)):
            if not isinstance(value, str) or not value.strip() or len(value) > most:
                raise PclError("The %s of an index must be text of 1 to %d characters."
                               % (what, most), code="bad_index_text")
        index = _data(self._guard("making the index", lambda client, app: client.create_index(
            display_name.strip(), description.strip())))
        index_id = index.get("id")
        if not isinstance(index_id, str) or not index_id:
            raise PclError("Globus Search made no index id known. Look at "
                           "https://app.globus.org or try again.", code="globus_error",
                           status=502)
        return index_id

    def index_info(self):
        """What Globus Search says about the index: `display_name`, `is_trial`, `status`,
        `max_size_in_mb`, `size_in_mb`, `num_subjects`, `num_entries` ... Globus tells this to
        anybody who knows the index id, so it also works when nobody is signed in (then
        without `permissions`)."""
        index_id = self._need_index()
        if self.signed_in():
            return _data(self._guard("reading the index",
                                     lambda client, app: client.get_index(index_id)))
        return _data(self._guard_plain("reading the index",
                                       lambda client: client.get_index(index_id)))

    def ingest(self, entries):
        """Send entries (each exactly `{subject, visible_to, content}`) as one batch. Returns
        the id of the task the service queued; the entries can be found once it succeeded."""
        index_id = self._need_index()
        if not isinstance(entries, (list, tuple)) or not entries:
            raise PclError("There is nothing to send: the list of entries is empty.",
                           code="bad_search_entry")
        gmeta = [self._checked_entry(entry, position) for position, entry in enumerate(entries)]
        document = {"ingest_type": "GMetaList", "ingest_data": {"gmeta": gmeta}}
        size = len(json.dumps(document, ensure_ascii=False).encode("utf-8"))
        if size > MAX_INGEST_BYTES:
            raise TooLarge("This batch of %d entries is %d bytes; Globus Search takes at most "
                           "10 MB at a time. Send fewer entries per batch."
                           % (len(gmeta), size), code="ingest_too_large")
        answer = _data(self._guard("sending entries",
                                   lambda client, app: client.ingest(index_id, document)))
        return self._task_id(answer)

    @staticmethod
    def _checked_entry(entry, position):
        where = "entry %d" % (position + 1)
        if not isinstance(entry, dict) or set(entry) != {"subject", "visible_to", "content"}:
            raise PclError("%s must have exactly the keys subject, visible_to and content."
                           % where, code="bad_search_entry")
        subject, visible_to, content = entry["subject"], entry["visible_to"], entry["content"]
        if (not isinstance(subject, str) or not subject
                or len(subject.encode("utf-8", "replace")) > MAX_SUBJECT_BYTES):
            raise PclError("%s: the subject must be text of 1 to %d bytes."
                           % (where, MAX_SUBJECT_BYTES), code="bad_search_entry")
        if not isinstance(visible_to, (list, tuple)) or not 1 <= len(visible_to) <= MAX_VISIBLE_TO:
            raise PclError("%s: visible_to must name 1 to %d Globus identities or groups."
                           % (where, MAX_VISIBLE_TO), code="bad_search_entry")
        for value in visible_to:
            if isinstance(value, str) and value.strip().lower() in NEVER_VISIBLE_TO:
                raise PclError("%s: entries are never public, so visible_to must not hold %s."
                               % (where, value.strip()), code="bad_visible_to")
            if not isinstance(value, str) or not value.startswith((IDENTITY_PREFIX, GROUP_PREFIX)):
                raise PclError("%s: visible_to holds something that is neither a Globus "
                               "identity (%s<id>) nor a Globus group (%s<id>)."
                               % (where, IDENTITY_PREFIX, GROUP_PREFIX), code="bad_visible_to")
        if not isinstance(content, dict) or not content:
            raise PclError("%s: the content must be a JSON object that is not empty." % where,
                           code="bad_search_entry")
        return {"subject": subject, "visible_to": list(visible_to), "content": content}

    @staticmethod
    def _task_id(answer):
        task_id = answer.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise PclError("Globus Search took the request but named no task to follow. Run: "
                           "pcldb search push again in a minute.", code="globus_error",
                           status=502)
        return task_id

    def task(self, task_id):
        """Where a queued task stands: `{"state", "message"}`. The states are CREATING, PENDING,
        PROGRESS (not finished), SUCCESS, FAILED and CANCELLED (finished)."""
        answer = _data(self._guard("asking about a task",
                                   lambda client, app: client.get_task(task_id)))
        state = answer.get("state")
        message = answer.get("message")
        details = answer.get("additional_details")
        parts = []
        if isinstance(message, str) and message.strip():
            parts.append(_short(message, 1000))
        if details and state != "SUCCESS":
            try:
                parts.append(_short(json.dumps(details, ensure_ascii=True, sort_keys=True), 1000))
            except (TypeError, ValueError):
                pass
        return {"state": state if isinstance(state, str) else "UNKNOWN",
                "message": " ".join(parts) or None}

    def delete_subject(self, subject):
        """Ask the service to remove one subject. Returns the id of the queued task (removing
        something that is not there also ends in SUCCESS)."""
        index_id = self._need_index()
        answer = _data(self._guard("removing an entry",
                                   lambda client, app: client.delete_subject(index_id, subject)))
        return self._task_id(answer)

    def search(self, request):
        """Send a search request (checked by `search.validate_request` first) and return the
        service's result as a plain dict."""
        request = _search.validate_request(request)
        index_id = self._need_index()
        result = _data(self._guard("searching",
                                   lambda client, app: client.post_search(index_id, request)))
        if not isinstance(result.get("gmeta"), list):
            raise Unavailable("Globus Search answered with something that is not a search "
                              "result. Try again.", code="globus_unavailable")
        return result


# ---------------------------------------------------------------------- one per settings

_shared = {}
_shared_lock = threading.Lock()


def shared(settings):
    """The one GlobusSearch of this process for these settings (the SDK's app object must not be
    used by two threads at once, so everybody shares one and its lock)."""
    made = GlobusSearch(settings)
    key = (str(made.token_path), made.client_id, made.index_id)
    with _shared_lock:
        if key not in _shared:
            _shared[key] = made
        return _shared[key]
