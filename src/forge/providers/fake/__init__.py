"""In-memory provider for deterministic contracts, never native/live acceptance."""

from forge.providers.fake.read import FakeReads
from forge.providers.fake.state import Blob, Fault
from forge.providers.fake.write import FakeWrites


class FakeProvider(FakeReads, FakeWrites):
    """One connection's native state; faults are consumed once in declared order."""


__all__ = ["Blob", "FakeProvider", "Fault"]
