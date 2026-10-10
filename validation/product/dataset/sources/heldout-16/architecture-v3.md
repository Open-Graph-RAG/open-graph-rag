# Credit allocation: architecture v3
Source version: 3. Effective 2026-09-20.
Credit allocation Console depends on Credit allocation Service.
Credit allocation Worker depends on Credit allocation Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Credit allocation Service; rollout approval owner is not recorded.
