"""The plant table in scripts/plants.py must keep matching the source.

A plant whose anchor drifted would silently test nothing; this makes the
drift a red test instead. Running the plants themselves is
`python scripts/plants.py` (slow: one pytest per guard).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("plants", ROOT / "scripts" / "plants.py")
assert _spec is not None and _spec.loader is not None
plants = importlib.util.module_from_spec(_spec)
sys.modules["plants"] = plants
_spec.loader.exec_module(plants)


@pytest.mark.parametrize("plant", plants.PLANTS, ids=lambda p: p.name)
def test_every_plant_anchor_occurs_exactly_once(plant: plants.Plant) -> None:
    text = (ROOT / plant.file).read_text()
    for old, _new in plant.edits:
        assert text.count(old) == 1, f"{plant.name}: anchor drifted in {plant.file}: {old!r}"


@pytest.mark.parametrize("plant", plants.PLANTS, ids=lambda p: p.name)
def test_every_plant_names_an_existing_test(plant: plants.Plant) -> None:
    path, _, name = plant.test.partition("::")
    assert f"def {name}(" in (ROOT / path).read_text(), f"{plant.name}: no test {plant.test}"


def test_apply_refuses_a_missing_or_ambiguous_anchor() -> None:
    with pytest.raises(LookupError):
        plants.apply("a b", (("zzz", "y"),))
    with pytest.raises(LookupError):
        plants.apply("a a", (("a", "y"),))
    assert plants.apply("a b", (("a", "y"),)) == "y b"


def test_verdicts_count_only_a_real_test_failure_as_red() -> None:
    """A broken environment must never read as 19 proven guards."""
    assert plants.baseline_verdict(0, "1 passed in 0.1s")[0] is True
    assert plants.baseline_verdict(1, "1 failed in 0.1s")[0] is False
    assert plants.baseline_verdict(2, "ERROR: file not found")[0] is False
    assert plants.mutant_verdict(1, "1 failed in 0.1s")[0] is True
    assert plants.mutant_verdict(0, "1 passed in 0.1s")[0] is False  # green: not proven
    assert plants.mutant_verdict(2, "ImportError while loading conftest")[0] is False
    assert plants.mutant_verdict(1, "No module named pytest")[0] is False  # exit 1, no summary
    assert plants.mutant_verdict(5, "no tests ran")[0] is False
