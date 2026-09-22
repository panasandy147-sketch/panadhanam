"""Discord / Telegram alerts. Never allowed to break a trading cycle.

A webhook is a nice-to-have: if Discord is down, or the URL is wrong, or the
network drops, the desk must carry on marking and managing positions. Every
failure here is logged and swallowed.

With no URL configured nothing is sent and nothing is attempted — the feature
is off, not broken.
"""
from __future__ import annotations

from typing import Any

import httpx

from panaoptions.logging import get_logger
from panaoptions.models import Direction, PaperTrade, Signal

log = get_logger("notify")


class Notifier:
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    @property
    def discord_url(self) -> str:
        return str(self.cfg.get("notify.discord_webhook_url", "") or "")

    @property
    def telegram(self) -> tuple[str, str]:
        return (str(self.cfg.get("notify.telegram_bot_token", "") or ""),
                str(self.cfg.get("notify.telegram_chat_id", "") or ""))

    @property
    def enabled(self) -> bool:
        return bool(self.discord_url) or all(self.telegram)

    # ------------------------------------------------------------------ #
    def format_entry(self, signal: Signal) -> str:
        right = "CALL" if signal.direction is Direction.LONG else "PUT"
        multiplier = self.cfg.multiplier
        currency = str(self.cfg.get("account.currency", "$"))

        lines = [
            f"**{signal.symbol} {right}** — {signal.pattern}",
            f"Contract: `{signal.contract.expiry} {signal.contract.strike:g} {right}` "
            f"({abs(signal.contract.delta):.2f} delta, {signal.contract.dte}d)",
            f"Entry: `{signal.entry_price:.2f}`  x{signal.quantity}",
            f"Stop: `{signal.stop_price:.2f}`  "
            f"TP1: `{signal.target_1:.2f}`  TP2: `{signal.target_2:.2f}`",
            f"Cost {currency}{signal.cost(multiplier):,.2f} · "
            f"risk at stop {currency}{signal.risk_at_stop(multiplier):,.2f}",
        ]
        if signal.ml_probability is not None:
            lines.append(f"Model probability: {signal.ml_probability:.0%}")
        if signal.confirmations:
            lines.append("Why: " + "; ".join(signal.confirmations[:4]))
        lines.append("_Paper trade. Nothing was sent to a broker._")
        return "\n".join(lines)

    def format_exit(self, trade: PaperTrade) -> str:
        currency = str(self.cfg.get("account.currency", "$"))
        reason = trade.exit_reason.value if trade.exit_reason else "CLOSED"
        sign = "+" if trade.realised_pnl >= 0 else ""
        return (f"**{trade.contract_label} closed** — {reason}\n"
                f"P&L: `{sign}{currency}{trade.realised_pnl:,.2f}`")

    # ------------------------------------------------------------------ #
    async def send(self, text: str) -> dict[str, Any]:
        if not self.enabled:
            return {"sent": False, "reason": "no webhook configured"}

        results: dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=10.0) as client:
            if self.discord_url:
                results["discord"] = await self._post(
                    client, self.discord_url, {"content": text})

            token, chat_id = self.telegram
            if token and chat_id:
                results["telegram"] = await self._post(
                    client, f"https://api.telegram.org/bot{token}/sendMessage",
                    {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
        return {"sent": any(results.values()), **results}

    async def _post(self, client: httpx.AsyncClient, url: str,
                    payload: dict[str, Any]) -> bool:
        try:
            r = await client.post(url, json=payload)
            if r.status_code >= 300:
                log.warning("webhook rejected (HTTP %s): %s",
                            r.status_code, r.text[:160])
                return False
            return True
        except Exception as exc:
            log.warning("webhook failed: %s", exc)
            return False

    async def entry(self, signal: Signal) -> dict[str, Any]:
        return await self.send(self.format_entry(signal))

    async def exit(self, trade: PaperTrade) -> dict[str, Any]:
        return await self.send(self.format_exit(trade))
