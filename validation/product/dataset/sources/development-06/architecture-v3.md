# Trial conversion: architecture v3
Source version: 3. Effective 2026-09-20.
Trial conversion Console depends on Trial conversion Service.
Trial conversion Worker depends on Trial conversion Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Trial conversion Service; rollout approval owner is not recorded.
