# Access review: architecture v3
Source version: 3. Effective 2026-09-20.
Access review Console depends on Access review Service.
Access review Worker depends on Access review Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Access review Service; rollout approval owner is not recorded.
