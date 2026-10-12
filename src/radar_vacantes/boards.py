"""Adaptadores read-only de job boards públicos.

Solo GET contra APIs públicas documentadas de Greenhouse, Ashby, Lever,
SmartRecruiters, Workable y Recruitee.
Ningún adaptador autentica, envía formularios ni controla un navegador: los
endpoints de postulación de esas plataformas existen pero **no se usan**.

Indeed no se consulta desde aquí: `parse_indeed_transcript` normaliza una
transcripción JSON de resultados que el usuario obtiene por su cuenta
(`radar buscar --indeed archivo.json`).

Todo contenido recibido es dato no confiable. Se normaliza y se guarda; nunca
se interpreta como instrucción.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

ATS_GREENHOUSE = "greenhouse"
ATS_ASHBY = "ashby"
ATS_LEVER = "lever"
ATS_SMARTRECRUITERS = "smartrecruiters"
ATS_WORKABLE = "workable"
ATS_RECRUITEE = "recruitee"
SUPPORTED_ATS = frozenset({
    ATS_GREENHOUSE, ATS_ASHBY, ATS_LEVER, ATS_SMARTRECRUITERS, ATS_WORKABLE, ATS_RECRUITEE,
})

@dataclass(frozen=True, slots=True)
class BoardRef:
    """Un job board concreto: plataforma, identificador público y nombre."""

    ats: str
    slug: str
    name: str


@dataclass(frozen=True, slots=True)
class JobPosting:
    """Vacante normalizada al contrato común de todas las fuentes.

    Los campos que la fuente no entrega quedan vacíos; no se inventan.
    """

    source: str
    source_job_id: str
    company: str
    title: str
    location: str
    official_job_url: str
    published_at: str
    first_seen_at: str
    workplace_type: str = ""
    department: str = ""
    salary_text: str = ""
    # Para leer el detalle y la descripción. No entran en `to_dict` ni en el
    # almacén del radar (la descripción se guarda aparte, con la candidatura).
    board_slug: str = ""
    description: str = ""

    @property
    def job_id(self) -> str:
        """Identificador estable para dedupe: fuente + empresa + id de la fuente."""
        raw = f"{self.source}|{self.company}|{self.source_job_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, str]:
        """Serialización plana para el almacén append-only."""
        return {
            "job_id": self.job_id,
            "source": self.source,
            "source_job_id": self.source_job_id,
            "company": self.company,
            "title": self.title,
            "location": self.location,
            "official_job_url": self.official_job_url,
            "published_at": self.published_at,
            "first_seen_at": self.first_seen_at,
            "workplace_type": self.workplace_type,
            "department": self.department,
            "salary_text": self.salary_text,
        }


def unpublished(ats: str, job: object) -> bool:
    """El propio ATS marca el aviso como no publicado (Ashby `isListed: false`, Recruitee
    `status` distinto de `published`): se omite a propósito, no es un formato desconocido."""
    if not isinstance(job, dict):
        return False
    if ats == ATS_ASHBY:
        return job.get("isListed") is False
    if ats == ATS_RECRUITEE:
        return job.get("status") not in (None, "published")
    return False


def _str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def html_to_text(raw: str) -> str:
    """HTML (a veces escapado dos veces, como en Greenhouse) a texto plano acotado."""
    text = html.unescape(html.unescape(raw)).replace("\xa0", " ")
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()[:60000]


def _lever_text(job: dict[str, Any]) -> str:
    """Descripción completa de Lever: cuerpo, listas (requisitos) y adicional."""
    parts = [_str(job.get("descriptionPlain"))]
    lists: Any = job.get("lists") or []
    for item in lists if isinstance(lists, list) else []:
        if isinstance(item, dict):
            parts.append(_str(item.get("text")))
            parts.append(html_to_text(_str(item.get("content"))))
    parts.append(_str(job.get("additionalPlain")))
    return "\n".join(p for p in parts if p).replace("\xa0", " ")[:60000]


def _join_locations(primary: str, extra: list[str]) -> str:
    """Ubicación principal más las secundarias, sin repetir, separadas por ';'.

    Lever (`allLocations`) y Ashby (`secondaryLocations`) publican ahí países
    adicionales; una vacante "Remote - US" con secundaria "Remote - Colombia" sí
    está abierta a Colombia.
    """
    seen: list[str] = []
    for loc in [primary, *extra]:
        if loc and loc not in seen:
            seen.append(loc)
    return " ; ".join(seen)[:1000]


def board_url(board: BoardRef) -> str:
    """URL pública de lectura del board."""
    if board.ats == ATS_GREENHOUSE:
        return f"https://boards-api.greenhouse.io/v1/boards/{board.slug}/jobs"
    if board.ats == ATS_ASHBY:
        return (
            f"https://api.ashbyhq.com/posting-api/job-board/{board.slug}"
            "?includeCompensation=true"
        )
    if board.ats == ATS_LEVER:
        return f"https://api.lever.co/v0/postings/{board.slug}?mode=json"
    if board.ats == ATS_SMARTRECRUITERS:
        # Posting API pública. Empresas con miles de vacantes (Bosch, 4.894): se filtra
        # Colombia en el servidor con el parámetro documentado `country`.
        return (
            f"https://api.smartrecruiters.com/v1/companies/{board.slug}/postings"
            "?country=co&limit=100"
        )
    if board.ats == ATS_WORKABLE:
        return f"https://www.workable.com/api/accounts/{board.slug}"
    if board.ats == ATS_RECRUITEE:
        return f"https://{board.slug}.recruitee.com/api/offers/"
    raise ValueError(f"ATS no soportado: {board.ats}")


def parse_greenhouse(board: BoardRef, payload: Any, seen_at: str) -> list[JobPosting]:
    """Normaliza la respuesta de Greenhouse Job Board API."""
    jobs: Any = payload.get("jobs", []) if isinstance(payload, dict) else []
    out: list[JobPosting] = []
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict):
            continue
        location: Any = job.get("location") or {}
        departments: Any = job.get("departments") or []
        department = ""
        if isinstance(departments, list) and departments and isinstance(departments[0], dict):
            department = _str(departments[0].get("name"))
        out.append(
            JobPosting(
                source=ATS_GREENHOUSE,
                board_slug=board.slug,
                source_job_id=str(job.get("id", "")),
                company=board.name,
                title=_str(job.get("title")),
                location=_str(location.get("name")) if isinstance(location, dict) else "",
                official_job_url=_str(job.get("absolute_url")),
                # Solo la fecha de primera publicación: `updated_at` es la última edición y
                # haría parecer fresca una vacante vieja.
                published_at=_str(job.get("first_published")),
                first_seen_at=seen_at,
                department=department,
            )
        )
    return out


def parse_ashby(board: BoardRef, payload: Any, seen_at: str) -> list[JobPosting]:
    """Normaliza la respuesta de Ashby Job Posting API."""
    jobs: Any = payload.get("jobs", []) if isinstance(payload, dict) else []
    out: list[JobPosting] = []
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict) or unpublished(ATS_ASHBY, job):
            continue
        compensation: Any = job.get("compensation") or {}
        salary = ""
        if isinstance(compensation, dict):
            salary = _str(compensation.get("compensationTierSummary"))
        workplace = _str(job.get("workplaceType"))
        if not workplace and job.get("isRemote") is True:
            workplace = "Remote"
        secondary: Any = job.get("secondaryLocations") or []
        extra = [
            _str(s.get("location")) for s in secondary if isinstance(s, dict)
        ] if isinstance(secondary, list) else []
        out.append(
            JobPosting(
                source=ATS_ASHBY,
                board_slug=board.slug,
                source_job_id=_str(job.get("id")),
                company=board.name,
                title=_str(job.get("title")),
                location=_join_locations(_str(job.get("location")), extra),
                description=_str(job.get("descriptionPlain"))[:60000],
                official_job_url=_str(job.get("jobUrl")),
                published_at=_str(job.get("publishedAt")),
                first_seen_at=seen_at,
                workplace_type=workplace,
                department=_str(job.get("department")),
                salary_text=salary,
            )
        )
    return out


def parse_lever(board: BoardRef, payload: Any, seen_at: str) -> list[JobPosting]:
    """Normaliza la respuesta de Lever Postings API."""
    out: list[JobPosting] = []
    for job in payload if isinstance(payload, list) else []:
        if not isinstance(job, dict):
            continue
        categories: Any = job.get("categories") or {}
        cats = categories if isinstance(categories, dict) else {}
        created: Any = job.get("createdAt")
        published = ""
        if isinstance(created, int):
            published = datetime.fromtimestamp(created / 1000, tz=UTC).isoformat()
        out.append(
            JobPosting(
                source=ATS_LEVER,
                board_slug=board.slug,
                source_job_id=_str(job.get("id")),
                company=board.name,
                title=_str(job.get("text")),
                location=_join_locations(
                    _str(cats.get("location")),
                    [_str(x) for x in cats.get("allLocations") or [] if isinstance(x, str)],
                ),
                official_job_url=_str(job.get("hostedUrl")),
                description=_lever_text(job),
                published_at=published,
                first_seen_at=seen_at,
                workplace_type=_str(job.get("workplaceType")),
                department=_str(cats.get("team") or cats.get("department")),
            )
        )
    return out


SOURCE_INDEED = "indeed"
#: Topes para una transcripción de Indeed: es contenido externo de origen no
#: verificado, así que se acota antes de guardarlo.
INDEED_MAX_JOBS = 200
INDEED_MAX_FIELD = 300
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"),
        start=1,
    )
}


def _norm_key(value: str) -> str:
    """Minúsculas, sin tildes y con espacios colapsados: base de la clave estable."""
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(plain.casefold().split())


def indeed_stable_id(company: str, title: str, location: str) -> str:
    """Clave de dedupe para Indeed.

    Los resultados de Indeed traen ids y enlaces cortos que cambian en cada
    búsqueda (la misma vacante sale con dos ids distintos). Por eso la identidad se arma con
    empresa + puesto + ubicación normalizados, nunca con el id del conector.
    """
    return "|".join(_norm_key(v) for v in (company, title, location))


def _indeed_date(value: str) -> str:
    """'September 23, 2026' → '2026-09-23'. Formato desconocido → vacío, no se adivina."""
    parts = value.replace(",", " ").split()
    if len(parts) != 3 or parts[0].lower() not in _MONTHS:
        return ""
    try:
        return date(int(parts[2]), _MONTHS[parts[0].lower()], int(parts[1])).isoformat()
    except ValueError:
        return ""


def _clip(value: object) -> str:
    return " ".join(_str(value).split())[:INDEED_MAX_FIELD]


def parse_indeed_transcript(payload: Any, seen_at: str) -> list[JobPosting]:
    """Normaliza una transcripción JSON de resultados de Indeed.

    Formato: ``{"jobs": [{"company", "title", "location", "url", "posted_on",
    "compensation"}]}``. Entradas sin empresa o puesto se descartan;
    URLs que no sean https quedan vacías.
    """
    jobs: Any = payload.get("jobs") if isinstance(payload, dict) else None
    out: list[JobPosting] = []
    seen: set[str] = set()
    for job in (jobs if isinstance(jobs, list) else [])[:INDEED_MAX_JOBS]:
        if not isinstance(job, dict):
            continue
        company, title, location = _clip(job.get("company")), _clip(job.get("title")), _clip(
            job.get("location")
        )
        if not company or not title:
            continue
        key = indeed_stable_id(company, title, location)
        if key in seen:
            continue
        seen.add(key)
        url = _clip(job.get("url"))
        salary = _clip(job.get("compensation"))
        out.append(
            JobPosting(
                source=SOURCE_INDEED,
                source_job_id=key,
                company=company,
                title=title,
                location=location,
                official_job_url=url if url.startswith("https://") else "",
                published_at=_indeed_date(_clip(job.get("posted_on"))),
                first_seen_at=seen_at,
                salary_text="" if salary.lower() in {"n/a", "none"} else salary,
                # Descripción del aviso, si la transcripción la trae (dato externo).
                description=" ".join(_str(job.get("description")).split())[:20000]
                if not _str(job.get("description")).startswith("http") else "",
            )
        )
    return out


def parse_smartrecruiters(board: BoardRef, payload: Any, seen_at: str) -> list[JobPosting]:
    """Normaliza SmartRecruiters Posting API (`content[]`)."""
    content: Any = payload.get("content", []) if isinstance(payload, dict) else []
    out: list[JobPosting] = []
    for job in content if isinstance(content, list) else []:
        if not isinstance(job, dict):
            continue
        loc: Any = job.get("location") or {}
        loc = loc if isinstance(loc, dict) else {}
        company: Any = job.get("company") or {}
        ident = _str(company.get("identifier")) if isinstance(company, dict) else ""
        jid = _str(job.get("id"))
        out.append(
            JobPosting(
                source=ATS_SMARTRECRUITERS,
                board_slug=board.slug,
                source_job_id=jid,
                company=board.name,
                title=_str(job.get("name")),
                location=_str(loc.get("fullLocation"))
                or ", ".join(x for x in (_str(loc.get("city")), _str(loc.get("country"))) if x),
                official_job_url=f"https://jobs.smartrecruiters.com/{ident or board.slug}/{jid}"
                if jid else "",
                published_at=_str(job.get("releasedDate")),
                first_seen_at=seen_at,
                workplace_type="Remote" if loc.get("remote") is True else "",
            )
        )
    return out


def parse_workable(board: BoardRef, payload: Any, seen_at: str) -> list[JobPosting]:
    """Normaliza el endpoint público de cuenta de Workable (`jobs[]`)."""
    jobs: Any = payload.get("jobs", []) if isinstance(payload, dict) else []
    out: list[JobPosting] = []
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict):
            continue
        places: list[str] = []
        raw_locs: Any = job.get("locations") or []
        for loc in raw_locs if isinstance(raw_locs, list) else []:
            if isinstance(loc, dict) and loc.get("hidden") is not True:
                place = ", ".join(x for x in (_str(loc.get("city")), _str(loc.get("country"))) if x)
                if place:
                    places.append(place)
        primary = ", ".join(x for x in (_str(job.get("city")), _str(job.get("country"))) if x)
        out.append(
            JobPosting(
                source=ATS_WORKABLE,
                board_slug=board.slug,
                source_job_id=_str(job.get("shortcode")),
                company=board.name,
                title=_str(job.get("title")),
                location=_join_locations(primary, places),
                official_job_url=_str(job.get("url")),
                published_at=_str(job.get("published_on")),
                first_seen_at=seen_at,
                workplace_type="Remote" if job.get("telecommuting") is True else "",
                department=_str(job.get("department")),
            )
        )
    return out


def parse_recruitee(board: BoardRef, payload: Any, seen_at: str) -> list[JobPosting]:
    """Normaliza Recruitee Careers Site API (`offers[]`, solo publicadas)."""
    offers: Any = payload.get("offers", []) if isinstance(payload, dict) else []
    out: list[JobPosting] = []
    for job in offers if isinstance(offers, list) else []:
        if not isinstance(job, dict) or unpublished(ATS_RECRUITEE, job):
            continue
        places: list[str] = []
        raw_locs: Any = job.get("locations") or []
        for loc in raw_locs if isinstance(raw_locs, list) else []:
            if isinstance(loc, dict):
                place = ", ".join(x for x in (_str(loc.get("city")), _str(loc.get("country"))) if x)
                if place:
                    places.append(place)
        out.append(
            JobPosting(
                source=ATS_RECRUITEE,
                board_slug=board.slug,
                source_job_id=_str(job.get("id")),
                company=board.name,
                title=_str(job.get("title")),
                location=_join_locations(places[0] if places else "", places[1:]),
                official_job_url=_str(job.get("careers_url")),
                description=html_to_text(
                    _str(job.get("description")) + "\n" + _str(job.get("requirements"))
                ),
                published_at=_str(job.get("published_at")),
                first_seen_at=seen_at,
                workplace_type="Remote" if job.get("remote") is True else "",
                department=_str(job.get("department")),
            )
        )
    return out


_PARSERS: dict[str, Callable[[BoardRef, Any, str], list[JobPosting]]] = {
    ATS_GREENHOUSE: parse_greenhouse,
    ATS_ASHBY: parse_ashby,
    ATS_LEVER: parse_lever,
    ATS_SMARTRECRUITERS: parse_smartrecruiters,
    ATS_WORKABLE: parse_workable,
    ATS_RECRUITEE: parse_recruitee,
}

