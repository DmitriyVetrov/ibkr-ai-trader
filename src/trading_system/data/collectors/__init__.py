"""Scheduled collection jobs writing to data/raw, data/normalized and snapshots.

A collector runs repeatedly and accumulates. Running one twice over an
unchanged provider response records that we looked again; it does not create a
second copy of the same history, and it never overwrites what is already
stored.
"""

from trading_system.data.collectors.base import (
    CollectionReport,
    DataCollector,
    detect_gap,
)
from trading_system.data.collectors.pipeline import RegistryCollector

__all__ = [
    "CollectionReport",
    "DataCollector",
    "RegistryCollector",
    "detect_gap",
]
