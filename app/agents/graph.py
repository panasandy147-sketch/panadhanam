"""The multi-agent workflow.

Topology (matching the desk hierarchy):

                    collect_data
                         |
        +----------------+----------------+
        |        (parallel fan-out)       |
   candlestick   derivatives   news   macro   fundamental
        |                                 |
        +----------------+----------------+
                         |
                       cmio           (synthesise, resolve conflicts)
                         |
                    risk_desk         (size, validate, veto)
                         |
                     dispatch         (alert / order)

Uses LangGraph when installed. When it isn't, an equivalent built-in async
orchestrator runs the same nodes in the same order — so the system never
hard-depends on the framework, and swapping to CrewAI/AutoGen later only means
replacing this one file.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from app.agents.base import BaseAgent
from app.agents.cmio import CMIOAgent
from app.agents.dispatcher import Dispatcher
from app.agents.risk import RiskManager
from app.agents.state import DeskState, new_state
from app.brokers.base import BrokerAdapter
from app.core.bus import Topic, bus
from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.core.models import AgentReport, Bias, CycleResult, MarketContext
from app.core.registry import autodiscover, get_agent_class

log = get_logger("graph")

try:
    from langgraph.graph import END, START, StateGraph
    LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover
    LANGGRAPH_AVAILABLE = False
    StateGraph = None  # type: ignore


class TradingDesk:
    """Builds and runs the agent workflow for one symbol per cycle."""

    def __init__(self, broker: BrokerAdapter, cfg: Config | None = None,
                 risk_manager: RiskManager | None = None) -> None:
        self.cfg = cfg or get_config()
        self.broker = broker
        autodiscover(("app.agents",))

        self.analysts: list[BaseAgent] = self._build_analysts()
        self.cmio = CMIOAgent(self.cfg)
        self.risk = risk_manager or RiskManager(self.cfg)
        self.dispatcher = Dispatcher(broker, self.cfg)
        self._graph = self._build_graph() if LANGGRAPH_AVAILABLE else None

        log.info("desk assembled: %s analysts (%s) | orchestrator=%s | reasoning=%s",
                 len(self.analysts), ", ".join(a.agent_id for a in self.analysts),
                 "langgraph" if self._graph else "builtin",
                 "claude" if self.cfg.llm_enabled else "rule-based")

    # ------------------------------------------------------------------ #
    def _build_analysts(self) -> list[BaseAgent]:
        """Instantiate every enabled analyst listed in config/agents.yaml."""
        out: list[BaseAgent] = []
        for agent_id in self.cfg.enabled_analysts():
            cls = get_agent_class(agent_id)
            if cls is None:
                log.warning("agent '%s' is enabled in agents.yaml but no class is "
                            "registered — skipping", agent_id)
                continue
            try:
                out.append(cls(self.cfg))
            except Exception as exc:
                log.error("failed to construct agent %s: %s", agent_id, exc)
        return out

    def reload(self) -> None:
        """Pick up config changes without restarting the process."""
        self.analysts = self._build_analysts()
        self.cmio = CMIOAgent(self.cfg)
        self._graph = self._build_graph() if LANGGRAPH_AVAILABLE else None

    # ------------------------------------------------------------------ #
    # Nodes
    # ------------------------------------------------------------------ #
    async def _node_analysts(self, state: DeskState) -> dict[str, Any]:
        """Fan out to every analyst concurrently — they are independent by design."""
        ctx: MarketContext = state["context"]
        # Every enabled analyst runs each cycle. Agents marked `schedule: premarket`
        # are cheap veto gates, so running them intraday costs nothing and keeps the
        # filter live if fundamentals change mid-session.
        runnable = self.analysts

        results = await asyncio.gather(
            *[a.run(ctx) for a in runnable], return_exceptions=True)

        reports: list[AgentReport] = []
        errors: list[str] = []
        for agent, res in zip(runnable, results, strict=False):
            if isinstance(res, Exception):
                log.error("analyst %s crashed: %s", agent.agent_id, res)
                errors.append(f"{agent.agent_id}: {res}")
                reports.append(AgentReport(
                    agent_id=agent.agent_id, symbol=ctx.symbol,
                    data_available=False, rationale=f"agent error: {res}"))
            else:
                reports.append(res)

        # The risk manager reads analyst invalidation levels off the context.
        ctx.__dict__["_reports"] = reports
        return {"reports": reports, "errors": errors}

    async def _node_cmio(self, state: DeskState) -> dict[str, Any]:
        decision = await self.cmio.synthesise(state["context"], state["reports"])
        await bus.publish(Topic.CYCLE_DONE, {
            "cycle_id": state["cycle_id"], "symbol": state["symbol"],
            "bias": decision["bias"].value if isinstance(decision["bias"], Bias) else decision["bias"],
            "composite_score": decision["composite_score"],
            "proceed": decision["proceed"],
            "rationale": decision["rationale"],
            "confirmations": decision["confirmations"],
            "conflicts": decision.get("conflicts", []),
            "counter_argument": decision.get("counter_argument", ""),
        })
        return {"decision": decision}

    async def _node_risk(self, state: DeskState) -> dict[str, Any]:
        decision = state["decision"]
        if not decision.get("proceed"):
            return {"signal": None}

        signal = self.risk.evaluate(
            ctx=state["context"],
            bias=decision["bias"],
            reports=state["reports"],
            composite_score=decision["composite_score"],
            confirmations=decision["confirmations"],
            rationale=decision["rationale"],
            counter_argument=decision.get("counter_argument", ""),
        )
        await bus.publish(Topic.SIGNAL_PROPOSED, signal)
        await bus.publish(Topic.RISK_STATE, self.risk.snapshot())
        return {"signal": signal}

    async def _node_dispatch(self, state: DeskState) -> dict[str, Any]:
        signal = state.get("signal")
        if signal is None:
            return {"dispatch": {"dispatched": False, "reason": "no signal"}}
        result = await self.dispatcher.dispatch(signal)
        return {"dispatch": result}

    # ------------------------------------------------------------------ #
    # LangGraph wiring
    # ------------------------------------------------------------------ #
    def _build_graph(self):
        graph = StateGraph(DeskState)
        graph.add_node("analysts", self._node_analysts)
        graph.add_node("cmio", self._node_cmio)
        graph.add_node("risk_desk", self._node_risk)
        graph.add_node("dispatcher", self._node_dispatch)

        graph.add_edge(START, "analysts")
        graph.add_edge("analysts", "cmio")

        # Skip the risk desk entirely when the CMIO decides not to proceed.
        graph.add_conditional_edges(
            "cmio",
            lambda s: "risk_desk" if s.get("decision", {}).get("proceed") else END,
            {"risk_desk": "risk_desk", END: END},
        )
        # Only dispatch what risk approved.
        graph.add_conditional_edges(
            "risk_desk",
            lambda s: "dispatcher" if (s.get("signal") is not None
                                       and s["signal"].status.value == "APPROVED") else END,
            {"dispatcher": "dispatcher", END: END},
        )
        graph.add_edge("dispatcher", END)
        return graph.compile()

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    async def run_cycle(self, ctx: MarketContext, cycle_id: str | None = None) -> CycleResult:
        cycle_id = cycle_id or f"CY-{uuid.uuid4().hex[:8]}"
        started = time.perf_counter()
        await bus.publish(Topic.CYCLE_START, {"cycle_id": cycle_id, "symbol": ctx.symbol})

        state = new_state(cycle_id, ctx.symbol, ctx)

        if self._graph is not None:
            final = await self._graph.ainvoke(state)
        else:
            final = await self._run_builtin(state)

        decision = final.get("decision") or {}
        bias = decision.get("bias", Bias.NEUTRAL)
        if not isinstance(bias, Bias):
            bias = Bias(str(bias)) if str(bias) in {b.value for b in Bias} else Bias.NEUTRAL

        signal = final.get("signal")
        rejected = list(signal.rejection_reasons) if signal else []
        if not decision.get("proceed", False) and not rejected:
            rejected = [decision.get("rationale", "CMIO chose not to proceed")]

        return CycleResult(
            cycle_id=cycle_id, symbol=ctx.symbol, bias=bias,
            composite_score=decision.get("composite_score", 0.0),
            reports=final.get("reports", []),
            signal=signal if signal and signal.status.value == "APPROVED" else None,
            rejected=rejected,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    async def _run_builtin(self, state: DeskState) -> DeskState:
        """Same topology, no framework dependency."""
        state.update(await self._node_analysts(state))
        state.update(await self._node_cmio(state))
        if state["decision"].get("proceed"):
            state.update(await self._node_risk(state))
            signal = state.get("signal")
            if signal is not None and signal.status.value == "APPROVED":
                state.update(await self._node_dispatch(state))
        return state

    # ------------------------------------------------------------------ #
    def describe(self) -> dict[str, Any]:
        """What the dashboard shows in the agent roster panel."""
        return {
            "orchestrator": "langgraph" if self._graph else "builtin",
            "reasoning": "claude" if self.cfg.llm_enabled else "rule-based",
            "model": self.cfg.llm_model if self.cfg.llm_enabled else None,
            "broker": self.broker.name,
            "agents": [
                {"id": a.agent_id, "name": a.name,
                 "type": "analyst", "enabled": True}
                for a in self.analysts
            ] + [
                {"id": "cmio", "name": self.cmio.name, "type": "orchestrator", "enabled": True},
                {"id": "risk", "name": self.risk.name, "type": "guardrail", "enabled": True},
                {"id": "dispatcher", "name": "Order & Signal Dispatcher",
                 "type": "execution", "enabled": True},
            ],
        }
