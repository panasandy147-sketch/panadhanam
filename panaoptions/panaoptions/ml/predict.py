"""predict_signal(): the live-loop entry point.

Deliberately narrow. It takes the bars seen so far, builds the same features
the model was trained on, and returns one probability. It never sees a label,
never touches the labelling module, and returns None rather than a guess when
anything is missing — a None is a silent abstention, and the desk falls back to
its rules. A fabricated 0.5 would be indistinguishable from a real answer.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from panaoptions.engine import indicators as ta
from panaoptions.logging import get_logger
from panaoptions.ml.features import build

log = get_logger("ml.predict")


class Predictor:
    def __init__(self, cfg, name: str = "breakout") -> None:
        from panaoptions.ml.train import load
        self.cfg = cfg
        self.model, self.features = load(name)

    def probability(self, candles: list, cfg=None) -> float | None:
        df = ta.to_frame(candles) if not isinstance(candles, pd.DataFrame) else candles
        return self.predict_signal(df)

    def predict_signal(self, current_bar_data: pd.DataFrame) -> float | None:
        """Probability that the LAST bar in `current_bar_data` works out."""
        if current_bar_data is None or current_bar_data.empty:
            return None
        try:
            frame = build(current_bar_data)
            row = frame.iloc[[-1]][self.features]
            if row.isna().any(axis=1).iloc[0]:
                # Early in the session the rolling windows are not full yet.
                log.debug("features incomplete for the latest bar — abstaining")
                return None
            return float(self.model.predict_proba(row)[0, 1])
        except Exception as exc:
            log.warning("prediction failed, abstaining: %s", exc)
            return None

    def explain(self, current_bar_data: pd.DataFrame,
                top_n: int = 6) -> dict[str, Any]:
        """Why this bar scored as it did — SHAP if installed, gain if not."""
        from panaoptions.ml.explain import explain_row
        return explain_row(self.model, self.features, current_bar_data, top_n)
