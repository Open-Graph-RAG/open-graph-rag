# Seat provisioning: architecture v3
Source version: 3. Effective 2026-09-20.
Seat provisioning Console depends on Seat provisioning Service.
Seat provisioning Worker depends on Seat provisioning Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Seat provisioning Service; rollout approval owner is not recorded.
