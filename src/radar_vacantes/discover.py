"""Descubrimiento de fuentes: detecta el sistema de vacantes de una empresa.

Lee la página oficial de empleo (GET, sin sesión) y busca en el HTML los enlaces a los
sistemas de selección conocidos. Una detección es una **propuesta**: entra al universo solo
después de que `read_source` la lea con éxito.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from radar_vacantes.sources import (
    METHOD_ORACLE,
    METHOD_WORKDAY,
    Http,
    Outcome,
    SourceResult,
    SourceSpec,
    classify_error,
    read_page,
)


@dataclass(frozen=True, slots=True)
class Detection:
    """Sistema de vacantes detectado en una página."""

    method: str
    ref: str
    evidence: str


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        METHOD_WORKDAY,
        re.compile(r"https://[\w-]+\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[\w-]+"),
    ),
    (
        METHOD_ORACLE,
        re.compile(
            r"https://[\w.-]+\.oraclecloud\.com/hcmUI/CandidateExperience/[a-z]{2}(?:-[A-Z]{2})?/sites/[\w-]+"
        ),
    ),
    (
        "greenhouse",
        re.compile(
            r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)"
        ),
    ),
    ("lever", re.compile(r"jobs\.lever\.co/([\w.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.%-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([\w-]+)")),
    ("recruitee", re.compile(r"https://([\w-]+)\.recruitee\.com")),
)
#: Sistemas reconocibles sin conector de lectura (quedan como revisión manual documentada).
_KNOWN_NO_API: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("successfactors", re.compile(r"successfactors\.(?:com|eu)|jobs\.sap\.com|sapsf", re.I)),
    ("cornerstone", re.compile(r"\.csod\.com", re.I)),
    ("phenom", re.compile(r"phenompeople|phenom\.com", re.I)),
    ("avature", re.compile(r"avature\.net", re.I)),
    ("taleo", re.compile(r"taleo\.net", re.I)),
    ("icims", re.compile(r"icims\.com", re.I)),
    ("eightfold", re.compile(r"eightfold\.ai", re.I)),
    ("gupy", re.compile(r"gupy\.io", re.I)),
    ("magneto", re.compile(r"magneto365\.com", re.I)),
    ("teamtailor", re.compile(r"teamtailor\.com", re.I)),
    ("hiringroom", re.compile(r"hiringroom\.com", re.I)),
    ("computrabajo", re.compile(r"computrabajo\.com", re.I)),
    ("elempleo", re.compile(r"elempleo\.com", re.I)),
)
_IGNORED_SLUGS = frozenset({"embed", "v1", "jobs", "api", "careers", "job_app", "static"})


def detect(html: str) -> tuple[list[Detection], list[str]]:
    """Detecciones con conector y nombres de sistemas reconocidos sin API legible."""
    found: dict[str, Detection] = {}
    for method, pattern in _PATTERNS:
        for m in pattern.finditer(html):
            ref = m.group(1) if m.groups() else m.group(0)
            if ref.lower() in _IGNORED_SLUGS:
                continue
            found.setdefault(f"{method}:{ref}", Detection(method, ref, m.group(0)[:200]))
    others = sorted({name for name, p in _KNOWN_NO_API if p.search(html)})
    return list(found.values()), others


def probe_careers_page(
    company_id: str, company: str, url: str, client: Http
) -> tuple[SourceResult, list[Detection], list[str]]:
    """Abre la página de empleo y devuelve su acceso y lo que se detectó en ella."""
    spec = SourceSpec(company_id, company, "page", url)
    try:
        html = client.get_text(url)
    except Exception as exc:  # noqa: BLE001 — cualquier fallo queda como resultado explícito
        outcome, detail = classify_error(exc)
        return SourceResult(spec, outcome, detail=detail), [], []
    access = read_page(spec, _Static(html), "")
    dets, others = detect(html)
    if access.outcome is Outcome.MANUAL and (dets or others):
        access.detail = "accesible; sistema detectado"
    return access, dets, others


class _Static:
    """Cliente que devuelve un HTML ya descargado (para reutilizar `read_page`)."""

    def __init__(self, html: str) -> None:
        self.html = html

    def get_json(self, url: str) -> object:
        raise NotImplementedError

    def post_json(self, url: str, body: dict[str, object]) -> object:
        raise NotImplementedError

    def get_text(self, url: str) -> str:
        return self.html
