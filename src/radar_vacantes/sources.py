"""Descubrimiento: lee cada fuente del inventario con un resultado explícito.

Una fuente que falla, bloquea o pide sesión **no** se registra como "sin vacantes": cada
lectura termina en un `Outcome` distinto. Solo lectura; los endpoints de búsqueda de
Workday usan POST porque así funciona su búsqueda pública, pero no escriben nada.

El cliente real (`UrllibHttp`) se identifica con su propio User-Agent, respeta robots.txt,
espacia los pedidos a un mismo host y solo abre URLs http(s). Todo contenido recibido es
dato no confiable.
"""

from __future__ import annotations

import http.client
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from radar_vacantes.boards import _PARSERS as _BOARD_PARSERS
from radar_vacantes.boards import (
    ATS_GREENHOUSE,
    ATS_SMARTRECRUITERS,
    SUPPORTED_ATS,
    BoardRef,
    JobPosting,
    board_url,
    html_to_text,
    parse_greenhouse,
    parse_smartrecruiters,
    unpublished,
)

METHOD_WORKDAY = "workday"
METHOD_ORACLE = "oracle"
METHOD_SUCCESSFACTORS = "successfactors"
METHOD_PAGE = "page"  # portal propio o sin API pública: se comprueba acceso y queda manual
SUPPORTED_METHODS = frozenset(
    {*SUPPORTED_ATS, METHOD_WORKDAY, METHOD_ORACLE, METHOD_SUCCESSFACTORS, METHOD_PAGE}
)


class Outcome(StrEnum):
    """Resultado de leer una fuente. Nunca se confunde error con ausencia de vacantes."""

    OK = "ok"  # respondió y trajo vacantes
    EMPTY = "empty"  # respondió bien, cero vacantes
    ERROR = "error"  # fallo de red, formato o servidor
    BLOCKED = "blocked"  # 401/403, CAPTCHA o anti-bots: no se rodea
    LOGIN_REQUIRED = "login"  # exige iniciar sesión
    NOT_FOUND = "not_found"  # identificador del portal ya no existe
    MANUAL = "manual"  # accesible pero sin API legible: revisión manual
    UNSUPPORTED = "unsupported"  # método aún sin conector


#: Resultados en los que la fuente se leyó de verdad (cuentan para cobertura y cierres).
READ_OUTCOMES = frozenset({Outcome.OK, Outcome.EMPTY})


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Una fuente concreta de una empresa del universo."""

    company_id: str
    company: str
    method: str
    ref: str  # slug del ATS o URL base del portal
    queries: tuple[str, ...] = ()  # búsquedas de texto (Workday/Oracle); vacío = todo

    @property
    def key(self) -> str:
        return f"{self.method}:{self.ref}"


@dataclass(slots=True)
class SourceResult:
    """Lo que devolvió una fuente en un barrido."""

    spec: SourceSpec
    outcome: Outcome
    postings: list[JobPosting] = field(default_factory=lambda: [])
    detail: str = ""
    pages: int = 0
    truncated: bool = False  # la fuente solo expone una parte de sus vacantes


class HttpError(Exception):
    """Respuesta HTTP no exitosa con su código y un fragmento del cuerpo."""

    def __init__(self, status: int, body: str = "") -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body[:2000]


class FormatError(ValueError):
    """La fuente respondió, pero no con la forma que su conector conoce.

    No equivale a "sin vacantes": si se tomara como lectura vacía, el historial declararía
    cerradas todas las vacantes que esa fuente tenía en la corrida anterior."""


def _items(payload: Any, *path: str) -> list[Any]:
    """La lista en `payload[path...]` (sin ruta: el payload mismo). Si el contenedor falta, es
    null o no es una lista, `FormatError`; solo una lista vacía es una lectura vacía legítima."""
    node = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise FormatError(f"formato inesperado: falta {'.'.join(path)}")
        node = node[key]
    if not isinstance(node, list):
        raise FormatError(f"formato inesperado: {'.'.join(path) or 'la respuesta'} no es una lista")
    return node


class Http(Protocol):
    """Cliente HTTP inyectable: las pruebas no tocan la red."""

    def get_json(self, url: str) -> Any: ...
    def post_json(self, url: str, body: dict[str, Any]) -> Any: ...
    def get_text(self, url: str) -> str: ...


def _tls_context() -> ssl.SSLContext:
    """Verificación TLS con el almacén de confianza del sistema operativo (truststore).

    Algunos portales colombianos no envían la cadena intermedia completa; el sistema la
    completa y Python solo no. Nunca se desactiva la verificación."""
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:  # pragma: no cover — sin truststore, verificación estándar
        return ssl.create_default_context()


_TLS = _tls_context()

#: User-Agent honesto: identifica la herramienta; no se hace pasar por un navegador.
USER_AGENT = "Mozilla/5.0 (compatible; radar-de-vacantes/1.0; solo lectura)"
#: Nombre con el que se consultan las reglas de robots.txt.
ROBOTS_AGENT = "radar-de-vacantes"


class RobotsDisallowedError(HttpError):
    """robots.txt del sitio no permite leer esa ruta: no se lee y no se rodea."""

    def __init__(self, url: str) -> None:
        super().__init__(403, "robots.txt")
        self.url = url


def check_url(url: str) -> str:
    """Solo URLs http(s) con host. Una URL de un aviso es dato externo: `file://`,
    `ftp://` o una ruta relativa nunca llegan a `urlopen`."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"URL no permitida: {url[:120]!r}")
    return url


