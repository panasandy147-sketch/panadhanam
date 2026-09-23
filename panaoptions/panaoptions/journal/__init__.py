"""The learning loop: grade every closed trade on PROCESS, not outcome.

A profitable trade that broke a rule is a bad trade. A losing trade that
honoured its invalidation is an acceptable one. Grading on P&L teaches the
opposite of what you want, because the market pays out on bad decisions often
enough to make them feel right.

Nothing here can change how the desk trades. It reads the ledger and writes a
verdict; the risk rules stay deterministic.
"""
