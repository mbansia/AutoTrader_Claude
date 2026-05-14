"""Diagnostics. Frozen JSON contract per docs/SYSTEM.md §8.1; rules per §8.2."""

from .anomalies import detect_anomalies
from .endpoint import build_snapshot
