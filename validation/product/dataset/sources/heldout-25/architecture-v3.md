# Resource transfer: architecture v3
Source version: 3. Effective 2026-09-20.
Resource transfer Console depends on Resource transfer Service.
Resource transfer Worker depends on Resource transfer Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Resource transfer Service; rollout approval owner is not recorded.
