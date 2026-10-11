"""Construction-time invariant checks shared by domain types.

Static typing already says "Decimal" and "datetime"; these checks also run at
runtime because values cross untyped boundaries (exchange payloads, database
rows) on their way into the domain.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from arbitron.domain.errors import DomainValidationError


def require_decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal):
        msg = f"{name} must be a Decimal, got {type(value).__name__}"
        raise DomainValidationError(msg)
    if not value.is_finite():
        msg = f"{name} must be finite, got {value}"
        raise DomainValidationError(msg)
    return value


def require_positive(name: str, value: object) -> Decimal:
    number = require_decimal(name, value)
    if number <= 0:
        msg = f"{name} must be positive, got {number}"
        raise DomainValidationError(msg)
    return number


def require_non_negative(name: str, value: object) -> Decimal:
    number = require_decimal(name, value)
    if number < 0:
        msg = f"{name} must not be negative, got {number}"
        raise DomainValidationError(msg)
    return number


def require_utc(name: str, value: object) -> datetime:
    if not isinstance(value, datetime):
        msg = f"{name} must be a datetime, got {type(value).__name__}"
        raise DomainValidationError(msg)
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        msg = f"{name} must be timezone-aware UTC, got {value.isoformat()}"
        raise DomainValidationError(msg)
    return value


def require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"{name} must be a non-empty string"
        raise DomainValidationError(msg)
    return value
