# Event replay: architecture v3
Source version: 3. Effective 2026-09-20.
Event replay Console depends on Event replay Service.
Event replay Worker depends on Event replay Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Event replay Service; rollout approval owner is not recorded.