class RobotsCache:
    """Reglas de robots.txt por origen, leídas una vez por corrida.

    Sigue RFC 9309: 4xx = sin restricciones; 401/403, 5xx o red caída = no se lee el
    sitio en esta corrida. `fetch(url) -> (status, texto)` se inyecta en las pruebas."""

    def __init__(self, fetch: Callable[[str], tuple[int, str]], agent: str = ROBOTS_AGENT) -> None:
        self._fetch = fetch
        self._agent = agent
        self._rules: dict[str, urllib.robotparser.RobotFileParser | bool] = {}
        self._lock = threading.Lock()

    def _load(self, origin: str) -> urllib.robotparser.RobotFileParser | bool:
        try:
            status, text = self._fetch(f"{origin}/robots.txt")
        except Exception:  # noqa: BLE001 — sin robots.txt legible, no se lee el sitio
            return False
        if status in (401, 403) or status >= 500:
            return False
        if status >= 400:
            return True
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(text.splitlines())
        return parser

    def allowed(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            rules = self._rules.get(origin)
        if rules is None:
            rules = self._load(origin)
            with self._lock:
                self._rules.setdefault(origin, rules)
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(self._agent, url)


class HostThrottle:
    """Espacio mínimo entre pedidos al mismo host, aunque haya varios hilos."""

    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        if self.interval <= 0:
            return
        host = urllib.parse.urlsplit(url).netloc
        with self._lock:
            now = self._clock()
            slot = max(now, self._next.get(host, now))
            self._next[host] = slot + self.interval
        if slot > now:
            self._sleep(slot - now)


class UrllibHttp:
    """Cliente real: timeout duro, sin cookies ni credenciales, User-Agent propio,
    respeta robots.txt y espacia los pedidos a un mismo host."""

    MAX_BYTES = 20_000_000

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        user_agent: str = USER_AGENT,
        respect_robots: bool = True,
        per_host_interval: float = 0.5,
    ) -> None:
        self.timeout = timeout
        self.headers = {"User-Agent": user_agent, "Accept": "application/json, text/html"}
        self.robots = RobotsCache(self._fetch_robots) if respect_robots else None
        self.throttle = HostThrottle(per_host_interval)

    def _fetch_robots(self, url: str) -> tuple[int, str]:
        self.throttle.wait(url)
        req = urllib.request.Request(url, headers={"User-Agent": self.headers["User-Agent"]})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=_TLS) as resp:
                return resp.status, resp.read(500_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, ""

    def _open(self, req: urllib.request.Request) -> bytes:
        url = check_url(req.full_url)
        if self.robots is not None and not self.robots.allowed(url):
            raise RobotsDisallowedError(url)
        self.throttle.wait(url)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=_TLS) as resp:
                return resp.read(self.MAX_BYTES)
        except urllib.error.HTTPError as exc:
            body = exc.read(4000).decode("utf-8", "replace") if exc.fp else ""
            raise HttpError(exc.code, body) from exc

    def get_json(self, url: str) -> Any:
        return json.loads(self._open(urllib.request.Request(url, headers=self.headers)))

    def post_json(
        self, url: str, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> Any:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={**self.headers, "Content-Type": "application/json", **(headers or {})},
        )
        return json.loads(self._open(req))

    def get_text(self, url: str) -> str:
        return self._open(urllib.request.Request(url, headers=self.headers)).decode(
            "utf-8", "replace"
        )


_BLOCK_MARKERS = re.compile(
    r"captcha|datadome|cf-challenge|challenge-platform|perimeterx|access denied|"
    r"request blocked|are you a robot|incapsula",
    re.IGNORECASE,
)
_LOGIN_MARKERS = re.compile(
    r"(sign|log)\s?in to (view|see|apply)|inicia(r)? sesi[oó]n para|type=\"password\"",
    re.IGNORECASE,
)

_NET_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    TimeoutError,
    OSError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
)


def classify_error(exc: BaseException) -> tuple[Outcome, str]:
    """Traduce una excepción a un resultado explícito, sin adivinar vacantes."""
    if isinstance(exc, RobotsDisallowedError):
        return Outcome.BLOCKED, "robots.txt no permite leerla"
    if isinstance(exc, FormatError):
        return Outcome.ERROR, str(exc)
    if isinstance(exc, HttpError):
        if exc.status in (401, 407):
            return Outcome.LOGIN_REQUIRED, f"HTTP {exc.status}"
        if exc.status in (403, 429) or _BLOCK_MARKERS.search(exc.body):
            return Outcome.BLOCKED, f"HTTP {exc.status}"
        if exc.status in (404, 410, 422):  # 422: sitio de Workday inexistente
            return Outcome.NOT_FOUND, f"HTTP {exc.status}"
        return Outcome.ERROR, f"HTTP {exc.status}"
    return Outcome.ERROR, type(exc).__name__


