from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from fv_data_mcp.errors import ResearchError


class Cursors:
    """Sign snapshot-pagination state and verify query-bound cursor integrity."""

    secret: bytes

    def __init__(self, secret: str) -> None:
        """Encode the configured HMAC signing secret without exposing it."""
        self.secret = secret.encode()

    def encode(self, payload: dict[str, Any]) -> str:
        """Serialize and sign pagination state without encrypting its contents."""
        body = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).rstrip(b"=")
        signature = hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        return body.decode() + "." + signature

    def decode(self, token: str, binding: dict[str, Any]) -> dict[str, Any]:
        """Verify signature, query binding, offset, and absolute cursor expiry.

        Raises:
            ResearchError: The cursor is invalid, mismatched, or expired.
        """
        try:
            if len(token) > 4096:
                raise ValueError
            body, signature = token.rsplit(".", 1)
            expected = hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            if data["binding"] != binding or type(data["offset"]) is not int or data["offset"] < 0:
                raise ValueError
            if data["expires"] <= time.time():
                raise ResearchError("snapshot_expired", "Pagination snapshot expired. Start again.")
            return data
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ResearchError(
                "invalid_cursor", "Cursor is invalid or query parameters changed."
            ) from None
