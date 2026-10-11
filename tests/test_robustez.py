"""Robustez y uso responsable: URLs, robots.txt, ritmo por host, perfil inválido, CLI y
reportes con contenido hostil. Ninguna prueba toca la red."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from radar_vacantes.boards import JobPosting
from radar_vacantes.cli import _gate, main
from radar_vacantes.engine import SearchStore
from radar_vacantes.evaluate import Profile, ProfileError, evaluate
from radar_vacantes.report import _detail, top_markdown
from radar_vacantes.sources import (
    HostThrottle,
    HttpError,
    Outcome,
    RobotsCache,
    RobotsDisallowedError,
    SourceSpec,
    UrllibHttp,
    check_url,
    read_source,
)
from radar_vacantes.verify import NO_VERIFICABLE, Check, verify

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = yaml.safe_load((ROOT / "config/perfil.ejemplo.yaml").read_text("utf-8"))
TODAY = date(2026, 10, 10)


def _posting(title: str = "Pricing Analyst", **kw: str) -> JobPosting:
    base: dict[str, str] = {
        "source": "greenhouse", "source_job_id": "1", "company": "Acme", "title": title,
        "location": "Bogotá, Colombia", "official_job_url": "https://example.com/1",
        "published_at": "2026-10-05", "first_seen_at": "",
    }
    base.update(kw)
    return JobPosting(**base)


# ------------------------------------------------------------------ URLs y cliente HTTP


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com/x", "/relativa", "javascript:alert(1)", ""]
)
def test_only_http_urls_are_opened(url: str) -> None:
    with pytest.raises(ValueError):
        check_url(url)
    # El cliente real rechaza la URL antes de abrir nada (ni red ni disco).
    with pytest.raises(ValueError):
        UrllibHttp(respect_robots=False).get_text(url)


def test_check_url_accepts_https() -> None:
    assert check_url("https://example.com/jobs?q=1") == "https://example.com/jobs?q=1"


def test_verify_never_reads_a_local_file_from_a_posting() -> None:
    p = _posting(official_job_url="file:///etc/passwd", source="lever")
    chk = verify(p, UrllibHttp(respect_robots=False), TODAY)
    assert chk.status == NO_VERIFICABLE


class _Robots:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def __call__(self, url: str) -> tuple[int, str]:
        self.calls.append(url)
        value = self.answers[url]
        if isinstance(value, BaseException):
            raise value
        return value


def test_robots_rules_are_respected_and_cached() -> None:
    fetch = _Robots({
        "https://a.example/robots.txt": (200, "User-agent: *\nDisallow: /privado/\n"),
        "https://b.example/robots.txt": (404, ""),
        "https://c.example/robots.txt": (503, ""),
        "https://d.example/robots.txt": TimeoutError(),
        "https://e.example/robots.txt": (200, "User-agent: radar-de-vacantes\nDisallow: /\n"),
    })
    robots = RobotsCache(fetch)
    assert robots.allowed("https://a.example/jobs")
    assert not robots.allowed("https://a.example/privado/1")
    assert robots.allowed("https://b.example/x")  # sin robots.txt: sin restricciones
    assert not robots.allowed("https://c.example/x")  # 5xx: no se lee en esta corrida
    assert not robots.allowed("https://d.example/x")  # red caída: tampoco
    assert not robots.allowed("https://e.example/x")  # regla para este agente
    assert fetch.calls.count("https://a.example/robots.txt") == 1


def test_client_refuses_paths_disallowed_by_robots() -> None:
    client = UrllibHttp()
    client.robots = RobotsCache(lambda _: (200, "User-agent: *\nDisallow: /\n"))
    with pytest.raises(RobotsDisallowedError):
        client.get_json("https://example.com/api/jobs")


def test_robots_block_is_reported_and_not_retried() -> None:
    calls = {"n": 0}

    class Blocked:
        def get_json(self, url: str) -> Any:
            calls["n"] += 1
            raise RobotsDisallowedError(url)

        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            raise NotImplementedError

        def get_text(self, url: str) -> str:
            raise NotImplementedError

    res = read_source(SourceSpec("a", "Acme", "greenhouse", "acme"), Blocked(), "x",
                      sleep=lambda _: None)
    assert res.outcome is Outcome.BLOCKED and "robots.txt" in res.detail
    assert calls["n"] == 1


def test_requests_to_the_same_host_are_spaced() -> None:
    now = {"t": 100.0}
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)

    throttle = HostThrottle(0.5, clock=lambda: now["t"], sleep=sleep)
    throttle.wait("https://a.example/1")
    throttle.wait("https://a.example/2")
    throttle.wait("https://b.example/1")
    assert slept == [0.5]


# ------------------------------------------------------------------ perfil


def test_example_profile_is_valid() -> None:
    p = Profile.from_config(EXAMPLE)
    assert p.years >= 0 and p.floor_monthly_cop > 0


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"years_relevant": None}, "years_relevant"),
        ({"years_relevant": "tres"}, "years_relevant"),
        ({"years_relevant": -1}, "years_relevant"),
        ({"english": None}, "english"),
        ({"tools": "excel"}, "tools"),
        ({"has_masters": "no"}, "has_masters"),
        ({"strengths": [{"pattern": "(sin cerrar", "reason": "x"}]}, "strengths[0].pattern"),
        ({"strengths": [{"pattern": "x"}]}, "strengths[0]"),
        ({"preferences": {"sector_points": {"pagos": "mucho"}}}, "sector_points.pagos"),
        ({"preferences": []}, "preferences"),
        ({"floor_monthly_cop": "6 millones"}, "floor_monthly_cop"),
    ],
)
def test_invalid_profile_names_the_field(change: dict[str, Any], message: str) -> None:
    cfg = {**EXAMPLE, **change}
    cfg = {k: v for k, v in cfg.items() if v is not None}
    with pytest.raises(ProfileError, match=message.replace("[", r"\[").replace("]", r"\]")):
        Profile.from_config(cfg)


def test_profile_must_be_a_mapping() -> None:
    with pytest.raises(ProfileError):
        Profile.from_config(["no", "es", "un", "dict"])


def test_salary_floor_comes_from_the_profile() -> None:
    p = _posting(salary_text="COP 4.000.000 - 5.000.000 mensual")
    low = Profile.from_config({**EXAMPLE, "floor_monthly_cop": 3_000_000})
    high = Profile.from_config({**EXAMPLE, "floor_monthly_cop": 8_000_000})
    none = Profile.from_config({**EXAMPLE, "floor_monthly_cop": 0})

    def note(profile: Profile) -> str:
        return evaluate(p, company_id="a", sector="", tier=2, profile=profile,
                        today=TODAY).salary_note

    assert "≥ COP 3,000,000" in note(low)
    assert "< COP 8,000,000" in note(high)
    assert "≥" not in note(none) and "<" not in note(none)


def test_trm_http_error_disables_the_gate_instead_of_crashing() -> None:
    class Down:
        def get_json(self, url: str) -> Any:
            raise HttpError(503)

        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            raise NotImplementedError

        def get_text(self, url: str) -> str:
            raise NotImplementedError

    assert _gate(Profile.from_config(EXAMPLE), Down()) is None


# ------------------------------------------------------------------ línea de comandos


def test_cli_missing_profile_explains_how_to_create_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match=r"perfil\.ejemplo\.yaml"):
        main(["buscar"])


def test_cli_rejects_broken_yaml_and_invalid_profile(tmp_path: Path) -> None:
    broken = tmp_path / "roto.yaml"
    broken.write_text("years_relevant: [3\n", "utf-8")
    with pytest.raises(SystemExit, match="no es YAML válido"):
        main(["demo", "--perfil", str(broken)])
    bad = tmp_path / "malo.yaml"
    bad.write_text(yaml.safe_dump({**EXAMPLE, "years_relevant": "muchos"}), "utf-8")
    with pytest.raises(SystemExit, match="Perfil inválido"):
        main(["demo", "--perfil", str(bad)])


def test_cli_rejects_non_positive_top(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["buscar", "--top", "0"])
    assert exc.value.code == 2
    assert "debe ser 1 o más" in capsys.readouterr().err


def test_demo_runs_offline(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["demo"]) == 0
    assert "No postular" in capsys.readouterr().out


def test_corrupt_history_is_reported(tmp_path: Path) -> None:
    runs = tmp_path / "search_runs"
    runs.mkdir()
    (runs / "2026-10-10T040000.json").write_text("{roto", "utf-8")
    with pytest.raises(ValueError, match="dañado"):
        SearchStore(tmp_path).previous_snapshot()
    (runs / "2026-10-11T040000.json").write_text(json.dumps({"snapshot": {"a": {}}}), "utf-8")
    assert SearchStore(tmp_path).previous_snapshot() == {"a": {}}


# ------------------------------------------------------------------ reportes


def test_report_escapes_hostile_text_and_links() -> None:
    hostile = _posting(
        title="<script>alert(1)</script>\n# Encabezado falso | celda",
        company="Acme <img src=x onerror=alert(1)>",
        official_job_url="javascript:alert(1)",
    )
    ev = evaluate(hostile, company_id="a", sector="", tier=2,
                  profile=Profile.from_config(EXAMPLE), today=TODAY)
    chk = Check(NO_VERIFICABLE, "<b>x</b>", hostile.official_job_url)
    md = top_markdown([(ev, chk)], verified="2026-10-10 04:00", discarded=[])
    assert "<script>" not in md and "<img" not in md and "<b>" not in md
    assert "javascript:" not in md and "sin enlace" in md
    assert "\n# Encabezado falso" not in md
    assert md.startswith("# Las 1 mejores vacantes verificadas")


def test_report_link_with_parentheses_stays_one_link() -> None:
    p = _posting(official_job_url="https://example.com/job/(123) x")
    ev = evaluate(p, company_id="a", sector="", tier=2, profile=Profile.from_config(EXAMPLE),
                  today=TODAY)
    text = "\n".join(_detail(1, ev, "hoy"))
    assert "https://example.com/job/%28123%29%20x" in text


# ------------------------------------------------------------------ salario y moneda


@pytest.mark.parametrize(
    ("text", "currency", "low", "interval"),
    [
        ("$4.500.000 - $5.000.000 mensual", "COP", 4_500_000, "monthly"),
        ("$4.500.000 COP mensual", "COP", 4_500_000, "monthly"),
        ("COP 4.500.000 al mes", "COP", 4_500_000, "monthly"),
        ("4.500.000 pesos por mes", "COP", 4_500_000, "monthly"),
        ("$80,000 - $100,000 per year", "USD", 80_000, "annual"),
        ("USD 3,000 per month", "USD", 3_000, "monthly"),
        ("$100k - $120k", "USD", 100_000, "annual"),
        ("US$ 25 per hour", "USD", 25, "hourly"),
        # Un año o una cifra antes de la moneda no entran en el rango.
        ("Salario 2026: COP 8.000.000 mensual", "COP", 8_000_000, "monthly"),
        ("COP 5.000.000 mensual (2026)", "COP", 5_000_000, "monthly"),
        ("4.500.000 - 6.000.000 COP mensual", "COP", 4_500_000, "monthly"),
    ],
)
def test_salary_currency_and_interval(text: str, currency: str, low: int, interval: str) -> None:
    from radar_vacantes.compensation import parse_salary

    rng = parse_salary(text)
    assert rng is not None
    assert (rng.currency, int(rng.low), rng.interval.value) == (currency, low, interval)


@pytest.mark.parametrize(
    "text",
    ["€50.000", "MXN 30,000", "30.000 pesos mexicanos al mes", "USD 3,000 o COP 12.000.000",
     "a convenir", ""],
)
def test_other_or_ambiguous_currencies_are_not_scored(text: str) -> None:
    from radar_vacantes.compensation import parse_salary

    assert parse_salary(text) is None


def test_peso_salary_with_dollar_sign_is_compared_in_cop() -> None:
    from decimal import Decimal

    from radar_vacantes.compensation import CompGate

    gate = CompGate(Decimal(5_500_000), Decimal(5_500_000), Decimal(4000), "2026-10-10")
    p = _posting(salary_text="$4.500.000 - $5.000.000 mensual")
    ev = evaluate(p, company_id="a", sector="", tier=2, profile=Profile.from_config(EXAMPLE),
                  today=TODAY, gate=gate)
    assert "≈ COP 4,500,000/mes, < COP 5,500,000/mes" in ev.salary_note


@pytest.mark.parametrize(
    ("level", "gap"),
    [("B2", True), ("B2+", True), ("intermedio", True), ("C1", False), ("C2 (IELTS 8)", False),
     ("nativo", False), ("bilingüe", False), ("C1 certificado", False), ("Avanzado (C1)", False),
     # Negaciones, metas y niveles mezclados: ante la duda, brecha.
     ("No fluido", True), ("Intermedio (no nativo)", True), ("Not fluent yet", True),
     ("B2+ (camino a C1)", True), ("B2, preparando C1", True), ("C1 en curso", True),
     ("Avanzado", True), ("IELTS 7.5", True)],
)
def test_english_gap_depends_on_the_declared_level(level: str, gap: bool) -> None:
    p = _posting(title="Financial Analyst", description=(
        "Requirements\nFluent English required\n2 years of experience in finance"))
    profile = Profile.from_config({**EXAMPLE, "english": level})
    ev = evaluate(p, company_id="a", sector="", tier=2, profile=profile, today=TODAY)
    assert any("inglés" in g for g in ev.gaps) is gap
    if not gap:
        assert ev.fit.value == "cumple completamente"


@pytest.mark.parametrize(
    ("published", "age", "recency"),
    [("2026-10-10", 0, "0 días"), ("2026-10-09", 1, "1 días"), ("2026-10-11", 0, "0 días"),
     ("2026-12-01", None, "sin fecha"), ("2026-02-30", None, "sin fecha"), ("", None, "sin fecha")],
)
def test_future_or_invalid_dates_are_not_recent(published: str, age: int | None,
                                                recency: str) -> None:
    ev = evaluate(_posting(published_at=published), company_id="a", sector="", tier=2,
                  profile=Profile.from_config(EXAMPLE), today=TODAY)
    assert (ev.age_days, ev.recency) == (age, recency)


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import radar_vacantes.cli as cli

    def refuse(**_: Any) -> None:
        raise AssertionError("se abrió la red antes de validar los argumentos")

    monkeypatch.setattr(cli, "UrllibHttp", refuse)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--seguimiento", "no-existe.md"], "No existe el archivo de seguimiento"),
        (["--zona", "Bogota"], "Zona horaria desconocida"),
    ],
)
def test_cli_bad_arguments_fail_before_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str], message: str
) -> None:
    _no_network(monkeypatch)
    monkeypatch.chdir(tmp_path)
    args = ["buscar", "--perfil", str(ROOT / "config/perfil.ejemplo.yaml"),
            "--universo", str(ROOT / "config/universo.yaml"), "--salida", str(tmp_path / "s")]
    with pytest.raises(SystemExit, match=message):
        main([*args, *extra])
    assert not (tmp_path / "s").exists()


def test_entry_track_and_cpg_sectors_come_from_the_profile() -> None:
    p = _posting(title="Associate", description="")

    def rel(cfg: dict[str, Any], sector: str) -> str:
        prof = Profile.from_config(cfg)
        return evaluate(p, company_id="a", sector=sector, tier=1, profile=prof,
                        today=TODAY).relevance.value

    assert rel(EXAMPLE, "consultoria") == "alta"  # valor por defecto del código
    off = {**EXAMPLE, "preferences": {**EXAMPLE["preferences"], "entry_track_sectors": {}}}
    assert rel(off, "consultoria") == "nula"
    mine = {**EXAMPLE, "preferences": {**EXAMPLE["preferences"],
                                       "entry_track_sectors": {"tecnologia": "entrada en tech"}}}
    assert rel(mine, "tecnologia") == "alta" and rel(mine, "consultoria") == "nula"
    prefs = EXAMPLE["preferences"]
    assert Profile.from_config(EXAMPLE).cpg_sectors is None
    custom = {**EXAMPLE, "preferences": {**prefs, "cpg_exception": {
        **prefs["cpg_exception"], "sectors": ["retail"]}}}
    assert Profile.from_config(custom).cpg_sectors == frozenset({"retail"})


@pytest.mark.parametrize(
    ("prefs", "message"),
    [({"entry_track_sectors": ["consultoria"]}, "entry_track_sectors"),
     ({"entry_track_sectors": {"consultoria": 3}}, "entry_track_sectors.consultoria"),
     ({"cpg_exception": {"sectors": "retail"}}, "cpg_exception.sectors")],
)
def test_invalid_track_preferences_name_the_field(prefs: dict[str, Any], message: str) -> None:
    with pytest.raises(ProfileError, match=message):
        Profile.from_config({**EXAMPLE, "preferences": prefs})



@pytest.mark.parametrize(
    ("level", "text", "gap"),
    [("B2", "C1", True), ("C1", "en progreso", False), ("native", "x", False), ("b1", "", True)],
)
def test_structured_english_level_wins_over_text(level: str, text: str, gap: bool) -> None:
    p = _posting(title="Financial Analyst", description=(
        "Requirements\nFluent English required\n2 years of experience in finance"))
    profile = Profile.from_config({**EXAMPLE, "english": text or "x", "english_level": level})
    ev = evaluate(p, company_id="a", sector="", tier=2, profile=profile, today=TODAY)
    assert any("inglés" in g for g in ev.gaps) is gap


@pytest.mark.parametrize("level", ["C3", "fluido", True, ["C1"]])
def test_invalid_english_level_names_the_field(level: Any) -> None:
    with pytest.raises(ProfileError, match="english_level"):
        Profile.from_config({**EXAMPLE, "english_level": level})
