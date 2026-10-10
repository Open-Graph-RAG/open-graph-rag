# Incident routing: architecture v3
Source version: 3. Effective 2026-09-20.
Incident routing Console depends on Incident routing Service.
Incident routing Worker depends on Incident routing Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Incident routing Service; rollout approval owner is not recorded.
