# Plan downgrade: architecture v3
Source version: 3. Effective 2026-09-20.
Plan downgrade Console depends on Plan downgrade Service.
Plan downgrade Worker depends on Plan downgrade Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Plan downgrade Service; rollout approval owner is not recorded.
