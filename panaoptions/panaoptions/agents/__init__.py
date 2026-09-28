"""The four-agent committee that stands between a signal and an order.

    Technical     5m/15m chart and relative volume          (agents/technical.py)
    Derivatives   contract, put/call, IV percentile, flow   (agents/derivatives.py)
    Macro         headlines and index futures; hard veto    (agents/macro.py)
    CMIO          combines, then the Risk Gatekeeper        (agents/cmio.py)
"""
from panaoptions.agents.base import AgentVote, Opinion
from panaoptions.agents.cmio import CMIO, Verdict, combine

__all__ = ["CMIO", "AgentVote", "Opinion", "Verdict", "combine"]
