"""Explainability. SHAP when it is installed, feature importance when it is not.

A probability with no reasoning behind it is an oracle, and an oracle is not
something to risk money on. This answers "why this trade" in the same units the
model thinks in: which feature pushed the score up, and by how much.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from panaoptions.logging import get_logger
from panaoptions.ml.features import build

log = get_logger("ml.explain")


def _underlying(model) -> Any:
    """The raw tree model inside the calibration wrapper."""
    members = getattr(model, "calibrated_classifiers_", None)
    if not members:
        return model
    member = members[0]
    return getattr(member, "estimator", None) or getattr(member, "base_estimator", model)


def explain_row(model, features: list[str], bars: pd.DataFrame,
                top_n: int = 6) -> dict[str, Any]:
    frame = build(bars)
    row = frame.iloc[[-1]][features]
    if row.isna().any(axis=1).iloc[0]:
        return {"method": "none", "reason": "features incomplete for this bar",
                "contributions": []}

    tree = _underlying(model)
    try:
        import shap

        explainer = shap.TreeExplainer(tree)
        values = explainer.shap_values(row)
        if isinstance(values, list):
            values = values[-1]
        pairs = sorted(zip(features, values[0], strict=False), key=lambda kv: -abs(kv[1]))
        return {
            "method": "shap",
            "contributions": [
                {"feature": name,
                 "value": round(float(row.iloc[0][name]), 4),
                 "impact": round(float(impact), 5),
                 "direction": "raises" if impact > 0 else "lowers"}
                for name, impact in pairs[:top_n]
            ],
        }
    except ImportError:
        pass
    except Exception as exc:
        log.debug("SHAP failed, falling back to importance: %s", exc)

    try:
        importance = getattr(tree, "feature_importances_", None)
        if importance is None:
            return {"method": "none", "contributions": []}
        pairs = sorted(zip(features, importance, strict=False), key=lambda kv: -kv[1])
        return {
            "method": "gain_importance",
            "note": ("Model-wide importance, not this bar's attribution. "
                     "`pip install shap` for per-trade reasoning."),
            "contributions": [
                {"feature": name,
                 "value": round(float(row.iloc[0][name]), 4),
                 "impact": round(float(score), 5)}
                for name, score in pairs[:top_n]
            ],
        }
    except Exception as exc:
        log.debug("importance unavailable: %s", exc)
        return {"method": "none", "contributions": []}
