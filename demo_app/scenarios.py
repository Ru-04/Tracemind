"""Deliberately failing code paths. Each function fails the same way on every call."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass


class ConfigurationError(RuntimeError):
    """A required setting is missing."""


class DependencyTimeoutError(TimeoutError):
    """A downstream dependency did not answer in time."""


class InvoiceError(ValueError):
    """An invoice failed validation."""


def controlled_exception() -> None:
    """S1: raises a fixed validation error."""
    raise InvoiceError("Invoice total cannot be negative: -42")


def dependency_timeout() -> None:
    """S2: simulates a database that never answers. No real sleeping, so tests stay fast."""
    raise DependencyTimeoutError("orders-db did not respond within 2.0s (attempt 3/3)")


def config_error() -> str:
    """S3: fails while the required gateway URL is unset or empty."""
    url = os.environ.get("DEMO_PAYMENT_GATEWAY_URL", "").strip()
    if not url:
        raise ConfigurationError("DEMO_PAYMENT_GATEWAY_URL is not set")
    return url


def average_item_price(total_cents: int, quantity: int) -> int:
    """S4: the known regression. Before the change that introduced it, an empty cart
    returned 0. The unguarded division makes it raise instead."""
    return total_cents // quantity


def regression() -> int:
    """S4: an empty cart reaches the unguarded division."""
    return average_item_price(total_cents=0, quantity=0)


@dataclass(frozen=True)
class Scenario:
    id: str
    route: str
    description: str
    expected_error_type: str
    run: Callable[[], object]


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "S1-controlled-exception",
        "/demo/controlled-exception",
        "Fixed validation exception",
        "InvoiceError",
        controlled_exception,
    ),
    Scenario(
        "S2-dependency-timeout",
        "/demo/dependency-timeout",
        "Simulated dependency timeout after retries",
        "DependencyTimeoutError",
        dependency_timeout,
    ),
    Scenario(
        "S3-config-error",
        "/demo/config-error",
        "Missing required configuration",
        "ConfigurationError",
        config_error,
    ),
    Scenario(
        "S4-regression",
        "/demo/regression",
        "Division by zero on an empty cart (regression)",
        "ZeroDivisionError",
        regression,
    ),
)
