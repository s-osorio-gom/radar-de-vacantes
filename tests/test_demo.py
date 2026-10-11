"""La demo es el contrato visible del motor: cada vacante sintética ilustra una trampa."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import yaml

from radar_vacantes.boards import JobPosting
from radar_vacantes.evaluate import Fit, Profile, evaluate

ROOT = Path(__file__).resolve().parents[1]
PROFILE = Profile.from_config(yaml.safe_load((ROOT / "config/perfil.ejemplo.yaml").read_text()))


def _by_id() -> dict[str, object]:
    rows = json.loads((ROOT / "ejemplo/vacantes.json").read_text("utf-8"))
    out: dict[str, object] = {}
    for r in rows:
        p = JobPosting(r["fuente"], r["id"], r["empresa"], r["cargo"], r["ciudad"], r["url"],
                       r["publicada"], "", description=r["descripcion"])
        out[r["id"]] = evaluate(p, company_id=r["id"], sector=r["sector"], tier=r["tier"],
                                profile=PROFILE, today=date(2026, 10, 10))
    return out


def test_each_trap_is_caught() -> None:
    ev = _by_id()
    assert ev["a1"].next_step.startswith("No postular")              # type: ignore[attr-defined]
    assert ev["a2"].fit is Fit.COMPLETO                               # type: ignore[attr-defined]
    assert any("posgrado" in g for g in ev["a3"].gaps)                # type: ignore[attr-defined]
    assert any("certificación" in g for g in ev["a4"].gaps)           # type: ignore[attr-defined]
    assert ev["a5"].fit is Fit.BAJO                                   # type: ignore[attr-defined]
    assert ev["a6"].next_step.startswith("No postular")              # type: ignore[attr-defined]
    assert ev["a7"].fit is Fit.COMPLETO                               # type: ignore[attr-defined]
    assert ev["a8"].fit is Fit.SIN_DATOS                              # type: ignore[attr-defined]


def test_universe_config_loads() -> None:
    from radar_vacantes.engine import load_universe

    cfg = yaml.safe_load((ROOT / "config/universo.yaml").read_text("utf-8"))
    companies = load_universe(cfg, cfg)
    assert len(companies) > 100
    # Algunas empresas se declaran sin fuente: quedan para `radar descubrir`.
    with_source = [c for c in companies if c.sources or c.pending_sources]
    assert len(with_source) >= 0.9 * len(companies)


def test_demo_files_ship_in_the_wheel() -> None:
    """Instalado sin clonar, la demo lee la copia empaquetada: el wheel debe traer los mismos
    archivos que la CLI busca."""
    import tomllib

    from radar_vacantes import cli

    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    shipped = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    for source, target in shipped.items():
        assert (ROOT / source).is_file()
        assert target == f"radar_vacantes/_ejemplo/{Path(source).name}"
    names = {Path(s).name for s in shipped}
    assert {cli.DEMO_VACANCIES.name, cli.EXAMPLE_PROFILE.name} <= names
