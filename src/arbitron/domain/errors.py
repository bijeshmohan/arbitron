class DomainError(Exception):
    """Base class for domain rule violations."""


class DomainValidationError(DomainError, ValueError):
    """A value object or entity was constructed with invalid data."""


class InvalidTransitionError(DomainError):
    """A state transition not permitted by the state machine was attempted."""
