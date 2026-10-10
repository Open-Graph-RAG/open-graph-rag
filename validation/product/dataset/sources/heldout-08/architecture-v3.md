# Tax calculation: architecture v3
Source version: 3. Effective 2026-09-20.
Tax calculation Console depends on Tax calculation Service.
Tax calculation Worker depends on Tax calculation Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Tax calculation Service; rollout approval owner is not recorded.
