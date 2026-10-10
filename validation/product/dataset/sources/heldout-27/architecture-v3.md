# Discount approval: architecture v3
Source version: 3. Effective 2026-09-20.
Discount approval Console depends on Discount approval Service.
Discount approval Worker depends on Discount approval Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Discount approval Service; rollout approval owner is not recorded.
