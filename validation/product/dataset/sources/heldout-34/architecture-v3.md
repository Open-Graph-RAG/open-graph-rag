# Address validation: architecture v3
Source version: 3. Effective 2026-09-20.
Address validation Console depends on Address validation Service.
Address validation Worker depends on Address validation Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Address validation Service; rollout approval owner is not recorded.
