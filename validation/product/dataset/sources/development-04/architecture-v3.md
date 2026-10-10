# Usage metering: architecture v3
Source version: 3. Effective 2026-09-20.
Usage metering Console depends on Usage metering Service.
Usage metering Worker depends on Usage metering Service.
The Analytics Archive consumes weekly exports and is not a synchronous dependency.
The proposed API change affects callers of Usage metering Service; rollout approval owner is not recorded.