# --------------------------------------------------------------------------- legado ATS


#: Contenedor de la lista de vacantes en la respuesta de cada ATS (vacío = la respuesta es la lista).
_LEGACY_LIST: dict[str, tuple[str, ...]] = {
    "greenhouse": ("jobs",),
    "ashby": ("jobs",),
    "lever": (),
    "smartrecruiters": ("content",),
    "workable": ("jobs",),
    "recruitee": ("offers",),
}


def _legacy_url(spec: SourceSpec, offset: int = 0) -> str:
    board = BoardRef(spec.method, spec.ref, spec.company)
    url = board_url(board)
    if spec.method == ATS_GREENHOUSE:
        url += "?content=true"  # descripción completa para evaluar requisitos
    if spec.method == ATS_SMARTRECRUITERS:
        url += f"&offset={offset}"
    return url


def read_legacy(
    spec: SourceSpec, client: Http, seen_at: str, *, max_pages: int = 30
) -> SourceResult:
    """Greenhouse, Ashby, Lever, SmartRecruiters (paginado), Workable y Recruitee."""
    board = BoardRef(spec.method, spec.ref, spec.company)
    if spec.method != ATS_SMARTRECRUITERS:
        payload = client.get_json(_legacy_url(spec))
        items = _items(payload, *_LEGACY_LIST[spec.method])
        # Lo que el ATS marca como no publicado se omite a propósito: no es formato raro.
        raw = sum(1 for job in items if not unpublished(spec.method, job))
        if spec.method == ATS_GREENHOUSE:
            postings = parse_greenhouse(board, payload, seen_at)
            jobs: Any = payload.get("jobs", []) if isinstance(payload, dict) else []
            by_id = {str(j.get("id")): j for j in jobs if isinstance(j, dict)}
            postings = [
                _with_description(
                    p, html_to_text(str(by_id.get(p.source_job_id, {}).get("content", "")))
                )
                for p in postings
            ]
        else:
            postings = _BOARD_PARSERS[spec.method](board, payload, seen_at)
        return _result(spec, postings, pages=1, raw=raw)
    postings: list[JobPosting] = []
    pages = 0
    offset = 0
    raw = 0
    while pages < max_pages:
        payload = client.get_json(_legacy_url(spec, offset))
        raw += len(_items(payload, *_LEGACY_LIST[spec.method]))
        pages += 1
        batch = parse_smartrecruiters(board, payload, seen_at)
        postings.extend(batch)
        total = int(payload.get("totalFound", 0)) if isinstance(payload, dict) else 0
        offset += len(batch)
        if not batch or offset >= total:
            break
    return _result(spec, postings, pages=pages, raw=raw)


def _with_description(p: JobPosting, text: str) -> JobPosting:
    return JobPosting(**{**{f: getattr(p, f) for f in p.__slots__}, "description": text})


def _result(
    spec: SourceSpec, postings: list[JobPosting], *, pages: int, raw: int = 0
) -> SourceResult:
    """Resultado de una lectura. `raw` = elementos que trajo la fuente: si trajo elementos y
    ninguno es una vacante legible (título e identificador o enlace), cambió el formato; tomarlo
    como "sin vacantes" cerraría en el historial todo lo anterior."""
    valid = [p for p in postings if p.title.strip() and (p.source_job_id or p.official_job_url)]
    if raw and not valid:
        raise FormatError(f"formato inesperado: {raw} elementos sin vacantes legibles")
    return SourceResult(spec, Outcome.OK if valid else Outcome.EMPTY, valid, pages=pages)


# --------------------------------------------------------------------------- Workday

_WD = re.compile(r"^https://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)")
_POSTED = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.IGNORECASE)


def workday_api(url: str) -> tuple[str, str]:
    """URL pública del sitio → (base API, base pública). ValueError si no es Workday."""
    m = _WD.match(url.strip())
    if not m:
        raise ValueError(f"URL de Workday no reconocida: {url}")
    tenant, pod, site = m.groups()
    host = f"https://{tenant}.{pod}.myworkdayjobs.com"
    return f"{host}/wday/cxs/{tenant}/{site}", f"{host}/{site}"


def workday_posted(text: str, today: date) -> str:
    """'Posted 3 Days Ago' → fecha ISO; '30+ Days Ago' → vacío (la fecha exacta sale del
    detalle). Nunca se inventa una antigüedad."""
    m = _POSTED.search(text or "")
    if not m:
        return ""
    word = m.group(1).lower()
    if word == "today":
        return today.isoformat()
    if word == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    if "+" in m.group(0):
        return ""
    return (today - timedelta(days=int(m.group(2)))).isoformat()


