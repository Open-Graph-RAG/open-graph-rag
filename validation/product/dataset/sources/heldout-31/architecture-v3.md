# Tenant split: architecture v3
Source version: 3. Effective 2026-09-20.
Tenant split Console depends on Tenant split Service.
Tenant split Worker depends on Tenant split Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Tenant split Service; rollout approval owner is not recorded.
