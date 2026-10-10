# Entitlement sync: architecture v3
Source version: 3. Effective 2026-09-20.
Entitlement sync Console depends on Entitlement sync Service.
Entitlement sync Worker depends on Entitlement sync Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Entitlement sync Service; rollout approval owner is not recorded.