def read_workday(
    spec: SourceSpec,
    client: Http,
    seen_at: str,
    *,
    page_size: int = 20,
    max_pages: int = 40,
    today: date | None = None,
) -> SourceResult:
    api, public = workday_api(spec.ref)
    day = today or datetime.fromisoformat(seen_at).date()
    seen: dict[str, JobPosting] = {}
    pages = 0
    raw = 0
    for query in spec.queries or ("",):
        offset = 0
        while pages < max_pages:
            data: Any = client.post_json(
                f"{api}/jobs",
                {"appliedFacets": {}, "limit": page_size, "offset": offset, "searchText": query},
            )
            pages += 1
            jobs = _items(data, "jobPostings")
            raw += len(jobs)
            for job in jobs:
                if not isinstance(job, dict) or not job.get("externalPath"):
                    continue
                path = str(job["externalPath"])
                seen.setdefault(
                    path,
                    JobPosting(
                        source=METHOD_WORKDAY,
                        board_slug=spec.ref,
                        source_job_id=path,
                        company=spec.company,
                        title=str(job.get("title", "")).strip(),
                        location=str(job.get("locationsText", "")).strip(),
                        official_job_url=f"{public}{path}",
                        published_at=workday_posted(str(job.get("postedOn", "")), day),
                        first_seen_at=seen_at,
                    ),
                )
            total = int(data.get("total", 0)) if isinstance(data, dict) else 0
            offset += page_size
            if not jobs or offset >= total:
                break
    return _result(spec, list(seen.values()), pages=pages, raw=raw)


def workday_detail(spec_ref: str, path: str, client: Http) -> dict[str, str]:
    """Detalle de una vacante de Workday: fecha exacta, descripción, modalidad, cierre."""
    api, _ = workday_api(spec_ref)
    info: Any = client.get_json(f"{api}{path}").get("jobPostingInfo", {})
    if not isinstance(info, dict):
        return {}
    return {
        "published_at": str(info.get("startDate", "")),
        "end_date": str(info.get("endDate", "") or ""),
        "description": html_to_text(str(info.get("jobDescription", ""))),
        "time_type": str(info.get("timeType", "")),
        "remote_type": str(info.get("remoteType", "") or ""),
        "location": str(info.get("location", "")),
        "can_apply": str(info.get("canApply", "")),
    }


# --------------------------------------------------------------------------- Oracle

_ORA = re.compile(
    r"^https://([\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z]{2}(?:-[A-Z]{2})?/sites/([\w-]+)"
)


def oracle_parts(url: str) -> tuple[str, str]:
    m = _ORA.match(url.strip())
    if not m:
        raise ValueError(f"URL de Oracle Recruiting no reconocida: {url}")
    return m.group(1), m.group(2)


def read_oracle(
    spec: SourceSpec, client: Http, seen_at: str, *, page_size: int = 25, max_pages: int = 40
) -> SourceResult:
    host, site = oracle_parts(spec.ref)
    seen: dict[str, JobPosting] = {}
    pages = 0
    raw = 0
    for query in spec.queries or ("",):
        offset = 0
        while pages < max_pages:
            kw = f',keyword="{query}"' if query else ""
            finder = f"findReqs;siteNumber={site}{kw},limit={page_size},offset={offset},sortBy=POSTING_DATES_DESC"
            url = (
                f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                f"?onlyData=true&expand=requisitionList.secondaryLocations&finder="
                f"{urllib.parse.quote(finder, safe=';=,')}"
            )
            data: Any = client.get_json(url)
            pages += 1
            items = _items(data, "items")
            if not items:
                raise FormatError("formato inesperado: items vacío, sin requisitionList")
            head: Any = items[0]
            reqs = _items(head, "requisitionList")
            raw += len(reqs)
            for r in reqs:
                if not isinstance(r, dict) or not r.get("Id"):
                    continue
                rid = str(r["Id"])
                sec: Any = r.get("secondaryLocations") or []
                extra = (
                    [str(s.get("Name", "")) for s in sec if isinstance(s, dict)]
                    if isinstance(sec, list)
                    else []
                )
                seen.setdefault(
                    rid,
                    JobPosting(
                        source=METHOD_ORACLE,
                        board_slug=spec.ref,
                        source_job_id=rid,
                        company=spec.company,
                        title=str(r.get("Title", "")).strip(),
                        location=" ; ".join(
                            x for x in [str(r.get("PrimaryLocation", "")), *extra] if x
                        ),
                        official_job_url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}",
                        published_at=str(r.get("PostedDate", "") or ""),
                        first_seen_at=seen_at,
                        workplace_type=str(r.get("WorkplaceType", "") or ""),
                        department=str(r.get("JobFamily", "") or ""),
                        description=str(r.get("ShortDescriptionStr", "") or ""),
                    ),
                )
            total = int(head.get("TotalJobsCount", 0)) if isinstance(head, dict) else 0
            offset += page_size
            if not reqs or offset >= total:
                break
    return _result(spec, list(seen.values()), pages=pages, raw=raw)


# --------------------------------------------------------------------------- SuccessFactors

_MONTHS = {
    "ene": 1,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "abr": 4,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "ago": 8,
    "aug": 8,
    "sep": 9,
    "set": 9,
    "oct": 10,
    "nov": 11,
    "dic": 12,
    "dec": 12,
}
_SF_ROW = re.compile(r'<tr class="data-row">(.*?)</tr>', re.S)
_SF_TITLE = re.compile(
    r'class="jobTitle-link"[^>]*href="([^"]+)"[^>]*>([^<]+)<|'
    r'href="([^"]+)"[^>]*class="jobTitle-link"[^>]*>([^<]+)<'
)
_SF_LOC = re.compile(r'<span class="jobLocation">\s*([^<]+?)\s*</span>')
_SF_DATE = re.compile(r'<span class="jobDate[^"]*">\s*([^<]+?)\s*</span>')
_SF_DEPT = re.compile(r'<span class="jobDepartment">\s*([^<]+?)\s*</span>')
_SF_TOTAL = re.compile(r"paginationLabel[^>]*>.*?(?:de|of)\s*<b>\s*([\d.,]+)\s*</b>", re.S)


