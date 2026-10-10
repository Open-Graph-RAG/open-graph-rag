# Payment retries: architecture v3
Source version: 3. Effective 2026-09-20.
Payment retries Console depends on Payment retries Service.
Payment retries Worker depends on Payment retries Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Payment retries Service; rollout approval owner is not recorded.
