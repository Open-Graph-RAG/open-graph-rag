# Currency conversion: architecture v3
Source version: 3. Effective 2026-09-20.
Currency conversion Console depends on Currency conversion Service.
Currency conversion Worker depends on Currency conversion Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Currency conversion Service; rollout approval owner is not recorded.
