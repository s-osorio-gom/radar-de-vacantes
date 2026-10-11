"""Corrida completa del motor de búsqueda.

universo → lectura de fuentes → detalle de lo relevante → deduplicación → evaluación →
historial (nuevas, cambios, cierres) → inventario → reportes. Solo lectura.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from radar_vacantes.boards import SUPPORTED_ATS, JobPosting, html_to_text
from radar_vacantes.compensation import CompGate
from radar_vacantes.evaluate import (
    Evaluation,
    Profile,
    Relevance,
    evaluate,
    needs_detail,
    norm,
)
from radar_vacantes.sources import (
    METHOD_ORACLE,
    METHOD_PAGE,
    METHOD_SUCCESSFACTORS,
    METHOD_WORKDAY,
    READ_OUTCOMES,
    Http,
    Outcome,
    SourceResult,
    SourceSpec,
    oracle_parts,
    read_source,
    workday_detail,
)


@dataclass(frozen=True, slots=True)
class Company:
    id: str
    name: str
    sector: str
    country: str
    tier: int
    cities: tuple[str, ...]
    careers_url: str
    sources: tuple[SourceSpec, ...]
    pending_sources: tuple[SourceSpec, ...]  # declaradas sin verified_at


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(name)).strip("-")


def load_universe(universe: dict[str, Any], radar: dict[str, Any]) -> list[Company]:
    """Empresas del universo más los boards verificados de la sección `boards`."""
    out: list[Company] = []
    by_name: dict[str, int] = {}
    for c in universe.get("companies") or []:
        if not isinstance(c, dict):
            continue
        cid, name = str(c["id"]), str(c["name"])
        verified: list[SourceSpec] = []
        pending: list[SourceSpec] = []
        for s in c.get("sources") or []:
            queries = tuple(str(q) for q in s.get("queries", []))
            spec = SourceSpec(cid, name, str(s["method"]), str(s["ref"]), queries)
            (verified if s.get("verified_at") or s["method"] == METHOD_PAGE else pending).append(
                spec
            )
        by_name[norm(name)] = len(out)
        out.append(
            Company(
                cid,
                name,
                str(c.get("sector", "")),
                str(c.get("country", "")),
                int(c.get("tier", 3)),
                tuple(str(x) for x in c.get("cities", [])),
                str(c.get("careers_url", "")),
                tuple(verified),
                tuple(pending),
            )
        )
    boards: Any = radar.get("boards") or {}
    for ats, entries in boards.items() if isinstance(boards, dict) else []:
        if ats not in SUPPORTED_ATS or not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict) or not e.get("verified_at"):
                continue
            name = str(e.get("name", e["slug"]))
            idx = by_name.get(norm(name))
            if idx is not None:
                comp = out[idx]
                spec = SourceSpec(comp.id, comp.name, str(ats), str(e["slug"]))
                out[idx] = Company(
                    comp.id,
                    comp.name,
                    comp.sector,
                    comp.country,
                    comp.tier,
                    comp.cities,
                    comp.careers_url,
                    (*comp.sources, spec),
                    comp.pending_sources,
                )
                continue
            cid = _slug(name)
            by_name[norm(name)] = len(out)
            out.append(
                Company(
                    cid,
                    name,
                    str(e.get("sector", "")),
                    "",
                    int(e.get("tier", 3)),
                    (),
                    "",
                    (SourceSpec(cid, name, str(ats), str(e["slug"])),),
                    (),
                )
            )
    return out


# ------------------------------------------------------------------ detalle


def enrich(p: JobPosting, client: Http) -> JobPosting:
    """Trae la descripción y la fecha exacta cuando el listado no las incluye."""
    try:
        if p.source == METHOD_WORKDAY:
            d = workday_detail(p.board_slug, p.source_job_id, client)
            loc = p.location
            if re.match(r"^\d+ locations?$", loc, re.IGNORECASE) and d.get("location"):
                loc = f"{d['location']} (+ otras ubicaciones)"
            return _replace(
                p,
                description=d.get("description", ""),
                location=loc,
                published_at=d.get("published_at") or p.published_at,
                workplace_type=d.get("remote_type", "") or p.workplace_type,
            )
        if p.source == METHOD_ORACLE:
            host, site = oracle_parts(p.board_slug)
            url = (
                f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                f"?expand=all&onlyData=true&finder=ById;Id=%22{p.source_job_id}%22,siteNumber={site}"
            )
            data: Any = client.get_json(url)
            item: Any = (data.get("items") or [{}])[0] if isinstance(data, dict) else {}
            parts = [
                str(item.get(k, "") or "")
                for k in (
                    "ExternalDescriptionStr",
                    "ExternalResponsibilitiesStr",
                    "ExternalQualificationsStr",
                )
            ]
            return _replace(p, description=html_to_text("\n".join(parts)) or p.description)
        if p.source == "magneto":
            return _from_schema(p, client.get_text(p.official_job_url))
        if p.source == METHOD_SUCCESSFACTORS:
            html = client.get_text(p.official_job_url)
            m = re.search(r'<span class="jobdescription">(.*?)</span>\s*</div>', html, re.S)
            text = html_to_text(m.group(1) if m else "")
            return _replace(p, description=text) if text else p
    except Exception:  # noqa: BLE001 — sin detalle, la vacante se evalúa con lo que hay
        return p
    return p


_LD = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def _from_schema(p: JobPosting, html: str) -> JobPosting:
    """Completa con el JobPosting de schema.org de la página (fecha, texto, ciudad, salario)."""
    for block in _LD.findall(html):
        try:
            data: Any = json.loads(block)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "JobPosting":
            continue
        loc: Any = data.get("jobLocation") or {}
        addr: Any = loc.get("address", {}) if isinstance(loc, dict) else {}
        city: Any = addr.get("addressLocality", "") if isinstance(addr, dict) else ""
        city_txt = ", ".join(city) if isinstance(city, list) else str(city)
        sal: Any = data.get("baseSalary") or {}
        val: Any = sal.get("value", {}) if isinstance(sal, dict) else {}
        salary = ""
        if isinstance(val, dict) and (val.get("minValue") or val.get("value")):
            unit = str(val.get("unitText", "MONTH")).lower()
            salary = (
                f"COP {val.get('minValue') or val.get('value')}"
                + (f" - {val.get('maxValue')}" if val.get("maxValue") else "")
                + (" per month" if "month" in unit else f" per {unit}")
            )
        org: Any = data.get("hiringOrganization") or {}
        company = str(org.get("name", "")) if isinstance(org, dict) else ""
        return _replace(
            p,
            title=html_to_text(str(data.get("title", p.title))).strip() or p.title,
            # El empleador real sale del detalle: Magneto muestra vacantes de otra
            # empresa cuando la consultada no tiene ninguna.
            company=company or p.company,
            description=html_to_text(str(data.get("description", ""))),
            published_at=str(data.get("datePosted", "") or "")[:10],
            location=f"{city_txt}, Colombia" if city_txt else p.location,
            salary_text=salary or p.salary_text,
        )
    return p


def _replace(p: JobPosting, **changes: str) -> JobPosting:
    data = {f: getattr(p, f) for f in p.__slots__}
    data.update(changes)
    return JobPosting(**data)


# ------------------------------------------------------------------ dedupe


def dedupe_key(p: JobPosting) -> str:
    first = norm(re.split(r"[,;/(]", p.location or "")[0]) if p.location else ""
    city = first.split()[0] if first else ""  # "Bogota Colombia" y "Bogotá, Cundinamarca" = bogota
    return f"{norm(p.company)}|{norm(p.title)}|{city}"


def dedupe(postings: list[JobPosting]) -> tuple[list[JobPosting], dict[str, list[str]]]:
    """Una vacante por empresa+cargo+ciudad. Prefiere el portal original a un agregador."""
    best: dict[str, JobPosting] = {}
    alt: dict[str, list[str]] = {}
    for p in postings:
        k = dedupe_key(p)
        cur = best.get(k)
        if cur is None:
            best[k] = p
            continue
        keep, other = (cur, p) if (cur.source != "indeed" or p.source == "indeed") else (p, cur)
        best[k] = keep
        if other.official_job_url and other.official_job_url != keep.official_job_url:
            alt.setdefault(k, []).append(other.official_job_url)
    return list(best.values()), alt


# ------------------------------------------------------------------ historial


def posting_id(p: JobPosting) -> str:
    return f"{p.source}|{p.board_slug}|{p.source_job_id}"


def diff(
    previous: dict[str, dict[str, str]], current: dict[str, dict[str, str]], read_sources: set[str]
) -> dict[str, list[str]]:
    """Nuevas, cambiadas y cerradas. Solo se declara cerrada una vacante cuya fuente se
    leyó bien en esta corrida: una fuente caída no cierra nada."""
    new = [k for k in current if k not in previous]
    changed = [
        k
        for k in current
        if k in previous
        and any(
            current[k].get(f) != previous[k].get(f) for f in ("title", "location", "salary_text")
        )
    ]
    closed = [
        k for k in previous if k not in current and previous[k].get("source_key") in read_sources
    ]
    return {"new": new, "changed": changed, "closed": closed}


# ------------------------------------------------------------------ corrida


@dataclass(slots=True)
class RunResult:
    stamp: str
    results: list[SourceResult]
    evaluations: list[Evaluation]
    companies: list[Company]
    history: dict[str, list[str]] = field(default_factory=lambda: {})
    snapshot: dict[str, dict[str, str]] = field(default_factory=lambda: {})


def run_search(
    companies: list[Company],
    client: Http,
    *,
    profile: Profile,
    today: date,
    seen_at: str,
    stamp: str,
    previous: dict[str, dict[str, str]],
    gate: CompGate | None = None,
    extra: list[JobPosting] | None = None,
    workers: int = 16,
    log: Callable[[str], None] = lambda _: None,
) -> RunResult:
    # Las fuentes declaradas sin verificar también se leen: leerlas es la verificación.
    specs = [s for c in companies for s in (*c.sources, *c.pending_sources)]
    by_id = {c.id: c for c in companies}
    company_of_name = {norm(c.name): c for c in companies}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda s: read_source(s, client, seen_at), specs))
    log(f"{sum(r.outcome in READ_OUTCOMES for r in results)}/{len(results)} fuentes leídas")
    raw: list[tuple[JobPosting, str]] = []
    for r in results:
        raw += [(p, r.spec.key) for p in r.postings]
    for p in extra or []:
        raw.append((p, "indeed"))
    source_key_of = {posting_id(p): k for p, k in raw}
    postings = [p for p, _ in raw]
    # El detalle (descripción y fecha exacta) solo para lo que puede importar.
    need = [
        i
        for i, p in enumerate(postings)
        if (p.source == "magneto")
        or (
            len(p.description) < 500
            and needs_detail(p)
            and p.source in (METHOD_WORKDAY, METHOD_ORACLE, METHOD_SUCCESSFACTORS)
        )
    ]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, p in zip(need, pool.map(lambda i: enrich(postings[i], client), need), strict=True):
            postings[i] = p
    log(f"detalle leído de {len(need)} vacantes")
    _validate_magneto(results, postings, source_key_of)
    unique, alternates = dedupe(postings)
    evaluations: list[Evaluation] = []
    for p in unique:
        comp = (
            by_id.get(_company_id_of(p, source_key_of, specs))
            or company_of_name.get(norm(p.company))
            or next(
                (
                    c
                    for c in companies
                    if _same_employer(c.name, p.company) and _same_employer(p.company, c.name)
                ),
                None,
            )
        )
        ev = evaluate(
            p,
            company_id=comp.id if comp else _slug(p.company),
            sector=comp.sector if comp else "",
            tier=comp.tier if comp else 3,
            profile=profile,
            today=today,
            gate=gate,
        )
        ev.also_at = alternates.get(dedupe_key(p), [])
        evaluations.append(ev)
    evaluations.sort(key=lambda e: (-e.score, e.posting.company, e.posting.title))
    # El historial compara publicaciones crudas por fuente, antes de deduplicar: la copia que
    # gana la deduplicación puede cambiar entre corridas y no por eso la otra "se cerró".
    score_of = {posting_id(e.posting): e for e in evaluations}
    snapshot: dict[str, dict[str, str]] = {}
    for p in postings:
        pid = posting_id(p)
        ev = score_of.get(pid)
        snapshot[pid] = {
            "title": p.title,
            "location": p.location,
            "salary_text": p.salary_text,
            "company": p.company,
            "url": p.official_job_url,
            "source_key": source_key_of.get(pid, ""),
            "published_at": p.published_at,
            "relevance": ev.relevance.value if ev else "",
            "score": str(ev.score) if ev else "",
        }
    # Una lista recortada (tope de página) no prueba que una vacante ausente se cerró.
    read_keys = {r.spec.key for r in results if r.outcome in READ_OUTCOMES and not r.truncated}
    history = diff(previous, snapshot, read_keys)
    return RunResult(stamp, results, evaluations, companies, history, snapshot)


def _same_employer(expected: str, actual: str) -> bool:
    exp = [w for w in norm(re.split(r"[(/]", expected)[0]).split() if len(w) > 2]
    act = norm(actual)
    if not exp:  # nombres cortos ("EY"): palabra completa
        short = norm(expected).split()
        return bool(short) and short[0] in act.split()
    return any(w in act for w in exp)


def _validate_magneto(
    results: list[SourceResult], postings: list[JobPosting], source_key_of: dict[str, str]
) -> None:
    """Una página de empresa en Magneto sin vacantes propias muestra las de otras: esa
    fuente cuenta como vacía para la empresa consultada (no como leída con vacantes)."""
    by_key: dict[str, list[JobPosting]] = {}
    for p in postings:
        by_key.setdefault(source_key_of.get(posting_id(p), ""), []).append(p)
    for r in results:
        if r.spec.method != "magneto" or r.spec.ref.startswith("q:") or r.outcome is not Outcome.OK:
            continue
        own = [p for p in by_key.get(r.spec.key, []) if _same_employer(r.spec.company, p.company)]
        if not own:
            r.outcome = Outcome.EMPTY
            r.detail = "Magneto mostró vacantes de otras empresas"


def _company_id_of(p: JobPosting, source_key_of: dict[str, str], specs: list[SourceSpec]) -> str:
    key = source_key_of.get(posting_id(p), "")
    for s in specs:
        if s.key == key:
            return s.company_id
    return ""


# ------------------------------------------------------------------ persistencia


class SearchStore:
    """Historial privado append-only: snapshots, inventario de fuentes y novedades."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.runs = root / "search_runs"
        self.inventory_path = root / "inventory.json"

    def previous_snapshot(self) -> dict[str, dict[str, str]]:
        if not self.runs.exists():
            return {}
        files = sorted(self.runs.glob("*.json"))
        if not files:
            return {}
        try:
            data: Any = json.loads(files[-1].read_text(encoding="utf-8"))
        except ValueError as exc:  # JSON roto o texto no UTF-8
            raise ValueError(
                f"El historial {files[-1]} está dañado ({exc}). Muévalo fuera de la carpeta "
                "para que la próxima corrida compare con la anterior."
            ) from exc
        snap: Any = data.get("snapshot", {}) if isinstance(data, dict) else {}
        return snap if isinstance(snap, dict) else {}

    def save(self, run: RunResult) -> Path:
        self.runs.mkdir(parents=True, exist_ok=True)
        path = self.runs / f"{run.stamp}.json"
        if path.exists():
            raise FileExistsError(path)
        path.write_text(
            json.dumps(
                {"stamp": run.stamp, "history": run.history, "snapshot": run.snapshot},
                ensure_ascii=False,
            ),
            "utf-8",
        )
        self._update_inventory(run)
        return path

    def inventory(self) -> dict[str, Any]:
        if not self.inventory_path.exists():
            return {"sources": {}}
        data: Any = json.loads(self.inventory_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"sources": {}}

    def _update_inventory(self, run: RunResult) -> None:
        inv = self.inventory()
        sources: dict[str, Any] = inv.setdefault("sources", {})
        by_id = {c.id: c for c in run.companies}
        for r in run.results:
            comp = by_id.get(r.spec.company_id)
            entry: dict[str, Any] = sources.setdefault(r.spec.key, {})
            relevant = sum(
                1
                for e in run.evaluations
                if e.posting.board_slug == r.spec.ref
                and e.relevance in (Relevance.ALTA, Relevance.MEDIA)
            )
            entry.update(
                {
                    "company": r.spec.company,
                    "sector": comp.sector if comp else "",
                    "country": comp.country if comp else "",
                    "careers_url": comp.careers_url if comp else "",
                    "method": r.spec.method,
                    "ref": r.spec.ref,
                    "last_checked": run.stamp,
                    "outcome": r.outcome.value,
                    "detail": r.detail,
                    "postings": len(r.postings),
                    "relevant": relevant,
                }
            )
            if r.outcome in READ_OUTCOMES:
                entry["last_ok"] = run.stamp
            hist: list[str] = entry.setdefault("history", [])
            hist.append(f"{run.stamp}:{r.outcome.value}:{len(r.postings)}")
            del hist[:-30]
        tmp = self.inventory_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(inv, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(self.inventory_path)


def coverage(run: RunResult) -> dict[str, Any]:
    """Cobertura real: empresas del universo con al menos una fuente leída."""
    by_company: dict[str, list[SourceResult]] = {}
    for r in run.results:
        by_company.setdefault(r.spec.company_id, []).append(r)
    rows: list[dict[str, Any]] = []
    via_indeed = {norm(e.posting.company) for e in run.evaluations if e.posting.source == "indeed"}

    def indeed_match(name: str) -> bool:
        key = norm(re.split(r"[(/]", name)[0]).strip()
        return any(key and (key in v or v in key) for v in via_indeed)

    for c in run.companies:
        res = by_company.get(c.id, [])
        read = any(r.outcome in READ_OUTCOMES for r in res)
        manual = any(r.outcome is Outcome.MANUAL for r in res)
        if read:
            status = "leída"
        elif indeed_match(c.name):
            status = "leída vía Indeed"
        elif manual:
            status = "accesible, revisión manual"
        elif res:
            status = "falló: " + ", ".join(
                sorted({f"{r.outcome.value} {r.detail}".strip() for r in res})
            )
        elif c.pending_sources:
            status = "fuente declarada sin verificar"
        else:
            status = "sin fuente identificada"
        rows.append(
            {
                "id": c.id,
                "company": c.name,
                "sector": c.sector,
                "tier": c.tier,
                "cities": list(c.cities),
                "status": status,
            }
        )
    accessible = [
        r
        for r in rows
        if r["status"] in ("leída", "leída vía Indeed", "accesible, revisión manual")
    ]
    read = [r for r in rows if r["status"] in ("leída", "leída vía Indeed")]
    return {"companies": len(rows), "read": len(read), "accessible": len(accessible), "rows": rows}
