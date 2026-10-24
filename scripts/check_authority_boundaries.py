"""Read-only exact current owner/caller and store-boundary coverage validation."""

import argparse
from pathlib import Path
import tomllib

import grimp

parser = argparse.ArgumentParser()
parser.add_argument("config", type=Path)
args = parser.parse_args()
config = tomllib.loads(args.config.read_text())["tool"]["importlinter"]
contracts = config["contracts"]
protected = [item for item in contracts if item["type"] == "protected"]
assert len(protected) == 10, "all ten authority owners must remain protected"
assert all(not item.get("ignore_imports") for item in contracts), "ignored import debt refused"
assert all(not item.get("ignore_imports") for item in [config]), (
    "global ignored import debt refused"
)
graph = grimp.build_graph(*config["root_packages"], include_external_packages=True, cache_dir=None)
for contract in protected:
    assert contract["as_packages"] is False, "authority ownership must stay module-exact"
    owners, allowed = contract["protected_modules"], contract["allowed_importers"]
    assert all("*" not in name for name in owners + allowed), "wildcard authority refused"
    current = set().union(*(graph.find_modules_that_directly_import(name) for name in owners))
    assert current == set(allowed), (
        contract["id"],
        "new callers need explicit review",
        sorted(current - set(allowed)),
        "remove stale allowances",
        sorted(set(allowed) - current),
    )
store = next(item for item in contracts if item["id"] == "store-transaction-boundary")
values = {"forge.engine.plan_values", "forge.engine.lane_values"}
implementation = {name for name in graph.modules if name.startswith("forge.engine.")} - values
covered = {name for name in store["forbidden_modules"] if name.startswith("forge.engine.")}
assert implementation == covered, (
    "store implementation barrier must cover every current engine implementation",
    sorted(implementation - covered),
    sorted(covered - implementation),
)
print("10 exact protected owners/callers, zero ignore debt, complete store implementation barrier")
