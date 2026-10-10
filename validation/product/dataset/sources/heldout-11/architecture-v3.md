# Account closure: architecture v3
Source version: 3. Effective 2026-09-20.
Account closure Console depends on Account closure Service.
Account closure Worker depends on Account closure Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Account closure Service; rollout approval owner is not recorded.
