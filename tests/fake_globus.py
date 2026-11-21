"""A stand-in for the Globus SDK, for tests: no network, no sign-in, no real tokens.

    fake = FakeGlobus()
    backend = GlobusSearch(settings, sdk=fake.sdk)

`fake.sdk` has the same names `pcldb.search_globus.load_sdk()` returns (UserApp,
GlobusAppConfig, SearchClient, JSONTokenStorage, the error classes), so the real `GlobusSearch`
runs on top of it unchanged. The fake keeps one small "service" in memory:

- `fake.calls`: every call in order, as `(name, argument, ...)` tuples.
- Sign-in: `FakeGlobus(signed_in=False)` starts signed out; `app.login()` signs in and writes a
  small token file (made-up text, never a real token) where the real SDK would.
- Indexes: `fake.indexes[index_id]["entries"]` maps subject -> entry once a task succeeded.
  `fake.add_index()` makes one without going through `create_index`.
- Tasks: every ingest or delete makes a task that plays its states one step per `get_task`
  call: CREATING -> PENDING -> PROGRESS -> SUCCESS by default. Change what the NEXT tasks play
  with `fake.next_tasks(["PROGRESS", "FAILED"], message="...")`, or call `fake.stall()` so
  that tasks never finish. Entries appear in the index when their task reaches SUCCESS.
  As the real service does, the index fixes a field's type at first sight: an ingest that
  brings another type for a known field ends FAILED with a message.
- Failures: `fake.fail("ingest", fake.api_error(400, "BadRequest.ValidationError", "..."))`
  makes the next call of that method raise; also `fake.network_error()`, `fake.token_error()`,
  `fake.auth_error()`. Pass `times=3` to fail more than once, `after=1` to let one call
  through first. `fake.finish(task_id)` lets a stalled task end.
- `fake.on_call = function` is called with the method name at the start of every call (for
  the test that proves the one lock).
- `post_search` answers with the stand-in's own engine over the index's entries, so facade
  tests get real results; `fake.search_result` replaces the answer when set.

The method names and argument names of FakeSearchClient are checked against the installed SDK
by `tests/test_search_globus.py`, so the fake cannot drift from the real client unnoticed.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

DEFAULT_PLAN = ("CREATING", "PENDING", "PROGRESS", "SUCCESS")
FAKE_IDENTITY = "00000000-0000-4000-8000-00000000f00d"       # made up; nobody's identity
_PRINCIPAL = re.compile(r"(public|all_authenticated_users|urn:globus:auth:identity:[0-9a-f-]{36}"
                        r"|urn:globus:groups:id:[0-9a-f-]{36})")


class FakeGlobusError(Exception):
    """Stands for globus_sdk.GlobusError."""


class FakeApiError(FakeGlobusError):
    """Stands for globus_sdk.SearchAPIError: an HTTP error answer of the service."""

    def __init__(self, http_status, code, message, error_data=None):
        super().__init__("%s %s %s" % (http_status, code, message))
        self.http_status = http_status
        self.code = code
        self.message = message
        self.error_data = error_data
        # What a real error object also carries and what must never reach a message:
        self.headers = {"Authorization": "Bearer FAKE-SECRET-ACCESS-TOKEN"}
        self.text = "raw body with FAKE-SECRET-ACCESS-TOKEN"


class FakeAuthApiError(FakeApiError):
    """Stands for globus_sdk.AuthAPIError (for example a refresh token that was cancelled)."""


class FakeNetworkError(FakeGlobusError):
    """Stands for globus_sdk.NetworkError."""


class FakeTokenError(Exception):
    """Stands for globus_sdk.token_storage.TokenValidationError."""


class FakeLoginError(Exception):
    """Stands for the SDK's errors of a sign-in that could not run."""


class FakeResponse:
    """Stands for GlobusHTTPResponse: `.data`, item access, `.http_status`."""

    def __init__(self, data, http_status=200):
        self.data = data
        self.http_status = http_status

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)