def parse_human_date(text: str) -> str:
    """'10 oct 2026', 'Oct 10, 2026' o '2026-10-10' → ISO. Formato dudoso → vacío."""
    t = text.strip().lower().replace(",", " ").replace(".", " ")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    parts = t.split()
    try:
        if len(parts) >= 3 and parts[0].isdigit() and parts[1][:3] in _MONTHS:
            return date(int(parts[2]), _MONTHS[parts[1][:3]], int(parts[0])).isoformat()
        if len(parts) >= 3 and parts[0][:3] in _MONTHS and parts[1].isdigit():
            return date(int(parts[2]), _MONTHS[parts[0][:3]], int(parts[1])).isoformat()
    except ValueError:
        return ""
    return ""


def read_successfactors(
    spec: SourceSpec, client: Http, seen_at: str, *, page_size: int = 25, max_pages: int = 40
) -> SourceResult:
    """Career Site Builder de SuccessFactors: la búsqueda pública viene renderizada en HTML."""
    base = spec.ref.rstrip("/")
    root = re.match(r"^https?://[^/]+", base)
    origin = root.group(0) if root else base
    seen: dict[str, JobPosting] = {}
    pages = 0
    for query in spec.queries or ("",):
        start = 0
        while pages < max_pages:
            url = (
                f"{base}/search/?q={urllib.parse.quote(query)}"
                f"&sortColumn=referencedate&sortDirection=desc&startrow={start}"
            )
            html = client.get_text(url)
            pages += 1
            if _BLOCK_MARKERS.search(html[:20000]) and not _SF_ROW.search(html):
                raise HttpError(403, "verificación anti-bots")
            rows = _SF_ROW.findall(html)
            for row in rows:
                t = _SF_TITLE.search(row)
                if not t:
                    continue
                href = t.group(1) or t.group(3)
                title = (t.group(2) or t.group(4) or "").strip()
                loc = _SF_LOC.search(row)
                dt = _SF_DATE.search(row)
                dept = _SF_DEPT.search(row)
                seen.setdefault(
                    href,
                    JobPosting(
                        source="successfactors",
                        board_slug=spec.ref,
                        source_job_id=href,
                        company=spec.company,
                        title=html_to_text(title),
                        location=loc.group(1).strip() if loc else "",
                        official_job_url=f"{origin}{href}" if href.startswith("/") else href,
                        published_at=parse_human_date(dt.group(1)) if dt else "",
                        first_seen_at=seen_at,
                        department=dept.group(1).strip() if dept else "",
                    ),
                )
            tot = _SF_TOTAL.search(html)
            total = int(re.sub(r"[.,]", "", tot.group(1))) if tot else 0
            start += page_size
            if not rows or start >= total:
                break
    return _result(spec, list(seen.values()), pages=pages)


# --------------------------------------------------------------------------- Magneto

_MG_LIST = re.compile(
    r'<script id="ItemList" type="application/ld\+json"[^>]*>(.*?)</script>', re.S
)


def _title_from_slug(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"-\d{5,}$", "", slug)
    return slug.replace("-", " ").strip().capitalize()


def read_magneto(
    spec: SourceSpec, client: Http, seen_at: str, *, max_pages: int = 15
) -> SourceResult:
    """Magneto365: página pública de empleos de la empresa (lista schema.org ItemList).

    El listado no trae fecha: queda "sin fecha", nunca estimada. ref = slug de la empresa."""
    seen: dict[str, JobPosting] = {}
    pages = 0
    for page in range(1, max_pages + 1):
        # ref "q:<término>" = búsqueda pública por cargo (todas las empresas de Magneto).
        if spec.ref.startswith("q:"):
            if page > 1:
                break
            url = f"https://www.magneto365.com/co/trabajos/ofertas-empleo-de-{spec.ref[2:]}"
        else:
            url = f"https://www.magneto365.com/co/empresas/{spec.ref}/empleos"
        html = client.get_text(url)
        pages += 1
        m = _MG_LIST.search(html)
        if not m:
            if page == 1:
                raise HttpError(404, "sin lista de empleos")
            break
        data: Any = json.loads(m.group(1))
        items: Any = data.get("itemListElement", []) if isinstance(data, dict) else []
        before = len(seen)
        for it in items if isinstance(items, list) else []:
            job_url = str(it.get("url", "")) if isinstance(it, dict) else ""
            if not job_url or job_url in seen:
                continue
            jid = re.search(r"-(\d{5,})$", job_url.rstrip("/"))
            seen[job_url] = JobPosting(
                source="magneto",
                board_slug=spec.ref,
                source_job_id=jid.group(1) if jid else job_url,
                company=spec.company,
                title=_title_from_slug(job_url),
                location="Colombia",
                official_job_url=job_url,
                published_at="",
                first_seen_at=seen_at,
            )
        if len(seen) == before:
            break
    res = _result(spec, list(seen.values()), pages=pages)
    # La página pública muestra a lo sumo 20; la paginación es del lado del navegador.
    res.truncated = spec.ref.startswith("q:") or len(seen) >= 20
    return res


