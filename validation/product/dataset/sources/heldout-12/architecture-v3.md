# Data export: architecture v3
Source version: 3. Effective 2026-09-20.
Data export Console depends on Data export Service.
Data export Worker depends on Data export Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Data export Service; rollout approval owner is not recorded.
