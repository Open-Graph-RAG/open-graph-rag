# Reporting migration: architecture v3
Source version: 3. Effective 2026-09-20.
Reporting migration Console depends on Reporting migration Service.
Reporting migration Worker depends on Reporting migration Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Reporting migration Service; rollout approval owner is not recorded.
