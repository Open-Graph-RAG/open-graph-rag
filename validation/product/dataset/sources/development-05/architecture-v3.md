# Refund automation: architecture v3
Source version: 3. Effective 2026-09-20.
Refund automation Console depends on Refund automation Service.
Refund automation Worker depends on Refund automation Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Refund automation Service; rollout approval owner is not recorded.
