# Service retirement: architecture v3
Source version: 3. Effective 2026-09-20.
Service retirement Console depends on Service retirement Service.
Service retirement Worker depends on Service retirement Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Service retirement Service; rollout approval owner is not recorded.