# --------------------------------------------------------------------------- Impactpool / HireHive

_IP_BLOCK = re.compile(r"<div class='vacancy'>(.*?)<div class='vacancy-logo'", re.S)
_IP_LINK = re.compile(r'href="(/vacancies/(\d+))">([^<]+)</a>')
_IP_PARA = re.compile(r"<div class='ip-content-paragraph'>(.*?)</div>", re.S)


def read_impactpool(
    spec: SourceSpec, client: Http, seen_at: str, *, max_pages: int = 15
) -> SourceResult:
    """Sitios de carrera en Impactpool (BID y otros organismos): listado público en HTML.

    El listado trae la fecha de cierre, no la de publicación: la publicación queda vacía."""
    base = spec.ref.rstrip("/")
    seen: dict[str, JobPosting] = {}
    pages = 0
    for page in range(1, max_pages + 1):
        html = client.get_text(f"{base}/vacancies" + (f"?page={page}" if page > 1 else ""))
        pages += 1
        before = len(seen)
        for block in _IP_BLOCK.findall(html):
            link = _IP_LINK.search(block)
            if not link:
                continue
            paras = [html_to_text(x) for x in _IP_PARA.findall(block)]
            closes = next((x for x in paras if x.lower().startswith("closes")), "")
            level = paras[1] if len(paras) > 1 else ""
            place = paras[-1] if paras else ""
            seen.setdefault(
                link.group(2),
                JobPosting(
                    source="impactpool",
                    board_slug=spec.ref,
                    source_job_id=link.group(2),
                    company=spec.company,
                    title=html_to_text(link.group(3)),
                    location=place,
                    official_job_url=f"{base}{link.group(1)}",
                    published_at="",
                    first_seen_at=seen_at,
                    department=level,
                    description=f"{level}. {closes}",
                ),
            )
        if len(seen) == before:
            break
    return _result(spec, list(seen.values()), pages=pages)


def read_hirehive(
    spec: SourceSpec, client: Http, seen_at: str, *, max_pages: int = 10
) -> SourceResult:
    """HireHive: API JSON pública del tablero (ref = https://<empresa>.hirehive.com)."""
    base = spec.ref.rstrip("/")
    postings: list[JobPosting] = []
    pages = 0
    raw = 0
    url: str | None = f"{base}/api/v1/jobs"
    while url and pages < max_pages:
        data: Any = client.get_json(url)
        pages += 1
        jobs = _items(data, "jobs")
        raw += len(jobs)
        for j in jobs:
            if not isinstance(j, dict):
                continue
            country: Any = j.get("country") or {}
            cname = str(country.get("name", "")) if isinstance(country, dict) else ""
            desc: Any = j.get("description") or {}
            ts = j.get("publishedDate")
            published = ""
            if isinstance(ts, (int, str)) and str(ts).isdigit():
                published = datetime.fromtimestamp(int(ts), tz=UTC).date().isoformat()
            postings.append(
                JobPosting(
                    source="hirehive",
                    board_slug=spec.ref,
                    source_job_id=str(j.get("id", "")),
                    company=spec.company,
                    title=str(j.get("title", "")).strip(),
                    location=", ".join(x for x in (str(j.get("location") or ""), cname) if x),
                    official_job_url=str(j.get("hostedUrl", "")),
                    published_at=published,
                    first_seen_at=seen_at,
                    description=html_to_text(str(desc.get("html", "")))
                    if isinstance(desc, dict)
                    else "",
                )
            )
        nxt = data.get("nextPage") if isinstance(data, dict) else None
        url = str(nxt) if isinstance(nxt, str) and nxt.startswith("https://") else None
    return _result(spec, postings, pages=pages, raw=raw)


# --------------------------------------------------------------------------- Cornerstone

_CS_TOKEN = re.compile(r'"token":"([^"]+)"')
_CS_API = re.compile(r"(https://[a-z0-9]+\.api\.csod\.com)/")
_CS_SITE = re.compile(r"^(https://[\w-]+\.csod\.com)/ux/ats/careersite/(\d+)/home\?c=([\w-]+)")


