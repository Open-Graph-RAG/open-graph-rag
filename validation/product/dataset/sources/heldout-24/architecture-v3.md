# Fraud screening: architecture v3
Source version: 3. Effective 2026-09-20.
Fraud screening Console depends on Fraud screening Service.
Fraud screening Worker depends on Fraud screening Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Fraud screening Service; rollout approval owner is not recorded.
