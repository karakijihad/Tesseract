"""AU-4 — agenda.yaml round-trip + the ConfigError path stays loud."""

from __future__ import annotations

from pathlib import Path

from tesseract.orchestrator.autonomy.agenda_store import load_weights_from_yaml
from tesseract.orchestrator.autonomy.models import AgendaSource


def test_load_weights_from_packaged_yaml() -> None:
    """The shipped tesseract/config/agenda.yaml must load with the
    documented defaults — operator-edit drift would otherwise break
    the production AgendaStore on first boot."""
    pkg = Path(__file__).resolve().parents[2]  # tesseract/ package
    weights = load_weights_from_yaml(pkg / "config" / "agenda.yaml")
    assert weights.operator_priority_weight == 50.0
    assert weights.age_weight == 1.0
    assert weights.risk_weight == -10.0
    assert weights.budget_remaining_weight == 5.0
    assert weights.source_trust_weight == 8.0
    assert weights.age_cap_hours == 168.0
    assert weights.source_trust[AgendaSource.OPERATOR] == 1.0
    assert weights.source_trust[AgendaSource.RECOVERY] == 0.95


def test_load_weights_falls_back_to_defaults(tmp_path: Path) -> None:
    """Missing keys must drop to dataclass defaults — operator can omit
    any tunable without breaking the boot."""
    cfg = tmp_path / "agenda.yaml"
    cfg.write_text("scoring:\n  age_weight: 5.0\n", encoding="utf-8")
    weights = load_weights_from_yaml(cfg)
    assert weights.age_weight == 5.0
    # Other weights keep their defaults.
    assert weights.operator_priority_weight == 50.0
    assert weights.risk_weight == -10.0


def test_yaml_covers_every_agenda_source() -> None:
    """Every concrete `AgendaSource` value must have a row in the
    shipped `agenda.yaml::source_trust` block. Drift here means a new
    source falls through to ``trust_map`` defaults silently — operator
    can't tune what they can't see."""
    pkg = Path(__file__).resolve().parents[2]
    weights = load_weights_from_yaml(pkg / "config" / "agenda.yaml")
    # Reading raw yaml directly so we assert the *file* (not the
    # code-default fallback) carries every source.
    import yaml
    raw = yaml.safe_load((pkg / "config" / "agenda.yaml").read_text(encoding="utf-8"))
    yaml_sources = set((raw.get("source_trust") or {}).keys())
    missing = [s.value for s in AgendaSource if s.value not in yaml_sources]
    assert not missing, (
        f"agenda.yaml::source_trust missing rows for {missing}; "
        f"every AgendaSource must be tunable from YAML."
    )
    # Sanity: the strategist row added in AU-23 reconciliation lands.
    assert weights.source_trust[AgendaSource.STRATEGIST] == 0.65


def test_load_weights_ignores_unknown_source(tmp_path: Path) -> None:
    cfg = tmp_path / "agenda.yaml"
    cfg.write_text(
        "source_trust:\n  invented_source: 0.5\n  operator: 0.95\n",
        encoding="utf-8",
    )
    weights = load_weights_from_yaml(cfg)
    assert weights.source_trust[AgendaSource.OPERATOR] == 0.95