def read_cornerstone(
    spec: SourceSpec, client: Http, seen_at: str, *, page_size: int = 50, max_pages: int = 10
) -> SourceResult:
    """Cornerstone OnDemand (Banco Mundial/IFC): el sitio público entrega a cualquier visitante
    un token anónimo de solo lectura para su búsqueda; se usa igual que lo hace la página."""
    m = _CS_SITE.match(spec.ref)
    if not m:
        raise ValueError(f"URL de Cornerstone no reconocida: {spec.ref}")
    origin, site, tenant = m.groups()
    html = client.get_text(spec.ref)
    tok, api = _CS_TOKEN.search(html), _CS_API.search(html)
    if not tok or not api:
        raise HttpError(403, "sin token público de búsqueda")
    if not isinstance(client, UrllibHttp):
        raise HttpError(501, "cliente sin soporte de token")
    postings: list[JobPosting] = []
    pages = 0
    raw = 0
    for page in range(1, max_pages + 1):
        body = {
            "careerSiteId": int(site), "careerSitePageId": int(site), "pageNumber": page,
            "pageSize": page_size, "cultureId": 1, "searchText": "", "cultureName": "en-US",
            "states": [], "countryCodes": [], "cities": [], "placeID": "", "radius": None,
            "postingsWithinDays": None, "customFieldCheckboxKeys": [],
            "customFieldDropdowns": [], "customFieldRadios": [],
        }
        data: Any = client.post_json(
            f"{api.group(1)}/rec-job-search/external/jobs", body,
            headers={"Authorization": f"Bearer {tok.group(1)}"},
        )
        pages += 1
        reqs = _items(data, "data", "requisitions")
        raw += len(reqs)
        inner: Any = data["data"]
        for r in reqs:
            if not isinstance(r, dict):
                continue
            locs: Any = r.get("locations") or []
            place = ", ".join(
                ", ".join(x for x in (str(lc.get("city") or ""), str(lc.get("country") or "")) if x)
                for lc in locs if isinstance(lc, dict)
            )
            rid = str(r.get("requisitionId", ""))
            posted = str(r.get("postingEffectiveDate", "") or "")
            mm = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", posted)
            postings.append(JobPosting(
                source="cornerstone", board_slug=spec.ref, source_job_id=rid,
                company=spec.company, title=str(r.get("displayJobTitle", "")).strip(),
                location=place,
                official_job_url=f"{origin}/ux/ats/careersite/{site}/home/requisition/{rid}?c={tenant}",
                published_at=f"{mm.group(3)}-{int(mm.group(1)):02d}-{int(mm.group(2)):02d}" if mm else "",
                first_seen_at=seen_at,
                description=html_to_text(str(r.get("externalDescription", "") or ""))
                + f"\nCierra: {r.get('postingExpirationDate', '')}",
            ))
        total = int(inner.get("totalCount", 0)) if isinstance(inner, dict) else 0
        if not reqs or page * page_size >= total:
            break
    return _result(spec, postings, pages=pages, raw=raw)


# --------------------------------------------------------------------------- Amazon


_PHENOM_ORIGIN = re.compile(r"^(https://[\w.-]+)(?:/([a-z]+)/([a-z]{2}))?")


def read_phenom(
    spec: SourceSpec, client: Http, seen_at: str, *, page_size: int = 50, max_pages: int = 10
) -> SourceResult:
    """Phenom People (BCG): la búsqueda pública del sitio es un POST a /widgets, el mismo que
    hace la página. ref = sitio con país y idioma (https://careers.bcg.com/global/en);
    queries = países a leer (p. ej. Colombia). Baja el detalle de cada vacante."""
    m = _PHENOM_ORIGIN.match(spec.ref)
    if not m:
        raise ValueError(f"URL de Phenom no reconocida: {spec.ref}")
    origin, country, lang = m.group(1), m.group(2) or "global", m.group(3) or "en"
    base = {"lang": f"{lang}_{country}", "deviceType": "desktop", "country": country,
            "siteType": "external", "global": True}
    postings: list[JobPosting] = []
    pages = 0
    raw = 0
    for place in spec.queries or ("",):
        offset = 0
        while pages < max_pages:
            body = {**base, "pageName": "search-results", "ddoKey": "refineSearch", "from": offset,
                    "jobs": True, "counts": False, "size": page_size, "clearAll": False,
                    "jdsource": "facets", "isSliderEnable": False, "pageId": "page20",
                    "keywords": "", "selected_fields": {"country": [place]} if place else {},
                    "locationData": {}}
            data: Any = client.post_json(f"{origin}/widgets", body)
            pages += 1
            jobs = _items(data, "refineSearch", "data", "jobs")
            raw += len(jobs)
            block: Any = data["refineSearch"]
            for j in jobs:
                if not isinstance(j, dict):
                    continue
                jid = str(j.get("jobId", ""))
                title = str(j.get("title", "")).strip()
                desc = str(j.get("descriptionTeaser", "") or "")
                try:
                    det: Any = client.post_json(
                        f"{origin}/widgets",
                        {**base, "pageName": "job", "ddoKey": "jobDetail", "jobId": jid},
                    )
                    job: Any = det.get("jobDetail", {}).get("data", {}).get("job", {})
                    if isinstance(job, dict) and job.get("description"):
                        desc = html_to_text(str(job["description"]))
                except (HttpError, AttributeError):
                    pass
                slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")
                postings.append(JobPosting(
                    source="phenom", board_slug=spec.ref, source_job_id=jid,
                    company=spec.company, title=title,
                    location=", ".join(x for x in (str(j.get("city") or ""),
                                                   str(j.get("country") or "")) if x),
                    official_job_url=f"{origin}/{country}/{lang}/job/{jid}/{slug}",
                    published_at=str(j.get("postedDate", "") or "")[:10],
                    first_seen_at=seen_at,
                    description=desc,
                ))
            total = int(block.get("totalHits", 0) or 0) if isinstance(block, dict) else 0
            offset += page_size
            if offset >= total or not jobs:
                break
    return _result(spec, postings, pages=pages, raw=raw)


