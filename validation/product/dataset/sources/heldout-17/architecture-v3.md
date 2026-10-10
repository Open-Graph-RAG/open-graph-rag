# Audit retention: architecture v3
Source version: 3. Effective 2026-09-20.
Audit retention Console depends on Audit retention Service.
Audit retention Worker depends on Audit retention Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Audit retention Service; rollout approval owner is not recorded.
