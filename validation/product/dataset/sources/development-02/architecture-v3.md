# Invoice adjustment: architecture v3
Source version: 3. Effective 2026-09-20.
Invoice adjustment Console depends on Invoice adjustment Service.
Invoice adjustment Worker depends on Invoice adjustment Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Invoice adjustment Service; rollout approval owner is not recorded.
