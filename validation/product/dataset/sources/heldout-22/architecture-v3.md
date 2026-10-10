# Support escalation: architecture v3
Source version: 3. Effective 2026-09-20.
Support escalation Console depends on Support escalation Service.
Support escalation Worker depends on Support escalation Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Support escalation Service; rollout approval owner is not recorded.
