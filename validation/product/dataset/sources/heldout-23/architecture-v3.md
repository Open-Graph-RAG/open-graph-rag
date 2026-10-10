# Billing reconciliation: architecture v3
Source version: 3. Effective 2026-09-20.
Billing reconciliation Console depends on Billing reconciliation Service.
Billing reconciliation Worker depends on Billing reconciliation Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Billing reconciliation Service; rollout approval owner is not recorded.
