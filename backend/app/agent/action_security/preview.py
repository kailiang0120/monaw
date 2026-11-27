"""Ephemeral exact action previews, separate from public events and durable logs."""

from collections import OrderedDict
from copy import deepcopy
import json
import threading
import time

from .sensitive_data import redact_sensitive

_LOCK = threading.RLock()
_PREVIEWS = OrderedDict()
_MAX_PREVIEWS = 128
_MAX_BYTES = 256 * 1024


def save_preview(ticket, parameters):
    """Retain a redacted snapshot only while this exact pending ticket can be reviewed."""
    value = deepcopy(parameters)
    if isinstance(value.get("env"), dict):
        value["env"] = {key: "[REDACTED]" for key in value["env"]}
    value = redact_sensitive(value)
    if len(json.dumps(value, ensure_ascii=False).encode()) > _MAX_BYTES:
        return
    with _LOCK:
        now = time.monotonic()
        for identifier, (_, expires, _) in list(_PREVIEWS.items()):
            if expires <= now:
                _PREVIEWS.pop(identifier, None)
        while len(_PREVIEWS) >= _MAX_PREVIEWS:
            _PREVIEWS.popitem(last=False)
        _PREVIEWS[ticket.id] = (ticket.authorization_binding, now + 600, value)


def get_preview(ticket):
    """Return only the snapshot bound to this ticket; missing previews require reissue."""
    with _LOCK:
        record = _PREVIEWS.get(ticket.id)
        if not record or record[0] != ticket.authorization_binding or record[1] <= time.monotonic():
            return None
        return {"authorization_binding": record[0], "parameters": deepcopy(record[2])}