def _kind(value):
    """The type the service would fix for a value at first sight."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        kinds = {_kind(item) for item in value}
        return kinds.pop() if len(kinds) == 1 else "mixed"
    if isinstance(value, dict):
        return "object"
    return "null"


class FakeGlobus:
    """The whole fake: one made-up Globus account, its indexes and tasks."""

    def __init__(self, *, signed_in=True, identity_id=FAKE_IDENTITY):
        self.calls = []
        self.signed_in = signed_in
        self.identity_id = identity_id
        self.indexes = {}
        self.tasks = {}
        self.logins = []                 # the sign-in way of every sign-in that ran
        self.search_result = None
        self.on_call = None              # called with the method name at the start of each call
        self._plans = []                 # plans waiting for the next tasks
        self._default_plan = (DEFAULT_PLAN, None)
        self._failures = {}              # method name -> [exception, ...]
        self.sdk = SimpleNamespace(
            UserApp=self._make(FakeUserApp),
            GlobusAppConfig=FakeAppConfig,
            SearchClient=self._make(FakeSearchClient),
            JSONTokenStorage=FakeTokenStorage,
            search_scope="urn:globus:auth:scope:search.api.globus.org:all",
            ApiError=FakeApiError,
            AuthApiError=FakeAuthApiError,
            NetworkError=FakeNetworkError,
            TokenError=FakeTokenError,
            LoginError=(FakeLoginError,),
            SdkError=FakeGlobusError,
        )

    def _make(self, cls):
        fake = self

        def build(*args, **kwargs):
            return cls(fake, *args, **kwargs)
        return build

    # ------------------------------------------------------------------ what tests set up

    def add_index(self, index_id=None):
        index_id = index_id or str(uuid.uuid4())
        self.indexes[index_id] = {"display_name": "test", "description": "test", "entries": {},
                                  "types": {}}
        return index_id

    def next_tasks(self, states, message=None, count=1):
        """The next `count` tasks play these states (the last one repeats)."""
        for _ in range(count):
            self._plans.append((tuple(states), message))

    def all_tasks(self, states, message=None):
        """Every later task plays these states."""
        self._default_plan = (tuple(states), message)

    def stall(self):
        """Tasks never finish from now on."""
        self.all_tasks(["CREATING", "PENDING", "PROGRESS"])

    def fail(self, method, error, times=1, after=0):
        """The next `times` calls of `method` raise `error`, after letting `after` calls pass."""
        self._failures.setdefault(method, []).extend([None] * after + [error] * times)

    def finish(self, task_id, state="SUCCESS", message=None):
        """A task that was stalled answers `state` from now on."""
        task = self.tasks[task_id]
        task["states"], task["step"], task["message"] = (state,), -1, message

    @staticmethod
    def api_error(http_status, code, message, error_data=None):
        return FakeApiError(http_status, code, message, error_data)

    @staticmethod
    def auth_error():
        return FakeAuthApiError(400, "invalid_grant", "refresh token is not active")

    @staticmethod
    def network_error():
        return FakeNetworkError("connection failed (https://search.api.globus.org/ "
                                "with FAKE-SECRET-ACCESS-TOKEN)")

    @staticmethod
    def token_error():
        return FakeTokenError("No token data for search.api.globus.org")

    def names(self):
        """The names of the calls so far, in order."""
        return [call[0] for call in self.calls]

    def count(self, name):
        return self.names().count(name)

    # ------------------------------------------------------------------ used by the fake classes

    def _enter(self, name, *args):
        self.calls.append((name,) + args)
        if self.on_call is not None:
            self.on_call(name)
        waiting = self._failures.get(name)
        if waiting:
            error = waiting.pop(0)
            if error is not None:
                raise error

    def _need_sign_in(self):
        if not self.signed_in:
            raise FakeTokenError("No token data for search.api.globus.org")

    def _new_task(self, kind, index_id, payload):
        states, message = self._plans.pop(0) if self._plans else self._default_plan
        task_id = str(uuid.uuid4())
        self.tasks[task_id] = {"task_id": task_id, "task_type": kind, "index_id": index_id,
                               "states": states, "step": -1, "message": message,
                               "payload": payload, "applied": False}
        return task_id

    def _advance(self, task_id):
        task = self.tasks[task_id]
        if task["step"] < len(task["states"]) - 1:
            task["step"] += 1
        state = task["states"][task["step"]]
        message = task["message"]
        if state == "SUCCESS" and not task["applied"]:
            task["applied"] = True
            problem = self._apply(task)
            if problem:
                task["states"] = task["states"][:task["step"]] + ("FAILED",)
                task["message"] = problem
                state, message = "FAILED", problem
        return state, message

    def _apply(self, task):
        index = self.indexes.get(task["index_id"])
        if index is None:
            return "the index is gone"
        if task["task_type"] == "delete-by-subject":
            index["entries"].pop(task["payload"], None)
            return None
        for entry in task["payload"]:
            for name, value in entry["content"].items():
                seen = index["types"].setdefault(name, _kind(value))
                if seen != _kind(value):
                    return ("mapper_parsing_exception: the field content.%s is %s in this index "
                            "and cannot take a value of type %s" % (name, seen, _kind(value)))
        for entry in task["payload"]:
            index["entries"][entry["subject"]] = copy.deepcopy(entry)
        return None


class FakeTokenStorage:
    """Stands for JSONTokenStorage(filepath)."""

    def __init__(self, filepath, *, namespace="DEFAULT"):
        self.filepath = Path(filepath)
        self.namespace = namespace
        self.closed = False

    def close(self):
        self.closed = True


class FakeAppConfig:
    """Stands for GlobusAppConfig: keeps what it was given. Unknown names are an error, as in
    the real dataclass."""

    _KNOWN = ("login_flow_manager", "login_redirect_uri", "token_storage",
              "request_refresh_tokens", "auto_redrive_gares", "token_validation_error_handler",
              "id_token_decoder", "environment")

    def __init__(self, **kwargs):
        for name in kwargs:
            if name not in self._KNOWN:
                raise TypeError("GlobusAppConfig got an unexpected keyword argument %r" % name)
        self.given = dict(kwargs)
        self.login_flow_manager = kwargs.get("login_flow_manager")
        self.token_storage = kwargs.get("token_storage")
        self.request_refresh_tokens = kwargs.get("request_refresh_tokens", False)
        self.token_validation_error_handler = kwargs.get("token_validation_error_handler",
                                                         "default")


class FakeUserApp:
    """Stands for globus_sdk.UserApp(app_name, *, client_id, config)."""

    def __init__(self, fake, app_name="Unnamed Globus App", *, client_id=None, config=None):
        self._fake = fake
        self.app_name = app_name
        self.client_id = client_id
        self.config = config
        self.closed = False
        fake.calls.append(("UserApp", app_name, client_id))

    @property
    def token_storage(self):
        fake = self._fake
        return SimpleNamespace(identity_id=fake.identity_id if fake.signed_in else None)

    def login_required(self):
        self._fake.calls.append(("login_required",))
        return not self._fake.signed_in

    def login(self, *, auth_params=None, force=False):
        fake = self._fake
        fake._enter("login", self.config.login_flow_manager)
        if force or not fake.signed_in:
            fake.logins.append(self.config.login_flow_manager)
            fake.signed_in = True
            storage = self.config.token_storage
            if storage is not None:
                storage.filepath.parent.mkdir(parents=True, exist_ok=True)
                storage.filepath.write_text(
                    json.dumps({"fake": True, "note": "made-up sign-in for tests"}) + "\n",
                    encoding="utf-8")

    def logout(self, *, sweep=False):
        self._fake._enter("logout")
        self._fake.signed_in = False

    def close(self):
        self.closed = True


class FakeSearchClient:
    """Stands for globus_sdk.SearchClient(app=..., app_scopes=[...])."""

    def __init__(self, fake, *, app=None, app_scopes=None):
        self._fake = fake
        self.app = app
        self.app_scopes = app_scopes
        self.closed = False
        fake.calls.append(("SearchClient", tuple(app_scopes or ())))

    def close(self):
        self.closed = True

    def create_index(self, display_name, description):
        fake = self._fake
        fake._enter("create_index", display_name, description)
        fake._need_sign_in()
        if len([1 for index in fake.indexes.values() if index.get("trial", True)]) >= 3:
            raise FakeApiError(409, "Conflict.TrialIndexLimit",
                               "You may own at most 3 trial indices.")
        index_id = fake.add_index()
        fake.indexes[index_id].update(display_name=display_name, description=description)
        return FakeResponse({"id": index_id, "display_name": display_name,
                             "description": description, "is_trial": True, "status": "open"})

    def get_index(self, index_id, *, query_params=None):
        fake = self._fake
        fake._enter("get_index", index_id)
        index = self._index(index_id)
        return FakeResponse({"id": index_id, "display_name": index["display_name"],
                             "description": index["description"], "is_trial": True,
                             "status": "open", "max_size_in_mb": 1, "size_in_mb": 0,
                             "num_subjects": len(index["entries"]),
                             "num_entries": len(index["entries"]), "permissions": ["owner"]})

    def ingest(self, index_id, data):
        fake = self._fake
        fake._enter("ingest", index_id, copy.deepcopy(data))
        fake._need_sign_in()
        self._index(index_id)
        if set(data) - {"ingest_type", "ingest_data", "field_mapping", "@version"}:
            raise self._invalid("ingest document has unknown keys")
        if data.get("ingest_type") != "GMetaList":
            raise self._invalid("ingest_type must be GMetaList here")
        entries = data.get("ingest_data", {}).get("gmeta")
        if not isinstance(entries, list) or not entries:
            raise self._invalid("gmeta must be a list that is not empty")
        for entry in entries:
            if set(entry) - {"subject", "visible_to", "content", "id", "principal_sets",
                             "@version"}:
                raise self._invalid("GMetaEntry has unknown keys")
            visible_to = entry.get("visible_to")
            if (not isinstance(entry.get("subject"), str) or not entry["subject"]
                    or not isinstance(entry.get("content"), dict)
                    or not isinstance(visible_to, list) or not 1 <= len(visible_to) <= 100
                    or not all(isinstance(item, str) and _PRINCIPAL.fullmatch(item)
                               for item in visible_to)):
                raise self._invalid("GMetaEntry is malformed")
        task_id = fake._new_task("ingest", index_id, copy.deepcopy(entries))
        return FakeResponse({"acknowledged": True, "task_id": task_id})

    def delete_subject(self, index_id, subject, *, query_params=None):
        fake = self._fake
        fake._enter("delete_subject", index_id, subject)
        fake._need_sign_in()
        self._index(index_id)
        return FakeResponse({"task_id": fake._new_task("delete-by-subject", index_id, subject)})

    def get_task(self, task_id, *, query_params=None):
        fake = self._fake
        fake._enter("get_task", task_id)
        fake._need_sign_in()
        if task_id not in fake.tasks:
            raise FakeApiError(404, "NotFound.Generic", "No such task")
        state, message = fake._advance(task_id)
        task = fake.tasks[task_id]
        body = {"task_id": task_id, "state": state, "task_type": task["task_type"],
                "index_id": task["index_id"], "message": message, "additional_details": None}
        return FakeResponse(body)

    def post_search(self, index_id, data, *, offset=None, limit=None):
        fake = self._fake
        fake._enter("post_search", index_id, copy.deepcopy(data))
        fake._need_sign_in()
        index = self._index(index_id)
        if fake.search_result is not None:
            return FakeResponse(copy.deepcopy(fake.search_result))
        from pcldb import search_local
        entries = [{"subject": subject, "content": entry["content"]}
                   for subject, entry in index["entries"].items()]
        return FakeResponse(search_local.search_entries(entries, data))

    def _index(self, index_id):
        index = self._fake.indexes.get(index_id)
        if index is None:
            raise FakeApiError(404, "NotFound.NoSuchIndex",
                               'There is no search index named "%s"' % index_id)
        return index

    @staticmethod
    def _invalid(message):
        return FakeApiError(400, "BadRequest.ValidationError", "Request failed input validation.",
                            {"messages": {"json": [message]}})
