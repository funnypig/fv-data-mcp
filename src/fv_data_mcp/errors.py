from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ResearchError(Exception):
    """Describe a safe, structured research failure and optional retry advice."""

    code: str
    message: str
    retryable: bool = False
    retry_after_seconds: float | None = None

    def __str__(self) -> str:
        """Return the client-safe error message."""
        return self.message

    def as_dict(self) -> dict[str, str | bool | float | None]:
        """Serialize error details for tool responses and shared failure caching."""
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after_seconds": self.retry_after_seconds,
        }


def unavailable(message: str = "A required service is temporarily unavailable.") -> ResearchError:
    """Create a retryable dependency-unavailability error with a short retry delay."""
    return ResearchError("service_unavailable", message, True, 5)
