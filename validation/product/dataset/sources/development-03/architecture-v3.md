# Identity migration: architecture v3
Source version: 3. Effective 2026-09-20.
Identity migration Console depends on Identity migration Service.
Identity migration Worker depends on Identity migration Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Identity migration Service; rollout approval owner is not recorded.
