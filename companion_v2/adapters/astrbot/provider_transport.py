"""Transport evidence for calls that could not reach the provider."""

from httpx import ConnectError, ConnectTimeout, PoolTimeout, UnsupportedProtocol


def definitely_not_sent(error):
    """Read typed causes, never provider text, to prove no request was sent.

    Read/write timeouts, generic connection errors and HTTP responses do not
    provide this proof. Their reservations must remain held for accounting.
    """
    seen = set()
    while isinstance(error, Exception) and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (ConnectError, ConnectTimeout, PoolTimeout, UnsupportedProtocol)):
            return True
        error = error.__cause__
    return False


_HTTP_REJECTION_STATUSES = frozenset({400, 401, 403, 404, 405, 415, 422, 429})


def rejected_before_generation(error):
    """Return true for HTTP responses that reject a request before generation.

    These are typed client-side rejections (bad request, auth, unsupported
    media, rate limit), not transport timeouts. No completion was produced, so
    the caller may safely mark the model call as not started and avoid leaving
    a reflection batch permanently ``running``.
    """
    seen = set()
    while isinstance(error, Exception) and id(error) not in seen:
        seen.add(id(error))
        status = getattr(error, "status_code", None)
        if type(status) is int and status in _HTTP_REJECTION_STATUSES:
            return True
        error = error.__cause__
    return False