def read_amazon(
    spec: SourceSpec, client: Http, seen_at: str, *, page_size: int = 100, max_pages: int = 20
) -> SourceResult:
    """amazon.jobs: búsqueda JSON pública por país (ref = código ISO3, p. ej. COL).

    Separa calificaciones básicas (indispensables) y preferidas (deseables)."""
    postings: list[JobPosting] = []
    pages = 0
    raw = 0
    offset = 0
    while pages < max_pages:
        url = (
            "https://www.amazon.jobs/en/search.json?normalized_country_code%5B%5D="
            f"{urllib.parse.quote(spec.ref)}&result_limit={page_size}&offset={offset}"
            "&sort=recent"
        )
        data: Any = client.get_json(url)
        pages += 1
        jobs = _items(data, "jobs")
        raw += len(jobs)
        for j in jobs:
            if not isinstance(j, dict):
                continue
            desc = "\n".join(
                [
                    html_to_text(str(j.get("description", ""))),
                    "Basic qualifications:\n"
                    + html_to_text(str(j.get("basic_qualifications", ""))),
                    "Preferred qualifications:\n"
                    + html_to_text(str(j.get("preferred_qualifications", ""))),
                ]
            )
            postings.append(
                JobPosting(
                    source="amazon",
                    board_slug=spec.ref,
                    source_job_id=str(j.get("id_icims", "")),
                    company=spec.company,
                    title=str(j.get("title", "")).strip(),
                    location=str(j.get("normalized_location", "") or j.get("location", "")),
                    official_job_url=f"https://www.amazon.jobs{j.get('job_path', '')}",
                    published_at=parse_human_date(str(j.get("posted_date", ""))),
                    first_seen_at=seen_at,
                    department=str(j.get("job_category", "")),
                    description=desc,
                )
            )
        hits = int(data.get("hits", 0)) if isinstance(data, dict) else 0
        offset += page_size
        if not jobs or offset >= hits:
            break
    return _result(spec, postings, pages=pages, raw=raw)


# --------------------------------------------------------------------------- portal propio


def read_page(spec: SourceSpec, client: Http, seen_at: str) -> SourceResult:
    """Portal sin API legible: solo comprueba acceso. Queda para revisión manual."""
    text = client.get_text(spec.ref)
    if _BLOCK_MARKERS.search(text[:20000]):
        return SourceResult(spec, Outcome.BLOCKED, detail="verificación anti-bots")
    if _LOGIN_MARKERS.search(text[:50000]):
        return SourceResult(spec, Outcome.LOGIN_REQUIRED, detail="la página pide sesión")
    return SourceResult(spec, Outcome.MANUAL, detail="accesible; sin API pública legible")


# --------------------------------------------------------------------------- despacho

Reader = Callable[[SourceSpec, Http, str], SourceResult]
_READERS: dict[str, Reader] = {
    **dict.fromkeys(SUPPORTED_ATS, read_legacy),
    METHOD_WORKDAY: read_workday,
    METHOD_ORACLE: read_oracle,
    METHOD_SUCCESSFACTORS: read_successfactors,
    "amazon": read_amazon,
    "magneto": read_magneto,
    "impactpool": read_impactpool,
    "hirehive": read_hirehive,
    "cornerstone": read_cornerstone,
    "phenom": read_phenom,
    METHOD_PAGE: read_page,
}


def read_source(
    spec: SourceSpec,
    client: Http,
    seen_at: str,
    *,
    retries: int = 2,
    sleep: Callable[[float], None] = time.sleep,
) -> SourceResult:
    """Lee una fuente. Cualquier fallo queda como resultado explícito, nunca como vacío.

    Los errores pasajeros (timeout, 5xx) se reintentan con espera; un bloqueo, un 404 o
    un pedido de sesión no se reintentan."""
    reader = _READERS.get(spec.method)
    if reader is None:
        return SourceResult(spec, Outcome.UNSUPPORTED, detail=f"sin conector para {spec.method}")
    result = SourceResult(spec, Outcome.ERROR)
    retried = 0
    for attempt in range(retries + 1):
        retried = attempt
        try:
            return reader(spec, client, seen_at)
        except (*_NET_ERRORS, HttpError) as exc:
            outcome, detail = classify_error(exc)
            result = SourceResult(spec, outcome, detail=detail)
            # Un formato desconocido no es pasajero: reintentar no lo arregla.
            if outcome is not Outcome.ERROR or isinstance(exc, FormatError) or attempt == retries:
                break
            sleep(2.0 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001 — un conector roto no tumba las demás fuentes
            # p. ej. RecursionError de un JSON hostil muy anidado: no es pasajero.
            result = SourceResult(
                spec, Outcome.ERROR, detail=f"fallo inesperado del conector: {type(exc).__name__}"
            )
            break
    if result.outcome is Outcome.ERROR and retried:
        result.detail += f" (tras {retried} {'reintento' if retried == 1 else 'reintentos'})"
    return result

