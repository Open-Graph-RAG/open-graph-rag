# Partner onboarding: architecture v3
Source version: 3. Effective 2026-09-20.
Partner onboarding Console depends on Partner onboarding Service.
Partner onboarding Worker depends on Partner onboarding Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Partner onboarding Service; rollout approval owner is not recorded.
