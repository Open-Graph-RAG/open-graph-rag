# Catalog publication: architecture v3
Source version: 3. Effective 2026-09-20.
Catalog publication Console depends on Catalog publication Service.
Catalog publication Worker depends on Catalog publication Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Catalog publication Service; rollout approval owner is not recorded.
