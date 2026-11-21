"""The errors a person can cause. Anything else that escapes is a bug.

A `PclError` carries a plain-word message, a short `code` for programs, the HTTP `status` the
server answers with, and optional `details` (JSON-ready). The command line prints the message
and exits with 1; the server sends `{"error": {"code", "message", "details"}}`.

Never put a password, a token, a key or a connection string into a message or into details.
"""

from __future__ import annotations


class PclError(Exception):
    """Something was asked that cannot be done. Say what happened and what to do next."""

    code = "error"
    status = 400

    def __init__(self, message, code=None, status=None, details=None):
        super().__init__(message)
        self.message = str(message)
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        self.details = details

    def __str__(self):
        return self.message

    def to_dict(self):
        """The error as the API sends it: {"code", "message", "details"}."""
        return {"code": self.code, "message": self.message, "details": self.details}


class NotFound(PclError):
    """The thing that was asked for does not exist."""

    code = "not_found"
    status = 404


class Forbidden(PclError):
    """The person is known but may not do this."""

    code = "forbidden"
    status = 403


class Unauthorized(PclError):
    """Nobody is signed in, or the token is not accepted."""

    code = "unauthorized"
    status = 401


class Conflict(PclError):
    """The request does not fit what is already there."""

    code = "conflict"
    status = 409


class TooLarge(PclError):
    """The request is bigger than allowed."""

    code = "too_large"
    status = 413


class RateLimited(PclError):
    """Too many requests in a short time."""

    code = "rate_limited"
    status = 429


class Unavailable(PclError):
    """Something this needs (the database, Globus Search, the ARC LLM) does not answer now."""

    code = "unavailable"
    status = 503
