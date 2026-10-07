from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_hardware_sizing_is_linked_and_estimate_boundary_is_visible():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    sizing = (ROOT / "docs" / "hardware-sizing.md").read_text(encoding="utf-8")
    assert "docs/hardware-sizing.md" in readme
    assert "ESTIMATED_ONLY" in readme
    assert "PHYSICAL_HARDWARE" in sizing
    assert "complete CUT" in sizing


def test_architecture_does_not_claim_unmeasured_solar_viability():
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert "Solar viable for field deployment" not in architecture
    assert "Estimate only" in architecture
