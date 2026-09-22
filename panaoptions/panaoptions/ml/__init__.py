"""The ML layer is OPTIONAL.

It needs scikit-learn, xgboost and shap, which are heavy and awkward to
install on Windows. Nothing outside this package imports them at module level,
so the desk runs perfectly well without any of it — the classifier is a VETO
on top of the rules, never a reason to take a trade the rules rejected.

    pip install -r requirements-ml.txt
"""
