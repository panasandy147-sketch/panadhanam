"""Walk-forward training with calibrated probabilities.

Why walk-forward and not a random split: a random split puts Tuesday afternoon
in the training set and Tuesday morning in the test set. The model then "knows"
the day it is being tested on and every metric is a fiction. Here each test
window is strictly AFTER the train window that produced it, which is the only
arrangement that answers the question you actually care about — would this have
worked on data it had never seen?

Why calibration: the strategy gates on "probability above 65%", so the number
has to mean 65%. A raw gradient-boosted score is a ranking, not a probability;
untreated, 0.65 from XGBoost might correspond to a 40% real hit rate.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from panaoptions.config import DATA_DIR
from panaoptions.logging import get_logger
from panaoptions.ml.features import FEATURE_COLUMNS, build
from panaoptions.ml.labels import triple_barrier

log = get_logger("ml.train")

MODEL_DIR = DATA_DIR / "models"


class MissingDependencies(RuntimeError):
    pass


def _require() -> tuple[Any, Any, Any]:
    try:
        import joblib
        from sklearn.calibration import CalibratedClassifierCV
        from xgboost import XGBClassifier
    except ImportError as exc:
        raise MissingDependencies(
            "The ML pipeline needs scikit-learn, xgboost and joblib:\n"
            "    pip install -r requirements-ml.txt\n"
            "The desk runs without them — the classifier is an optional veto."
        ) from exc
    return XGBClassifier, CalibratedClassifierCV, joblib


@dataclass
class FoldResult:
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_rows: int
    test_rows: int
    positives_pct: float
    auc: float | None = None
    brier: float | None = None
    precision_at_threshold: float | None = None
    signals_at_threshold: int = 0


@dataclass
class TrainingReport:
    symbol: str
    folds: list[FoldResult] = field(default_factory=list)
    feature_importance: dict[str, float] = field(default_factory=dict)
    threshold: float = 0.65
    trained_at: str = ""
    rows: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "trained_at": self.trained_at,
            "rows": self.rows,
            "threshold": self.threshold,
            "folds": [f.__dict__ for f in self.folds],
            "feature_importance": self.feature_importance,
            "mean_auc": round(float(np.mean(
                [f.auc for f in self.folds if f.auc is not None] or [0])), 4),
            "mean_precision_at_threshold": round(float(np.mean(
                [f.precision_at_threshold for f in self.folds
                 if f.precision_at_threshold is not None] or [0])), 4),
        }


def dataset(df5: pd.DataFrame, daily: pd.DataFrame | None, cfg) -> pd.DataFrame:
    """Features and label, aligned, with unlabelled rows dropped."""
    features = build(df5, daily)
    labels = triple_barrier(
        df5,
        horizon=int(cfg.get("ml.horizon_bars", 6)),
        target_atr=float(cfg.get("ml.target_atr_multiple", 1.5)),
        stop_atr=float(cfg.get("ml.stop_atr_multiple", 1.0)),
    )
    frame = features.join(labels)
    return frame.dropna(subset=["label"]).dropna()


def walk_forward(frame: pd.DataFrame, cfg, symbol: str = "ALL") -> tuple[Any, TrainingReport]:
    """Train fold by fold, then fit the final model on everything."""
    XGBClassifier, CalibratedClassifierCV, joblib = _require()
    from sklearn.metrics import brier_score_loss, roc_auc_score

    threshold = float(cfg.get("ml.min_probability", 0.65))
    report = TrainingReport(symbol=symbol, threshold=threshold,
                            trained_at=datetime.now().isoformat(),
                            rows=len(frame))
    if frame.empty:
        raise ValueError("no labelled rows — check the date range and the feed")

    train_days = int(cfg.get("ml.train_window_days", 120))
    test_days = int(cfg.get("ml.test_window_days", 20))

    days = sorted({ts.date() for ts in frame.index})
    if len(days) < train_days + test_days:
        raise ValueError(
            f"{len(days)} sessions of data but the walk-forward needs at least "
            f"{train_days + test_days}. Fetch a longer history, or shorten "
            f"ml.train_window_days.")

    start = 0
    while start + train_days + test_days <= len(days):
        train_days_slice = days[start:start + train_days]
        test_days_slice = days[start + train_days:start + train_days + test_days]

        train = frame[[ts.date() in set(train_days_slice) for ts in frame.index]]
        test = frame[[ts.date() in set(test_days_slice) for ts in frame.index]]
        if train.empty or test.empty:
            start += test_days
            continue

        model = _fit(train, XGBClassifier, CalibratedClassifierCV)
        probabilities = model.predict_proba(test[FEATURE_COLUMNS])[:, 1]
        y_true = test["label"].to_numpy()

        fold = FoldResult(
            train_start=str(train_days_slice[0]), train_end=str(train_days_slice[-1]),
            test_start=str(test_days_slice[0]), test_end=str(test_days_slice[-1]),
            train_rows=len(train), test_rows=len(test),
            positives_pct=round(float(y_true.mean()) * 100, 2),
        )
        if len(set(y_true)) > 1:
            fold.auc = round(float(roc_auc_score(y_true, probabilities)), 4)
            fold.brier = round(float(brier_score_loss(y_true, probabilities)), 4)

        selected = probabilities >= threshold
        fold.signals_at_threshold = int(selected.sum())
        if fold.signals_at_threshold:
            fold.precision_at_threshold = round(
                float(y_true[selected].mean()), 4)
        report.folds.append(fold)
        log.info("fold %s..%s -> AUC %s, %d signals at p>=%.2f, precision %s",
                 fold.test_start, fold.test_end, fold.auc,
                 fold.signals_at_threshold, threshold,
                 fold.precision_at_threshold)
        start += test_days

    final = _fit(frame, XGBClassifier, CalibratedClassifierCV)
    report.feature_importance = _importance(final)
    return final, report


def _fit(frame: pd.DataFrame, XGBClassifier, CalibratedClassifierCV):
    x, y = frame[FEATURE_COLUMNS], frame["label"].astype(int)
    base = XGBClassifier(
        n_estimators=250, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0,
        eval_metric="logloss", n_jobs=2, random_state=42,
    )
    # `cv` must stay time-ordered here too; shuffling inside calibration would
    # reintroduce the lookahead the walk-forward exists to prevent.
    from sklearn.model_selection import TimeSeriesSplit
    calibrated = CalibratedClassifierCV(base, method="isotonic",
                                        cv=TimeSeriesSplit(n_splits=3))
    calibrated.fit(x, y)
    return calibrated


def _importance(model) -> dict[str, float]:
    """Gain importance, averaged over the calibrated ensemble."""
    try:
        totals = np.zeros(len(FEATURE_COLUMNS))
        members = model.calibrated_classifiers_
        for member in members:
            estimator = getattr(member, "estimator", None) or member.base_estimator
            totals += np.asarray(estimator.feature_importances_, dtype=float)
        totals /= max(len(members), 1)
        pairs = sorted(zip(FEATURE_COLUMNS, totals, strict=False), key=lambda kv: -kv[1])
        return {name: round(float(value), 5) for name, value in pairs}
    except Exception as exc:
        log.debug("could not read feature importance: %s", exc)
        return {}


def save(model, report: TrainingReport, name: str = "breakout") -> dict[str, str]:
    _, _, joblib = _require()
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model_path = MODEL_DIR / f"{name}.joblib"
    report_path = MODEL_DIR / f"{name}.report.json"
    joblib.dump({"model": model, "features": FEATURE_COLUMNS}, model_path)
    report_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    log.info("model saved to %s", model_path)
    return {"model": str(model_path), "report": str(report_path)}


def load(name: str = "breakout") -> tuple[Any, list[str]]:
    _, _, joblib = _require()
    path = MODEL_DIR / f"{name}.joblib"
    if not path.exists():
        raise FileNotFoundError(
            f"No trained model at {path}. Run: python run.py --train")
    bundle = joblib.load(path)
    return bundle["model"], bundle.get("features", FEATURE_COLUMNS)
