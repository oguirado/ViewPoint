"""Compatibility import for supervisors constructed with the S4 broker name."""
from bus.broker import Broker as HandoffBroker

__all__ = ["HandoffBroker"]
