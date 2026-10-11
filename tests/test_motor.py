"""Pruebas del motor de búsqueda ampliado. Sin red: cliente HTTP falso."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from radar_vacantes.boards import JobPosting
from radar_vacantes.engine import Company, dedupe, diff, run_search
from radar_vacantes.evaluate import Authorization, Fit, Profile, Relevance, evaluate
from radar_vacantes.sources import (
    HttpError,
    Outcome,
    SourceSpec,
    parse_human_date,
    read_source,
    workday_posted,
)
from radar_vacantes.verify import ABIERTA, CERRADA, verify

PROFILE = Profile(
    years=3,
    english="B2+",
    tools=frozenset({"excel", "python", "sql", "power bi"}),
    certifications=frozenset(),
    has_masters=False,
    degree="Economía",
    strengths=((r"\bpricing\b", "fijó precios"),),
)
TODAY = date(2026, 10, 10)


class FakeHttp:
    """Respuestas por URL; una excepción en el mapa se lanza."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def _get(self, url: str) -> Any:
        self.calls.append(url)
        for key, value in self.responses.items():
            if key in url:
                if isinstance(value, BaseException):
                    raise value
                return value
        raise HttpError(404)

    def get_json(self, url: str) -> Any:
        return self._get(url)

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        return self._get(f"{url}#offset={body['offset']}")

    def get_text(self, url: str) -> str:
        return str(self._get(url))


def posting(
    title: str,
    *,
    location: str = "Bogotá, Colombia",
    desc: str = "",
    source: str = "greenhouse",
    company: str = "Acme",
    jid: str = "1",
    published: str = "2026-10-05",
) -> JobPosting:
    return JobPosting(
        source,
        jid,
        company,
        title,
        location,
        f"https://x/{jid}",
        published,
        "",
        board_slug="acme",
        description=desc,
    )


# ------------------------------------------------------------------ descubrimiento


def test_error_blocked_and_empty_are_distinct() -> None:
    spec = SourceSpec("a", "Acme", "greenhouse", "acme")
    ok_empty = read_source(
        spec, FakeHttp({"boards/acme": {"jobs": []}}), "2026-10-10T00:00:00+00:00"
    )
    assert ok_empty.outcome is Outcome.EMPTY
    blocked = read_source(
        spec, FakeHttp({"boards/acme": HttpError(403, "captcha")}), "x", retries=0
    )
    assert blocked.outcome is Outcome.BLOCKED
    gone = read_source(spec, FakeHttp({"boards/acme": HttpError(404)}), "x", retries=0)
    assert gone.outcome is Outcome.NOT_FOUND
    down = read_source(
        spec, FakeHttp({"boards/acme": TimeoutError()}), "x", retries=1, sleep=lambda _: None
    )
    assert down.outcome is Outcome.ERROR and down.detail.endswith("(tras 1 reintento)")


def test_transient_error_is_retried() -> None:
    calls = {"n": 0}

    class Flaky(FakeHttp):
        def get_json(self, url: str) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError
            return {"jobs": [{"id": 1, "title": "Pricing Analyst", "location": {"name": "Bogotá"}}]}

    res = read_source(
        SourceSpec("a", "Acme", "greenhouse", "acme"), Flaky({}), "x", sleep=lambda _: None
    )
    assert res.outcome is Outcome.OK and calls["n"] == 2


def test_smartrecruiters_paginates() -> None:
    def page(ids: range) -> dict[str, Any]:
        return {
            "totalFound": 150,
            "content": [
                {"id": str(i), "name": f"Analyst {i}", "location": {"city": "Bogotá"}} for i in ids
            ],
        }

    http = FakeHttp({"offset=0": page(range(100)), "offset=100": page(range(100, 150))})
    res = read_source(SourceSpec("a", "Acme", "smartrecruiters", "Acme"), http, "x")
    assert len(res.postings) == 150 and res.pages == 2


def test_workday_paginates_and_dates() -> None:
    def page(n: int, start: int) -> dict[str, Any]:
        return {
            "total": 25,
            "jobPostings": [
                {
                    "title": f"Finance Analyst {i}",
                    "externalPath": f"/job/{i}",
                    "locationsText": "Bogota",
                    "postedOn": "Posted 3 Days Ago",
                }
                for i in range(start, start + n)
            ],
        }

    http = FakeHttp({"#offset=0": page(20, 0), "#offset=20": page(5, 20)})
    spec = SourceSpec("a", "Acme", "workday", "https://acme.wd1.myworkdayjobs.com/Ext")
    res = read_source(spec, http, "2026-10-10T05:00:00+00:00")
    assert len(res.postings) == 25
    assert res.postings[0].published_at == "2026-10-07"


