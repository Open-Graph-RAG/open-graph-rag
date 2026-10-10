# Contract extension: architecture v3
Source version: 3. Effective 2026-09-20.
Contract extension Console depends on Contract extension Service.
Contract extension Worker depends on Contract extension Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Contract extension Service; rollout approval owner is not recorded.
