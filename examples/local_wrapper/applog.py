"""Optional application-local helpers around standard-library logging."""

import logging

_LOGGER = logging.getLogger("application")


def log(level: int, message: str, **fields: object) -> None:
    """Write a message at an arbitrary logging level."""
    _LOGGER.log(level, message, extra=fields)


def debug(message: str, **fields: object) -> None:
    """Write a debug message."""
    _LOGGER.debug(message, extra=fields)


def info(message: str, **fields: object) -> None:
    """Write an informational message."""
    _LOGGER.info(message, extra=fields)


def warning(message: str, **fields: object) -> None:
    """Write a warning message."""
    _LOGGER.warning(message, extra=fields)


def error(message: str, exception: BaseException, **fields: object) -> None:
    """Write an error message with exception information."""
    _LOGGER.error(message, exc_info=exception, extra=fields)