def test_relative_and_human_dates_never_invented() -> None:
    assert workday_posted("Posted Today", TODAY) == "2026-10-10"
    assert workday_posted("Posted 30+ Days Ago", TODAY) == ""
    assert parse_human_date("10 oct 2026") == "2026-10-10"
    assert parse_human_date("October 9, 2026") == "2026-10-09"
    assert parse_human_date("hace un rato") == ""


# ------------------------------------------------------------------ dedupe e historial


def test_dedupe_prefers_original_portal() -> None:
    official = posting("Pricing Analyst", source="workday", jid="w1")
    aggregator = posting("Pricing Analyst", source="indeed", jid="i1")
    unique, alt = dedupe([aggregator, official])
    assert len(unique) == 1 and unique[0].source == "workday"
    assert list(alt.values()) == [["https://x/i1"]]


def test_closures_only_from_sources_read_fully() -> None:
    prev = {
        "a|1": {"source_key": "gh:a", "title": "x"},
        "b|1": {"source_key": "wd:b", "title": "y"},
    }
    out = diff(prev, {}, read_sources={"gh:a"})
    assert out["closed"] == ["a|1"]  # la fuente caída (wd:b) no cierra nada


def test_truncated_list_does_not_close(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [
        {"url": f"https://www.magneto365.com/co/empresas/acme/empleos/analista-{i}-10000{i:02d}"}
        for i in range(20)
    ]
    html = (
        '<script id="ItemList" type="application/ld+json">{"itemListElement": '
        + str(jobs).replace("'", '"')
        + "}</script>"
    )
    company = Company(
        "acme",
        "Acme",
        "banca",
        "CO",
        2,
        (),
        "",
        (SourceSpec("acme", "Acme", "magneto", "acme"),),
        (),
    )
    prev = {"magneto|acme|99999": {"source_key": "magneto:acme", "title": "old"}}
    run = run_search(
        [company],
        FakeHttp({"empresas/acme/empleos": html}),
        profile=PROFILE,
        today=TODAY,
        seen_at="x",
        stamp="s",
        previous=prev,
    )
    assert run.history["closed"] == []


# ------------------------------------------------------------------ evaluación


def test_title_variants_are_not_discarded() -> None:
    e = evaluate(
        posting("Analista de Precios y Rentabilidad"),
        company_id="a",
        sector="",
        tier=2,
        profile=PROFILE,
        today=TODAY,
    )
    assert e.relevance is Relevance.ALTA


def test_functional_exclusion_beats_strategy_word() -> None:
    e = evaluate(
        posting("Profesional Compras y Contratación Estrategia"),
        company_id="a",
        sector="",
        tier=2,
        profile=PROFILE,
        today=TODAY,
    )
    assert e.relevance is Relevance.NULA


def test_consulting_entry_track_is_relevant() -> None:
    e = evaluate(
        posting("Associate, Colombia 2027"),
        company_id="consultora",
        sector="consultoria",
        tier=1,
        profile=PROFILE,
        today=TODAY,
    )
    assert e.relevance is Relevance.ALTA


def test_hard_requirements_found_outside_sections() -> None:
    desc = (
        "Overview of the role.\nAdvanced university degree in Economics, plus a minimum of "
        "four (4) years of relevant professional experience is required.\nResponsibilities\nX"
    )
    e = evaluate(
        posting("Economist", desc=desc),
        company_id="a",
        sector="",
        tier=1,
        profile=PROFILE,
        today=TODAY,
    )
    assert e.fit is not Fit.COMPLETO
    assert any("posgrado" in g for g in e.gaps) and any("4+" in g for g in e.gaps)


def _ev(title: str, desc: str, *, sector: str = "", tier: int = 1) -> Any:
    return evaluate(
        posting(title, desc=desc),
        company_id="a",
        sector=sector,
        tier=tier,
        profile=PROFILE,
        today=TODAY,
    )


def test_no_prior_experience_programs_are_not_eligible() -> None:
    """Programa de entrada de un banco global: con experiencia previa, no procede."""
    e = _ev(
        "Graduate Program - Markets Sales",
        "In Colombia, these opportunities are open to undergraduate students and recent "
        "graduates with no prior work experience.",
        sector="banca_global",
    )
    assert e.fit is Fit.BAJO
    assert e.gaps[0].startswith("no elegible")
    assert e.next_step.startswith("No postular")
    ok = _ev("Analista", "Aplican personas con o sin experiencia laboral.\nExcel avanzado.")
    assert not any(g.startswith("no elegible") for g in ok.gaps)


def test_experience_ceiling_is_not_a_minimum() -> None:
    """Consultora: "Up to 4 years" admite a quien tiene 3."""
    desc = "What You'll Bring\nUp to 4 years of professional experience\nA bachelor's degree"
    e = _ev("Associate, Colombia 2027", desc, sector="consultoria")
    assert not any("4+" in g for g in e.gaps)
    assert e.fit is Fit.COMPLETO
    over = _ev("Associate", "Requirements\nMaximum 2 years of work experience")
    assert any("hasta 2 años" in g for g in over.gaps)


def test_degree_leading_the_line_is_required() -> None:
    """Organismo internacional: sin la palabra "required" sigue siendo exigencia."""
    desc = (
        "Ideal Candidate Profile\nAn advanced university degree in finance, economics or a "
        "related field with a strong quantitative focus.\nA minimum of one year of experience."
    )
    e = _ev("Junior Economist", desc, sector="multilateral")
    assert any("posgrado" in g for g in e.gaps)
    soft = _ev("Analyst", "Requirements\nA master's degree is preferred.\nExcel")
    assert not any("posgrado" in g for g in soft.gaps)


def test_generic_certification_and_track_record_are_gaps() -> None:
    """Certificación genérica (consultora) e historial de inversiones (banco de desarrollo)."""
    consultora = _ev(
        "Consultor(a) Snr Strategy & Business Design",
        "¿Qué se necesita para aplicar?\n• Certificación en Finanzas, Data Science, Gestión de "
        "Proyectos o Agile, PMI, PMO.\n• Inglés intermedio.",
        sector="consultoria",
    )
    assert any("certificación" in g for g in consultora.gaps)
    assert consultora.fit is not Fit.COMPLETO
    inversion = _ev(
        "Associate Investment Officer",
        "The ideal candidate will have a demonstrated track record of executing debt and/or "
        "equity investments, preferably across MAS sectors.",
        sector="multilateral",
    )
    assert any("historial" in g for g in inversion.gaps)
    assert inversion.fit is Fit.BAJO
    advisor = _ev("Macroeconomic Frameworks Resident Advisor", "Requirements\nExcel",
                  sector="multilateral")
    assert advisor.fit is Fit.BAJO


def test_must_and_nice_are_separated() -> None:
    desc = (
        "Requirements\n• 2+ years of experience in pricing\n• Advanced Excel\n"
        "Preferred qualifications\n• Tableau\n• MBA"
    )
    e = evaluate(
        posting("Pricing Analyst", desc=desc),
        company_id="a",
        sector="",
        tier=2,
        profile=PROFILE,
        today=TODAY,
    )
    assert e.fit is Fit.COMPLETO
    assert any("Tableau" in n for n in e.nice) and not any("Tableau" in m for m in e.must)


def test_authorization_is_never_assumed() -> None:
    def auth(location: str, desc: str = "", sector: str = "") -> Authorization:
        return evaluate(
            posting("Finance Analyst", location=location, desc=desc),
            company_id="a",
            sector=sector,
            tier=1,
            profile=PROFILE,
            today=TODAY,
        ).authorization

    assert auth("Bogotá, Colombia") is Authorization.COLOMBIA
    assert auth("Remote - LATAM") is Authorization.REMOTO
    assert auth("Remote - Estonia") is Authorization.SIN_INFO
    assert auth("New York", "Must be authorized to work in the US") is Authorization.REQUIERE
    assert auth("New York", "Visa sponsorship is available") is Authorization.PATROCINIO
    assert auth("Washington DC", sector="multilateral") is Authorization.MULTILATERAL


def test_missing_salary_and_date_do_not_penalize_or_invent() -> None:
    e = evaluate(
        posting("Pricing Analyst", published=""),
        company_id="a",
        sector="",
        tier=2,
        profile=PROFILE,
        today=TODAY,
    )
    assert e.recency == "sin fecha" and e.salary_note == "no publicado" and e.age_days is None


# ------------------------------------------------------------------ verificación


def test_verify_uses_structured_data_not_hidden_strings() -> None:
    page = (
        '<script>{"404":"La oferta ya no está disponible"}</script>'
        '<script type="application/ld+json">{"@type":"JobPosting","validThrough":"2026-10-31"}'
        "</script>"
    )
    chk = verify(posting("Analista", source="magneto"), FakeHttp({"x/1": page}), TODAY)
    assert chk.status == ABIERTA
    expired = page.replace("2026-10-31", "2026-10-01")
    assert (
        verify(posting("Analista", source="magneto"), FakeHttp({"x/1": expired}), TODAY).status
        == CERRADA
    )
    assert verify(posting("Analista"), FakeHttp({"x/1": HttpError(404)}), TODAY).status == CERRADA


class PhenomHttp:
    """Fake del POST /widgets de Phenom: lista y detalle."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def get_json(self, url: str) -> Any:
        raise HttpError(404)

    def get_text(self, url: str) -> str:
        raise HttpError(404)

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        self.bodies.append(body)
        if body["ddoKey"] == "jobDetail":
            return {"jobDetail": {"data": {"job": {
                "description": "<p>Up to 4 years of professional experience</p>"}}}}
        return {"refineSearch": {"totalHits": 1, "data": {"jobs": [{
            "jobId": "59593", "title": "Associate, Colombia 2027", "city": "Bogota",
            "country": "Colombia", "postedDate": "2026-09-24T00:00:00.000+0000",
            "descriptionTeaser": "teaser"}]}}}


def test_phenom_reads_list_and_detail() -> None:
    from radar_vacantes.sources import read_phenom

    http = PhenomHttp()
    spec = SourceSpec(
        "acme", "Acme", "phenom", "https://careers.example.com/global/en", ("Colombia",)
    )
    r = read_phenom(spec, http, "2026-10-10")
    assert r.outcome is Outcome.OK and len(r.postings) == 1
    p = r.postings[0]
    assert p.published_at == "2026-09-24" and "Up to 4 years" in p.description
    assert p.official_job_url == "https://careers.example.com/global/en/job/59593/Associate-Colombia-2027"
    assert http.bodies[0]["selected_fields"] == {"country": ["Colombia"]}


def test_no_published_requirements_is_not_full_fit() -> None:
    """Organismo internacional: el listado solo dice "T6. Closes Oct 11, 2026"."""
    e = _ev("Junior Economist – Capital Markets", "T6. Closes Oct 11, 2026", sector="multilateral")
    assert e.fit is Fit.SIN_DATOS
    wb = evaluate(
        posting("Investment Analyst", desc="Analyze financial statements and prepare financial "
                "models. Work across industries.", source="cornerstone"),
        company_id="wb", sector="multilateral", tier=1, profile=PROFILE, today=TODAY,
    )
    assert wb.fit is Fit.SIN_DATOS


def test_summary_source_needs_a_real_requirements_header() -> None:
    desc = ("Coordinate platforms applying tagging, clearance requirements. "
            "Support events. Education sector work.")
    wb = evaluate(posting("Investment Analyst", desc=desc, source="cornerstone"),
                  company_id="wb", sector="multilateral", tier=1, profile=PROFILE, today=TODAY)
    assert wb.fit is Fit.SIN_DATOS


def test_old_postings_lose_points() -> None:
    fresh = evaluate(posting("Pricing Analyst", published="2026-10-01"), company_id="a",
                     sector="", tier=2, profile=PROFILE, today=TODAY)
    stale = evaluate(posting("Pricing Analyst", published="2025-03-10"), company_id="a",
                     sector="", tier=2, profile=PROFILE, today=TODAY)
    assert stale.score < fresh.score - 15 and "aviso viejo" in stale.recency


def test_applied_jobs_leave_the_top() -> None:
    from radar_vacantes.report import already_tracked, tracked_from_seguimiento

    seg = ("| Empresa | Puesto | Estado |\n|---|---|---|\n"
           "| Pagos Ejemplo | Senior Analyst, Commercial Strategy & Product Monetization "
           "(R-000001) | Enviada |\n"
           "| Banco Ejemplo | Corporate Banking Full-Time Analyst, Bogotá 2027 | Lista |")
    tracked = tracked_from_seguimiento(seg)

    def ev(company: str, title: str) -> Any:
        return evaluate(posting(title, company=company), company_id="x", sector="", tier=1,
                        profile=PROFILE, today=TODAY)

    assert already_tracked(ev("Pagos Ejemplo", "Senior Analyst, Commercial Strategy & Product "
                              "Monetization"), tracked)
    assert already_tracked(ev("Banco Ejemplo", "Banking – Corporate Banking, Full-Time Analyst, "
                              "Bogotá – Colombia, 2027"), tracked)
    assert not already_tracked(ev("Banco Ejemplo", "Services, Full-Time Analyst, Bogotá 2027"),
                               tracked)
    assert not already_tracked(ev("Pagos Ejemplo", "Consultant, Advisors"), tracked)


def test_past_year_programs_are_expired() -> None:
    e = _ev("2025 LATAM Markets Analyst Program - Colombia", "Requirements\nExcel",
            sector="banca_global")
    assert any("2025" in g for g in e.gaps)
    now = _ev("Associate, Colombia 2027", "Requirements\nExcel", sector="consultoria")
    assert not any("ya pasado" in g for g in now.gaps)
