"""Verificación de vacantes antes de recomendarlas.

Cada vacante de la lista final se comprueba en su fuente original: que exista, que siga
abierta y que acepte postulaciones. Lo que no se puede comprobar queda marcado así; nunca
se presenta como verificado.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from radar_vacantes.boards import JobPosting
from radar_vacantes.sources import Http, HttpError, oracle_parts, workday_api

ABIERTA = "abierta y acepta postulaciones"
CERRADA = "cerrada o ya no acepta postulaciones"
NO_VERIFICABLE = "no verificable automáticamente"

_CLOSED = re.compile(
    r"no longer (accepting applications|available|open)|this (job|position|posting) (has been|is) "
    r"(closed|filled|expired)|job (is )?(closed|expired)|vacante (cerrada|finalizada|expirada)|"
    r"ya no (est[aá] disponible|recibe postulaciones)|position has been filled|"
    r"oferta (cerrada|finalizada|vencida)|the page you are looking for (doesn.t|does not) exist",
    re.IGNORECASE,
)
_LD = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


@dataclass(frozen=True, slots=True)
class Check:
    status: str
    evidence: str
    apply_url: str


def _date_ok(text: str, today: date) -> bool | None:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text or "")
    if not m:
        return None
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) >= today


def verify(p: JobPosting, client: Http, today: date) -> Check:
    url = p.official_job_url
    try:
        if p.source == "workday":
            api, _ = workday_api(p.board_slug)
            info: Any = client.get_json(f"{api}{p.source_job_id}").get("jobPostingInfo", {})
            can = info.get("canApply") is True and info.get("posted") is not False
            end_ok = _date_ok(str(info.get("endDate", "") or ""), today)
            if can and end_ok is not False:
                end = info.get("endDate") or "sin fecha de cierre"
                return Check(ABIERTA, f"Workday: canApply=true; cierre {end}", url)
            return Check(
                CERRADA,
                f"Workday: canApply={info.get('canApply')}, cierre {info.get('endDate')}",
                url,
            )
        if p.source == "oracle":
            host, site = oracle_parts(p.board_slug)
            data: Any = client.get_json(
                f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                f"?onlyData=true&finder=ById;Id=%22{p.source_job_id}%22,siteNumber={site}"
            )
            items: Any = data.get("items") or []
            if not items:
                return Check(CERRADA, "Oracle: la requisición ya no aparece", url)
            end_ok = _date_ok(str(items[0].get("ExternalPostedEndDate", "") or ""), today)
            if end_ok is False:
                return Check(CERRADA, "Oracle: fecha de cierre vencida", url)
            return Check(ABIERTA, "Oracle: requisición publicada", url)
        html = client.get_text(url)
        # Primero los datos estructurados del aviso; el texto de cierre solo cuenta si es
        # visible: los portales traen esas frases en sus traducciones internas (Magneto).
        for block in _LD.findall(html):
            try:
                ld: Any = json.loads(block)
            except ValueError:
                continue
            if isinstance(ld, dict) and ld.get("@type") == "JobPosting":
                ok = _date_ok(str(ld.get("validThrough", "") or "")[:10], today)
                if ok is False:
                    return Check(CERRADA, f"validThrough {ld.get('validThrough')} vencida", url)
                return Check(
                    ABIERTA,
                    f"aviso publicado (vigente hasta {ld.get('validThrough') or 'sin fecha'})",
                    url,
                )
        visible = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
        if _CLOSED.search(visible[:300000]):
            return Check(CERRADA, "la página del aviso dice que está cerrada", url)
        if p.source == "magneto":
            return Check(NO_VERIFICABLE, "Magneto: la página no trae los datos del aviso", url)
        if p.source in (
            "greenhouse",
            "lever",
            "ashby",
            "smartrecruiters",
            "workable",
            "recruitee",
            "amazon",
            "impactpool",
            "hirehive",
            "cornerstone",
            "successfactors",
        ):
            return Check(
                ABIERTA, f"listada hoy en la API pública de {p.source}; página responde", url
            )
        if p.source == "indeed":
            return Check(
                NO_VERIFICABLE,
                "Indeed responde, pero hay que confirmar en el portal del empleador",
                url,
            )
        return Check(ABIERTA, "la página del aviso responde", url)
    except HttpError as exc:
        if exc.status in (404, 410):
            return Check(CERRADA, f"HTTP {exc.status}: el aviso ya no existe", url)
        return Check(NO_VERIFICABLE, f"HTTP {exc.status}", url)
    except Exception as exc:  # noqa: BLE001 — cualquier otro fallo: no verificable, nunca "abierta"
        return Check(NO_VERIFICABLE, type(exc).__name__, url)
