# Region routing: architecture v3
Source version: 3. Effective 2026-09-20.
Region routing Console depends on Region routing Service.
Region routing Worker depends on Region routing Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Region routing Service; rollout approval owner is not recorded.
