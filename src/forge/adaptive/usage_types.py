"""Immutable route identity shared by usage and closing-budget accounting."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderRoute:
    """The provider route one call/receipt was served through.

    ``provider`` is the route's owner (the driver id for harness receipts,
    the provider name for ``llm_calls`` rows); ``model`` the model id.
    Routes are the leaf of the join chain — usage aggregates name them so
    spend stays attributable after the fold.
    """

    provider: str
    model: str

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}" if self.model else self.provider

    def to_json(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model}
