# Quota enforcement: architecture v3
Source version: 3. Effective 2026-09-20.
Quota enforcement Console depends on Quota enforcement Service.
Quota enforcement Worker depends on Quota enforcement Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Quota enforcement Service; rollout approval owner is not recorded.
