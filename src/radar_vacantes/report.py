"""Reportes del motor de búsqueda: oportunidades, novedades y cobertura."""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from collections import Counter
from typing import Any

from radar_vacantes import __version__
from radar_vacantes.engine import RunResult, coverage, posting_id
from radar_vacantes.evaluate import Authorization, Evaluation, Relevance
from radar_vacantes.sources import READ_OUTCOMES, Outcome

TIER_NAME = {1: "élite", 2: "top", 3: "otra"}


def _cell(text: str, n: int = 80) -> str:
    """Texto externo a una línea de Markdown: sin saltos (no abre encabezados ni listas),
    sin `|` (no rompe la tabla) y sin `<` ni `>` (no inyecta HTML al renderizar)."""
    flat = " ".join(str(text).split()).replace("|", "/")[:n]
    return flat.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _url(url: str) -> str:
    """Solo enlaces http(s); se escapan los caracteres que cortarían el enlace Markdown."""
    parts = urllib.parse.urlsplit(str(url).strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return urllib.parse.quote(parts.geturl(), safe=":/?#[]@!$&'*+,;=%~")


def _link(label: str, url: str) -> str:
    safe = _url(url)
    return f"[{label}]({safe})" if safe else "sin enlace"


def is_candidate(e: Evaluation) -> bool:
    return e.relevance in (Relevance.ALTA, Relevance.MEDIA)


def buckets(evals: list[Evaluation]) -> dict[str, list[Evaluation]]:
    cand = [e for e in evals if is_candidate(e)]
    local = {Authorization.COLOMBIA, Authorization.REMOTO, Authorization.PATROCINIO}
    viable = [e for e in cand if e.authorization in local]
    return {
        "recientes": [e for e in viable if e.age_days is not None and e.age_days <= 20],
        "antiguas": [e for e in viable if e.age_days is None or e.age_days > 20],
        "internacionales": [
            e
            for e in cand
            if e.authorization in (Authorization.MULTILATERAL, Authorization.SIN_INFO)
        ],
        "requieren": [e for e in cand if e.authorization is Authorization.REQUIERE],
    }


def _table(rows: list[Evaluation], verified: str) -> list[str]:
    out = [
        "| # | Publicada | Empresa | Cargo | Ubicación | Cumple | Autorización | Pts | Enlace |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, e in enumerate(rows, 1):
        p = e.posting
        pub = p.published_at[:10] if p.published_at else "sin fecha"
        out.append(
            f"| {i} | {pub} | {_cell(p.company, 30)} ({TIER_NAME.get(e.tier, '')}) | {_cell(p.title, 70)} | "
            f"{_cell(p.location, 40)}{' · ' + _cell(p.workplace_type, 15) if p.workplace_type else ''} | "
            f"{e.fit.value} | {e.authorization.value} | {e.score} | {_link('abrir', p.official_job_url)} |"
        )
    del verified
    return out


def _detail(i: int, e: Evaluation, verified: str) -> list[str]:
    p = e.posting
    lines = [
        f"### {i}. {_cell(p.company, 120)} — {_cell(p.title, 200)}",
        "",
        f"- **Publicada:** {p.published_at[:10] if p.published_at else 'sin fecha en la fuente'} "
        f"({e.recency}) · **Verificada:** {verified} en {p.source}",
        f"- **Ubicación y modalidad:** {_cell(p.location, 200) or 'no indicada'}"
        f"{' · ' + _cell(p.workplace_type, 40) if p.workplace_type else ''}",
        f"- **Cumple:** {e.fit.value} · **Afinidad:** {e.relevance.value} ({e.family}) · "
        f"**Empleador:** {TIER_NAME.get(e.tier, '')} · **Puntaje:** {e.score}",
        f"- **Autorización de trabajo:** {e.authorization.value}",
        f"- **Salario:** {_cell(e.salary_note, 200)}",
    ]
    if e.seniority:
        lines.append(f"- **Nivel:** {e.seniority}")
    if e.must:
        lines.append("- **Indispensables:** " + "; ".join(_cell(x, 160) for x in e.must[:5]))
    if e.nice:
        lines.append("- **Deseables:** " + "; ".join(_cell(x, 160) for x in e.nice[:3]))
    if not e.must and not e.nice:
        lines.append("- **Requisitos:** la fuente no separa indispensables de deseables")
    lines.append(
        "- **Por qué encaja:** "
        + ("; ".join(_cell(x, 200) for x in e.reasons) if e.reasons else "por la familia del cargo")
    )
    lines.append(
        "- **Brechas:** "
        + ("; ".join(_cell(x, 200) for x in e.gaps) if e.gaps else "ninguna detectada en el texto")
    )
    lines.append(f"- **Cómo te acerca a tu meta:** {path_note(e)}")
    lines.append(f"- **Siguiente paso:** {e.next_step}")
    lines.append(f"- **Enlace para postular:** {_url(p.official_job_url) or 'sin enlace'}")
    if e.also_at:
        alts = [u for u in (_url(x) for x in e.also_at[:3]) if u]
        if alts:
            lines.append("- **También publicada en:** " + ", ".join(alts))
    return [*lines, ""]


def search_markdown(run: RunResult, *, verified: str, top: int = 40) -> str:
    b = buckets(run.evaluations)
    cov = coverage(run)
    read = sum(r.outcome in READ_OUTCOMES for r in run.results)
    n_post = sum(len(r.postings) for r in run.results)
    snap = run.snapshot
    lines = [
        f"# Búsqueda de vacantes — {run.stamp}",
        "",
        "Solo lectura: nada se envió. Puntaje determinístico: afinidad 35,",
        "cumplimiento de requisitos 25, autorización 20, nivel del empleador 15, antigüedad 5.",
        "",
        "## Resumen",
        "",
        f"- Empresas en el universo: **{cov['companies']}** · con fuente leída: **{cov['read']}** · "
        f"accesibles (leídas + revisión manual): **{cov['accessible']}**",
        f"- Fuentes consultadas: {len(run.results)} · leídas bien: {read} · vacantes leídas: {n_post} · "
        f"únicas tras deduplicar: {len(run.evaluations)}",
        f"- Afinidad alta o media: {sum(1 for e in run.evaluations if e.relevance in (Relevance.ALTA, Relevance.MEDIA))}"
        f" · recientes (≤20 días) y viables: **{len(b['recientes'])}** · antiguas o sin fecha: {len(b['antiguas'])}"
        f" · internacionales que exigen autorización: {len(b['requieren'])}",
        f"- Novedades frente a la corrida anterior: {len(run.history.get('new', []))} nuevas · "
        f"{len(run.history.get('changed', []))} cambiadas · {len(run.history.get('closed', []))} cerradas",
        "",
        "## 1. Prioridad: publicadas en los últimos 20 días",
        "",
        *_table(b["recientes"][:top], verified),
        "",
        "## 2. Pertinentes más antiguas o sin fecha (siguen abiertas en la fuente)",
        "",
        *_table(b["antiguas"][:25], verified),
        "",
        "## 3. Detalle de las prioritarias",
        "",
    ]
    for i, e in enumerate(b["recientes"][:top], 1):
        lines += _detail(i, e, verified)
    lines += [
        "## 4. Internacionales por confirmar (multilaterales o sin información de patrocinio)",
        "",
        *_table(b["internacionales"][:25], verified),
        "",
    ]
    lines += ["## 5. Internacionales que exigen autorización de trabajo (no presupuesta)", ""]
    for e in b["requieren"][:15]:
        lines.append(
            f"- {_cell(e.posting.company, 60)} — {_cell(e.posting.title, 90)} · {_cell(e.posting.location, 40)} · "
            f"{_link('abrir', e.posting.official_job_url)}"
        )
    closed = run.history.get("closed", [])
    if closed:
        lines += ["", "## 6. Cerradas desde la corrida anterior", ""]
        lines += [f"- {_cell(k, 200)}" for k in closed[:40]]
    lines += ["", *coverage_markdown(run, cov)]
    del snap
    return "\n".join(lines) + "\n"


def coverage_markdown(run: RunResult, cov: dict[str, Any] | None = None) -> list[str]:
    cov = cov or coverage(run)
    rows: list[dict[str, Any]] = cov["rows"]
    by_sector: dict[str, Counter[str]] = {}
    for r in rows:
        key = (
            "leída"
            if r["status"].startswith("leída")
            else ("manual" if r["status"].startswith("accesible") else "pendiente")
        )
        by_sector.setdefault(r["sector"] or "sin sector", Counter())[key] += 1
    city_count: Counter[str] = Counter()
    for e in run.evaluations:
        if (
            e.relevance in (Relevance.ALTA, Relevance.MEDIA)
            and e.authorization is Authorization.COLOMBIA
        ):
            loc = e.posting.location.lower()
            for city in ("bogot", "medell", "cali", "barranquilla", "cartagena", "bucaramanga"):
                if city in loc:
                    city_count[city] += 1
    out = [
        "## Cobertura",
        "",
        "| Sector | Leídas | Revisión manual | Pendientes |",
        "|---|---|---|---|",
    ]
    for sector, c in sorted(by_sector.items()):
        out.append(f"| {sector} | {c['leída']} | {c['manual']} | {c['pendiente']} |")
    out += [
        "",
        "Vacantes relevantes en Colombia por ciudad: "
        + (", ".join(f"{k}: {v}" for k, v in city_count.most_common()) or "ninguna"),
        "",
        "### Empresas pendientes o con fallo",
        "",
    ]
    for r in rows:
        if not r["status"].startswith("leída"):
            out.append(
                f"- {_cell(r['company'], 60)} ({r['sector']}, {TIER_NAME.get(r['tier'], '')}): "
                f"{_cell(r['status'], 200)}"
            )
    out += ["", "### Fuentes con error (no equivale a 'sin vacantes')", ""]
    for res in run.results:
        if res.outcome not in READ_OUTCOMES and res.outcome is not Outcome.MANUAL:
            out.append(f"- {_cell(res.spec.key, 200)} ({_cell(res.spec.company, 60)}): {res.outcome.value} {_cell(res.detail, 120)}")
    return out


def latest_json(run: RunResult, verified: str, top: int = 60) -> dict[str, Any]:
    b = buckets(run.evaluations)

    def row(e: Evaluation) -> dict[str, Any]:
        p = e.posting
        return {
            "id": posting_id(p),
            "company": p.company,
            "title": p.title,
            "location": p.location,
            "workplace": p.workplace_type,
            "published_at": p.published_at[:10],
            "verified_at": verified,
            "url": p.official_job_url,
            "fit": e.fit.value,
            "authorization": e.authorization.value,
            "relevance": e.relevance.value,
            "tier": e.tier,
            "score": e.score,
            "must": e.must[:5],
            "nice": e.nice[:3],
            "reasons": e.reasons,
            "gaps": e.gaps,
            "next_step": e.next_step,
            "salary": e.salary_note,
            "is_new": posting_id(p) in set(run.history.get("new", [])),
        }

    return {
        "stamp": run.stamp,
        "recientes": [row(e) for e in b["recientes"][:top]],
        "antiguas": [row(e) for e in b["antiguas"][:40]],
        "internacionales": [row(e) for e in b["internacionales"][:30]],
        "history": {k: len(v) for k, v in run.history.items()},
    }


def tablero_docs(
    rows: list[tuple[Evaluation, Any]], *, verified: str, lote: str
) -> list[dict[str, Any]]:
    """Documentos para un tablero o una hoja de seguimiento (una fila por vacante).

    El id es estable por vacante, así una corrida nueva actualiza la fila en vez de duplicarla,
    y 'lote' permite al tablero mostrar solo la lista vigente sin borrar las anteriores."""
    docs: list[dict[str, Any]] = []
    for e, chk in rows:
        p = e.posting
        doc_id = hashlib.sha1(posting_id(p).encode("utf-8")).hexdigest()[:16]
        docs.append({
            "doc_id": doc_id,
            "data": {
                "empresa": p.company,
                "puesto": p.title,
                "ubicacion": p.location,
                "fuente": p.source,
                "puntaje": e.score,
                "decision": "SHORTLIST" if e.fit.value == "cumple completamente" else "REVIEW",
                "cumple": e.fit.value,
                "verificacion": str(getattr(chk, "status", "")),
                "publicada": p.published_at[:10],
                "razones": [path_note(e), *e.reasons[:2]],
                "brechas": e.gaps[:3],
                "url": str(getattr(chk, "apply_url", "") or p.official_job_url),
                "lote": lote,
                "verificada_en": verified,
                "motor": f"radar-de-vacantes {__version__}",
            },
        })
    return docs


_WORD = re.compile(r"[a-záéíóúñ0-9]{4,}")
_STOP = {"colombia", "bogota", "bogotá", "analyst", "analista", "senior", "junior", "full",
         "time", "program", "programa", "2026", "2027", "2025"}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def tracked_from_seguimiento(markdown: str) -> list[tuple[str, set[str]]]:
    """(empresa, palabras del puesto) de cada fila de una tabla Markdown de seguimiento."""
    rows: list[tuple[str, set[str]]] = []
    for line in markdown.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] in ("Empresa", "") or set(cells[0]) <= {"-", ":"}:
            continue
        rows.append((cells[0].lower(), _tokens(cells[1])))
    return rows


def already_tracked(e: Evaluation, tracked: list[tuple[str, set[str]]]) -> bool:
    """La vacante ya está en el seguimiento: misma empresa y casi el mismo puesto."""
    company = e.posting.company.lower()
    title = _tokens(e.posting.title)
    for name, words in tracked:
        first = name.split()[0].strip("()") if name.split() else ""
        if not first or (first not in company and company.split()[0] not in name):
            continue
        if title and words and len(title & words) / min(len(title), len(words)) >= 0.6:
            return True
    return False


def select_top(evals: list[Evaluation], *, per_company: int = 3) -> list[Evaluation]:
    """Candidatas a la lista final, en orden: afinidad alta, postulables desde Colombia,
    sin prácticas, convocatorias restringidas ni cargos directivos, y que no cumplan poco."""
    from radar_vacantes.evaluate import Fit

    ok_auth = {
        Authorization.COLOMBIA,
        Authorization.REMOTO,
        Authorization.PATROCINIO,
        Authorization.MULTILATERAL,
    }
    out: list[Evaluation] = []
    count: Counter[str] = Counter()
    for e in evals:
        if (
            e.relevance is not Relevance.ALTA
            or e.authorization not in ok_auth
            or e.fit is Fit.BAJO
            or e.seniority.startswith(("nivel directivo", "práctica"))
            or any(g.startswith("convocatoria restringida") for g in e.gaps)
        ):
            continue
        key = e.posting.company.lower()
        if count[key] >= per_company:
            continue
        count[key] += 1
        out.append(e)
    return out


def top_markdown(
    rows: list[tuple[Evaluation, Any]], *, verified: str, discarded: list[tuple[Evaluation, Any]]
) -> str:
    lines = [
        f"# Las {len(rows)} mejores vacantes verificadas — {verified}",
        "",
        "Seleccionadas por el motor y verificadas una por una en la fuente original.",
        "Cumple = comparación determinística de los requisitos del aviso con el perfil.",
        "",
        "| # | Publicada | Empresa | Cargo | Ciudad · modalidad | Cumple | Verificación | Enlace |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, (e, chk) in enumerate(rows, 1):
        p = e.posting
        lines.append(
            f"| {i} | {p.published_at[:10] or 'sin fecha'} | {_cell(p.company, 30)} | {_cell(p.title, 70)} | "
            f"{_cell(p.location, 35)}{' · ' + _cell(p.workplace_type, 12) if p.workplace_type else ''} | "
            f"{e.fit.value} | {chk.status} | {_link('postular', chk.apply_url)} |"
        )
    lines += ["", "## Detalle", ""]
    for i, (e, chk) in enumerate(rows, 1):
        lines += _detail(i, e, verified)
        lines.insert(len(lines) - 1, f"- **Verificación:** {chk.status} — {_cell(chk.evidence, 200)}")
    if discarded:
        lines += ["## Descartadas en la verificación", ""]
        lines += [
            f"- {_cell(e.posting.company, 60)} — {_cell(e.posting.title, 80)}: {chk.status} ({_cell(chk.evidence, 120)})"
            for e, chk in discarded
        ]
    return "\n".join(lines) + "\n"


#: Por qué cada sector o familia acerca a la meta del perfil. Se carga desde
#: `perfil.yaml → meta` con `set_goal_notes`; estas son notas por defecto.
GOAL_NOTES: dict[str, str] = {
    "banca_global": "experiencia directa en un banco global",
    "banca_inversion": "transacciones y mercados de capitales",
    "multilateral": "entrada al sistema multilateral",
    "gestor_activos": "inversión en un fondo global: modelación y valoración",
    "rating": "análisis de crédito y estados financieros",
    "consultoria": "consultoría estratégica o de transacciones",
    "banca_central": "política económica y mercados",
    "pagos": "estrategia y monetización en una red global",
    "tecnologia": "análisis de negocio en una empresa global",
}
GOAL_DEFAULT = ["experiencia transferible; aporte indirecto a la meta"]


def set_goal_notes(notes: dict[str, str], default: str = "") -> None:
    GOAL_NOTES.clear()
    GOAL_NOTES.update(notes)
    if default:
        GOAL_DEFAULT[0] = default


def path_note(e: Evaluation) -> str:
    """Por qué el cargo sirve para la meta declarada (determinístico por sector y familia)."""
    if e.family in GOAL_NOTES:
        return GOAL_NOTES[e.family]
    return GOAL_NOTES.get(e.sector, GOAL_DEFAULT[0])
