# Price revision: architecture v3
Source version: 3. Effective 2026-09-20.
Price revision Console depends on Price revision Service.
Price revision Worker depends on Price revision Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Price revision Service; rollout approval owner is not recorded.
