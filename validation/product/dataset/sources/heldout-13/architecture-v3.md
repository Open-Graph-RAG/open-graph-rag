# Consent renewal: architecture v3
Source version: 3. Effective 2026-09-20.
Consent renewal Console depends on Consent renewal Service.
Consent renewal Worker depends on Consent renewal Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Consent renewal Service; rollout approval owner is not recorded.
