"""Off-chain oracle / bridge for the BybitAttestor contract.

Listens to on-chain events (DepositRequested, WithdrawRequested),
persists request lifecycle in SQLite, and (in later subtasks .12-.14)
will drive Bybit V5 operations + push attestations back on-chain.
"""
