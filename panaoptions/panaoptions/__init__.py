"""panaoptions — intraday US options paper trading on a small account.

Deliberately standalone: it shares no code with the panadhanam package in the
same repository. They have different risk models, different instruments and
different session rules, and coupling them would mean a change to one could
silently alter the other's behaviour.

Nothing in this package can place a real order. There is no broker adapter.
"""
__version__ = "0.1.0"
