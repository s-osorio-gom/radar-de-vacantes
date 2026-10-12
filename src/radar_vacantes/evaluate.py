"""Evaluación determinística de afinidad.

Separada del descubrimiento: aquí no se borra nada; cada vacante recibe relevancia,
requisitos (indispensables vs. deseables), brechas frente al perfil, condición de
autorización de trabajo, antigüedad y un puntaje. Ningún cálculo se delega a un modelo.

Reglas de evidencia: nunca se presume un permiso de trabajo no documentado; un salario
ausente no resta; una fecha ausente se reporta como "sin fecha", no se estima.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from radar_vacantes.boards import JobPosting
from radar_vacantes.compensation import CompGate, Interval, parse_salary


class Relevance(StrEnum):
    ALTA = "alta"
    MEDIA = "media"
    BAJA = "baja"
    NULA = "nula"


class Authorization(StrEnum):
    COLOMBIA = "Colombia"
    REMOTO = "remoto desde Colombia"
    PATROCINIO = "internacional: ofrece patrocinio de visa"
    MULTILATERAL = "organismo internacional: revisar requisito de nacionalidad"
    REQUIERE = "internacional: exige autorización de trabajo previa"
    SIN_INFO = "internacional: no dice si patrocina"


class Fit(StrEnum):
    COMPLETO = "cumple completamente"
    PARCIAL = "cumple parcialmente"
    BAJO = "cumple poco"
    SIN_DATOS = "sin descripción para comparar"


@dataclass(frozen=True, slots=True)
class Profile:
    """Lo que el perfil documenta (config/perfil.yaml): años, herramientas, fortalezas y gustos."""

    years: int
    english: str  # nivel autoevaluado, p. ej. "B2+"
    tools: frozenset[str]  # en minúsculas
    certifications: frozenset[str]
    has_masters: bool
    degree: str
    strengths: tuple[tuple[str, str], ...]  # (patrón, razón de encaje)
    sector_points: tuple[tuple[str, int], ...] = ()
    family_points: tuple[tuple[str, int], ...] = ()
    cpg_exception: tuple[int, tuple[str, ...], str] = (1, (), "")
    floor_monthly_cop: int = 0  # piso salarial mensual; 0 = sin piso declarado
    # None = los valores por defecto del código (_TRACK_SECTORS, _CPG).
    entry_track_sectors: tuple[tuple[str, str], ...] | None = None
    cpg_sectors: frozenset[str] | None = None
    english_level: str = ""  # A1…C2 o NATIVO; vacío = se interpreta `english` con cautela

    @classmethod
    def from_config(cls, cfg: Any) -> Profile:
        """Perfil desde el YAML. Un perfil mal formado se rechaza con un mensaje que dice
        qué campo corregir (`ProfileError`), nunca con una traza ni con valores supuestos."""
        if not isinstance(cfg, dict):
            raise ProfileError("el perfil debe ser un diccionario YAML (clave: valor)")
        prefs = _mapping(cfg, "preferences")
        exc = _mapping(prefs, "cpg_exception", "preferences.")
        strengths: list[tuple[str, str]] = []
        for i, s in enumerate(_seq(cfg, "strengths")):
            if not isinstance(s, dict) or not s.get("pattern") or not s.get("reason"):
                raise ProfileError(f"strengths[{i}] debe tener 'pattern' y 'reason'")
            strengths.append((_regex(s["pattern"], f"strengths[{i}].pattern"), str(s["reason"])))
        title_re = exc.get("title_pattern") or ""
        return cls(
            years=_int(cfg, "years_relevant", required=True),
            english=_text(cfg, "english", required=True),
            english_level=_english_level(cfg),
            tools=frozenset(str(t).lower() for t in _seq(cfg, "tools")),
            certifications=frozenset(str(c).lower() for c in _seq(cfg, "certifications")),
            has_masters=_bool(cfg, "has_masters"),
            degree=_text(cfg, "degree"),
            strengths=tuple(strengths),
            sector_points=_points(prefs, "sector_points"),
            family_points=_points(prefs, "family_points"),
            cpg_exception=(
                _int(exc, "min_tier", default=1, where="preferences.cpg_exception."),
                tuple(str(f) for f in _seq(exc, "families", "preferences.cpg_exception.")),
                _regex(title_re, "preferences.cpg_exception.title_pattern") if title_re else "",
            ),
            floor_monthly_cop=_int(cfg, "floor_monthly_cop", default=0),
            entry_track_sectors=_labels(prefs, "entry_track_sectors"),
            cpg_sectors=_sectors(exc),
        )


class ProfileError(ValueError):
    """El perfil YAML no tiene la forma esperada."""


def _mapping(cfg: dict[str, Any], key: str, where: str = "") -> dict[str, Any]:
    value = cfg.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProfileError(f"{where}{key} debe ser un diccionario")
    return value


def _seq(cfg: dict[str, Any], key: str, where: str = "") -> list[Any]:
    value = cfg.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProfileError(f"{where}{key} debe ser una lista, p. ej. [excel, sql]")
    return value


def _int(
    cfg: dict[str, Any], key: str, *, required: bool = False, default: int = 0, where: str = ""
) -> int:
    value = cfg.get(key)
    if value is None:
        if required:
            raise ProfileError(f"falta {where}{key}")
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProfileError(f"{where}{key} debe ser un entero mayor o igual a 0")
    return value


def _text(cfg: dict[str, Any], key: str, *, required: bool = False) -> str:
    value = cfg.get(key)
    if value is None or value == "":
        if required:
            raise ProfileError(f"falta {key}")
        return ""
    if isinstance(value, (dict, list)):
        raise ProfileError(f"{key} debe ser un texto")
    return str(value)


def _bool(cfg: dict[str, Any], key: str) -> bool:
    value = cfg.get(key, False)
    if not isinstance(value, bool):
        raise ProfileError(f"{key} debe ser true o false")
    return value


def _regex(pattern: Any, where: str) -> str:
    try:
        re.compile(str(pattern))
    except re.error as exc:
        raise ProfileError(f"{where}: expresión regular inválida ({exc})") from exc
    return str(pattern)


def _points(prefs: dict[str, Any], key: str) -> tuple[tuple[str, int], ...]:
    out: list[tuple[str, int]] = []
    for k, v in _mapping(prefs, key, "preferences.").items():
        if isinstance(v, bool) or not isinstance(v, int):
            raise ProfileError(f"preferences.{key}.{k} debe ser un entero (puede ser negativo)")
        out.append((str(k), v))
    return tuple(out)


ENGLISH_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2", "NATIVO")


def _english_level(cfg: dict[str, Any]) -> str:
    value = cfg.get("english_level")
    if value is None:
        return ""
    level = str(value).strip().upper().replace("NATIVE", "NATIVO")
    if isinstance(value, (bool, dict, list)) or level not in ENGLISH_LEVELS:
        raise ProfileError(f"english_level debe ser uno de {', '.join(ENGLISH_LEVELS)}")
    return level


def english_covers_fluent(profile: Profile) -> bool:
    """¿El perfil cubre un "inglés fluido/avanzado" exigido? Con `english_level`, manda ese
    campo. Con texto libre se lee con cautela: un nivel alto cuenta solo si no hay a la vez un
    nivel menor, una negación o una meta ("no fluido", "camino a C1", "C1 en curso").
    Ante la duda, no lo cubre (y el aviso muestra la brecha)."""
    if profile.english_level:
        return profile.english_level in ("C1", "C2", "NATIVO")
    text = profile.english
    return bool(
        _ENGLISH_DECLARED_HIGH.search(text)
        and not _ENGLISH_DECLARED_LOWER.search(text)
        and not _ENGLISH_DOUBT.search(text)
    )


def _labels(prefs: dict[str, Any], key: str) -> tuple[tuple[str, str], ...] | None:
    """Sin la clave: valores por defecto. Para apagar la regla, `{}` explícito: un null suele
    ser una clave a medio escribir y no debe apagarla en silencio."""
    if key not in prefs:
        return None
    if prefs[key] is None:
        raise ProfileError(f"preferences.{key} está vacío: quítelo o use {{}} para apagar la regla")
    out: list[tuple[str, str]] = []
    for k, v in _mapping(prefs, key, "preferences.").items():
        if not isinstance(k, str) or not isinstance(v, str) or not v:
            raise ProfileError(f"preferences.{key}.{k} debe ser sector: texto (la etiqueta)")
        out.append((k, v))
    return tuple(out)


def _sectors(exc: dict[str, Any]) -> frozenset[str] | None:
    where = "preferences.cpg_exception.sectors"
    if "sectors" not in exc:
        return None
    if exc["sectors"] is None:
        raise ProfileError(f"{where} está vacío: quítelo o use [] para apagar la excepción")
    items = _seq(exc, "sectors", "preferences.cpg_exception.")
    if not all(isinstance(x, str) and x for x in items):
        raise ProfileError(f"{where} debe ser una lista de nombres de sector")
    return frozenset(items)


@dataclass(slots=True)
class Evaluation:
    posting: JobPosting
    company_id: str
    sector: str
    tier: int
    relevance: Relevance
    family: str
    authorization: Authorization
    fit: Fit
    must: list[str] = field(default_factory=lambda: [])
    nice: list[str] = field(default_factory=lambda: [])
    reasons: list[str] = field(default_factory=lambda: [])
    gaps: list[str] = field(default_factory=lambda: [])
    age_days: int | None = None
    recency: str = "sin fecha"
    salary_note: str = "no publicado"
    seniority: str = ""
    score: int = 0
    next_step: str = ""
    also_at: list[str] = field(default_factory=lambda: [])


def norm(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return " ".join(plain.casefold().split())


# ------------------------------------------------------------------ relevancia

_FAMILIES: tuple[tuple[str, int, re.Pattern[str]], ...] = tuple(
    (name, weight, re.compile(pattern, re.IGNORECASE))
    for name, weight, pattern in (
        (
            "pricing_revenue",
            10,
            r"\b(pricing|revenue (management|growth|strategy|optimi[sz]ation|analyst)|monetization|"
            r"rgm|top ?line|precios|rentabilidad|revenue manager)\b",
        ),
        (
            "corporate_finance",
            10,
            r"\b(corporate (development|finance|banking)|investment bank\w*|m&a|mergers|valuation|"
            r"capital markets|equity research|investment (analyst|associate|officer)|"
            r"banca de inversi[oó]n|finanzas corporativas|mercados? de capitales|private equity|"
            r"debt capital|leveraged finance|structured finance|credit (analyst|research|officer)|"
            r"fundamentals financial|asset management|portfolio (analyst|manager))\b",
        ),
        (
            "strategy",
            9,
            r"\b(strateg(y|ic)|estrat[eé]gi\w*|management consult\w*|consultor\w* estrat|"
            r"associate consultant|business analyst.*consult|planning (analyst|specialist))\b",
        ),
        (
            "fpa",
            9,
            r"\b(fp&a|financial (planning|analyst|analysis)|finance (analyst|manager|business partner)|"
            r"business partner financ\w*|planeaci[oó]n financiera|analista financier\w*|control de gesti[oó]n|"
            r"commercial finance|finanzas comerciales|budget\w*|presupuest\w*)\b",
        ),
        (
            "economics",
            8,
            r"\b(econom(ist|ista|ic research|ics)|investigaciones econ[oó]micas|macro\w*|research analyst)\b",
        ),
        (
            "commercial",
            8,
            r"\b(go.?to.?market|gtm|commercial (analyst|excellence|strategy|intelligence|planning)|"
            r"inteligencia comercial|planeaci[oó]n comercial|revenue operations|revops|"
            r"sales (strategy|operations|planning)|category manag\w*|trade marketing)\b",
        ),
        (
            "treasury_risk",
            7,
            r"\b(treasury|tesorer[ií]a|market risk|riesgo de mercado|liquidity|alm|fx|"
            r"credit risk|riesgo de cr[eé]dito)\b",
        ),
        (
            "bizops",
            7,
            r"\b(business operations|bizops|strategy (&|and) operations|operations strategy|chief of staff|"
            r"business (analyst|analytics)|analytics consultant|business consultant)\b",
        ),
        (
            "finance_general",
            5,
            r"\b(finance|financial|finanzas|financier\w*|investment|inversi[oó]n)\b",
        ),
        (
            "data",
            4,
            r"\b(data analyst|analytics|business intelligence|analista de datos|anal[ií]tica)\b",
        ),
    )
)
_OFF_PROFILE = re.compile(
    r"\b(engineer|developer|scientist|designer|architect|ingenier[oa]|desarrollador|nurse|enfermer\w*|"
    r"driver|conductor|technician|t[eé]cnico|warehouse|bodega|recruiter|reclutador|counsel|abogad\w*|"
    r"paralegal|customer (service|care|support)|servicio al cliente|call center|cajer\w*|"
    r"asesor\w* (comercial|de servicio)|sales (representative|executive|development)|account executive|"
    r"payroll|n[oó]mina|bookkeep\w*|auxiliar|operario|mec[aá]nic\w*|medical|m[eé]dic\w*|pharmac\w*|"
    r"clinical|cl[ií]nic\w*|compras|procurement|purchasing|abastecimiento|supply (planning|chain)|"
    r"log[ií]stic\w*|workflow|workforce|scheduling)\b",
    re.IGNORECASE,
)
#: Funciones que no son la trayectoria objetivo aunque el título diga "estrategia":
#: compras, cadena de suministro, logística y planeación de personal.
_FUNC_OFF = re.compile(
    r"\b(compras|procurement|purchasing|abastecimiento|contrataci[oó]n|"
    r"supply (planning|chain)|log[ií]stic\w*|workflow|workforce|scheduling|"
    r"client development|location analyst|(digital|media|brand|content|creative) strategy|"
    r"e-?trading|gesti[oó]n de accesos|contable|contabilidad|accounting|impuestos|\btax\b|"
    r"precios de transferencia|transfer pricing|kyc|aml|client service|event planner|"
    r"auditor\w*|middle office|payroll|n[oó]mina|account maintenance|operations analyst|"
    r"\bsap\b|ewm|brand|communications|comunicaciones|inteligencia de mercado|ciberseguridad|"
    r"cybersecurity|developer|desarrollador|technology consult\w*|consultor sap|hub\b|capacitaci\w*|gesti[oó]n del cambio|"
    r"change management|training|reclutamiento)\b",
    re.IGNORECASE,
)
_DESC_OFF = re.compile(
    r"\b(payroll|benefits? administration|bookkeeping|n[oó]mina|liquidaci[oó]n de n[oó]mina)\b",
    re.IGNORECASE,
)
_DESC_FINANCE = re.compile(
    r"\b(financial model\w*|modelo\w* financier\w*|valuation|valoraci[oó]n|dcf|p&l|pricing|forecast\w*|"
    r"business case\w*|pron[oó]stic\w*|rentabilidad|profitability)\b",
    re.IGNORECASE,
)

_OVER = re.compile(
    r"\b(principal|lead investment officer|senior investment officer|director|directora|head of|vp|vice president|vicepresident\w*|chief|svp|evp|"
    r"managing director|executive director|partner|socio|gerente general|country manager)\b",
    re.IGNORECASE,
)
_SENIOR_MGR = re.compile(
    r"\b(senior manager|gerente senior|associate director|principal)\b", re.IGNORECASE
)
_RESTRICTED = re.compile(
    r"(discapacidad|disability|veterans? only|solo estudiantes|students only)", re.IGNORECASE
)
_JUNIOR = re.compile(
    r"\b(intern|internship|trainee|apprentice|practicante|aprendiz|pasant[ií]a|summer)\b",
    re.IGNORECASE,
)


def relevance_of(p: JobPosting) -> tuple[Relevance, str]:
    """Relevancia por título y área; la descripción solo sube un título genérico."""
    title = f"{p.title} {p.department}"
    best, family = 0, ""
    for name, weight, pattern in _FAMILIES:
        if weight > best and pattern.search(title):
            best, family = weight, name
    if _FUNC_OFF.search(p.title):
        return Relevance.NULA, "función fuera del objetivo"
    off = bool(_OFF_PROFILE.search(p.title))
    if off and best < 9:
        return Relevance.NULA, family or "fuera de perfil"
    if len(_DESC_OFF.findall(p.description[:6000])) >= 3:
        return Relevance.NULA, "contabilidad o nómina por descripción"
    desc_hits = len(_DESC_FINANCE.findall(p.description[:20000])) if p.description else 0
    if best >= 8:
        return Relevance.ALTA, family
    if best >= 5 or (best >= 4 and desc_hits >= 3):
        return Relevance.MEDIA, family
    if desc_hits >= 4:
        return Relevance.BAJA, "descripción financiera"
    return Relevance.NULA, family or "sin familia"


# ------------------------------------------------------------------ autorización

_CO = re.compile(
    r"\b(colombia|bogot[aá]|medell[ií]n|cali|barranquilla|cartagena|bucaramanga|pereira|"
    r"manizales|cundinamarca|antioquia|\bcol\b)\b",
    re.IGNORECASE,
)
_LATAM_REMOTE = re.compile(
    r"\b(latam|latin america|am[eé]rica latina|latinoam[eé]rica|south america|"
    r"americas|anywhere|worldwide|global remote)\b",
    re.IGNORECASE,
)
_REMOTE = re.compile(r"\b(remote|remoto|teletrabajo|work from home)\b", re.IGNORECASE)
_SPONSOR_YES = re.compile(
    r"(visa sponsorship (is )?(available|provided|offered)|we (will |can )?sponsor|"
    r"sponsorship (is )?available|"
    r"patrocinio de visa)",
    re.IGNORECASE,
)
_SPONSOR_NO = re.compile(
    r"((not|unable to|cannot|can't|won't|will not|do not|does not) (provide |offer )?"
    r"(visa |immigration )?sponsor|no (visa )?sponsorship|must (be|have) (legally |currently )?"
    r"(authori[sz]ed|eligible) to work|without (the need for )?(current or future )?"
    r"(visa )?sponsorship|work authori[sz]ation (is )?required|"
    r"(u\.?s\.?|us) citizen(ship)?|security clearance)",
    re.IGNORECASE,
)


def authorization_of(p: JobPosting, sector: str) -> Authorization:
    loc = f"{p.location} {p.workplace_type}"
    if _CO.search(loc):
        return Authorization.COLOMBIA
    text = p.description[:30000]
    if _REMOTE.search(loc) and _LATAM_REMOTE.search(loc):
        return Authorization.REMOTO
    if sector == "multilateral" and not _SPONSOR_NO.search(text):
        return Authorization.MULTILATERAL
    if _SPONSOR_NO.search(text):
        return Authorization.REQUIERE
    if _SPONSOR_YES.search(text):
        return Authorization.PATROCINIO
    return Authorization.SIN_INFO


# ------------------------------------------------------------------ requisitos

_MUST_HDR = re.compile(
    r"^\s*(requirements?|requisitos|qualifications|basic qualifications|minimum qualifications|"
    r"what you('ll)? (need|bring)|what we('re)? looking for|must.have|you have|about you|"
    r"all about you|perfil( requerido)?|qu[eé] buscamos|requerimientos|conocimientos y experiencia|"
    r"experiencia requerida|who you are)\b",
    re.IGNORECASE,
)
_NICE_HDR = re.compile(
    r"^\s*(preferred( qualifications)?|nice.to.have|bonus|pluses|a plus|deseable\w*|"
    r"valoraremos|se valorar[aá]|ideally|preferred skills|desirable)\b",
    re.IGNORECASE,
)
_OTHER_HDR = re.compile(
    r"^\s*(responsibilities|what you('ll)? do|key responsibilities|funciones|"
    r"responsabilidades|benefits|beneficios|about (us|the (team|role|company))|"
    r"our purpose|why join|qu[eé] ofrecemos|role|overview|tus principales desaf[ií]os)\b",
    re.IGNORECASE,
)
_NICE_INLINE = re.compile(
    r"\b(preferred|is a plus|nice to have|deseable|bonus|ideally|valorable|"
    r"preferiblemente|preferably)\b",
    re.IGNORECASE,
)
_YEARS = re.compile(
    r"(\d{1,2})\)?\s*(?:\+|or more|o m[aá]s)?\s*(?:(?:-|–|to|a)\s*(\d{1,2})\s*)?"
    r"(?:\+\s*)?(?:years?|yrs|años)",
    re.IGNORECASE,
)
_ENGLISH_HIGH = re.compile(
    r"\b(fluent|fluency|native|c1|c2|advanced english|ingl[eé]s (avanzado|fluido|nativo)|"
    r"excellent (written and (verbal|spoken)|english)|proficient in english)\b",
    re.IGNORECASE,
)
#: Nivel del perfil que ya cubre un "inglés fluido/avanzado" exigido (ver english_covers_fluent).
_ENGLISH_DECLARED_HIGH = re.compile(
    r"\b(c1|c2|nativ\w*|native|fluent|flu[ií]do|biling\w*)", re.IGNORECASE
)
_ENGLISH_DECLARED_LOWER = re.compile(
    r"\b(a1|a2|b1|b2|b[aá]sico|basic|intermedi\w*|elementary|elemental)", re.IGNORECASE
)
_ENGLISH_DOUBT = re.compile(
    r"\b(no|not|non|sin|without|camino|rumbo|hacia|towards?|preparando|preparing|en curso|"
    r"in progress|cursando|estudiando|studying|aprendiendo|learning|yet|todav[ií]a|a[uú]n|"
    r"meta|goal|objetivo|pronto|soon|casi|almost|pendiente|pending|working on)\b",
    re.IGNORECASE,
)
_MASTERS = re.compile(
    r"\b(mba|master'?s|maestr[ií]a|posgrado|post.?grad\w*|graduate degree|ph\.?d|"
    r"advanced (university )?degree)\b",
    re.IGNORECASE,
)
_CERTS = re.compile(r"\b(cfa|cpa|frm|acca|cma|amv|series \d+|caia|cia)\b", re.IGNORECASE)
_TOOLS = re.compile(
    r"\b(sap(?: fi/?co| s/?4 ?hana)?|hyperion|oracle|netsuite|anaplan|adaptive|tableau|"
    r"power ?bi|sql|python|r\b|vba|salesforce|bloomberg|capital iq|factset|excel|"
    r"alteryx|looker|snowflake|stata|google sheets)\b",
    re.IGNORECASE,
)


def _lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in re.split(r"[\n•▪◦·]|(?<=[.;])\s+(?=[A-ZÁÉÍÓÚ•-])", text):
        line = " ".join(raw.strip(" -*\t").split())
        if 3 <= len(line) <= 400:
            out.append(line)
    return out


def _is_header(line: str, pattern: re.Pattern[str]) -> bool:
    """Encabezado corto. Tolera palabras partidas por el HTML ("Qualification s")."""
    if len(line) >= 70:
        return False
    squashed = re.sub(r"(?<=\w) (?=\w\b)", "", line)
    return bool(pattern.search(line) or pattern.search(squashed))


_REQ_LINE = re.compile(
    r"\b(experien\w*|knowledge|proficien\w*|required|requerid\w*|degree|t[ií]tulo|"
    r"profesional en|años|years|conocimiento\w*|manejo|dominio|certifica\w*|"
    r"english|ingl[eé]s|fluent|bachelor|master|mba)\b",
    re.IGNORECASE,
)


def split_requirements(description: str) -> tuple[list[str], list[str]]:
    """Separa requisitos indispensables y deseables por encabezados y marcas en línea.
    Sin encabezados reconocibles, toma las líneas con forma de requisito."""
    must: list[str] = []
    nice: list[str] = []
    section = ""
    lines = _lines(description)
    for line in lines:
        if _is_header(line, _NICE_HDR):
            section = "nice"
            continue
        if _is_header(line, _MUST_HDR):
            section = "must"
            continue
        if _is_header(line, _OTHER_HDR):
            section = ""
            continue
        if section == "must":
            (nice if _NICE_INLINE.search(line) else must).append(line)
        elif section == "nice":
            nice.append(line)
    if not must:
        for line in lines:
            if _REQ_LINE.search(line) and line not in nice:
                (nice if _NICE_INLINE.search(line) else must).append(line)
    return must[:25], nice[:15]


#: "Up to 4 years", "maximum 4 years", "hasta 4 años": es un tope, no un mínimo.
_CEILING = re.compile(
    r"\b(up to|maximum( of)?|max\.?|no more than|at most|hasta|m[aá]ximo( de)?)\s*\(?$",
    re.IGNORECASE,
)


def _is_ceiling(line: str, start: int) -> bool:
    return bool(_CEILING.search(line[max(0, start - 20) : start].rstrip()))


def _years_required(lines: list[str]) -> int:
    best = 0
    for line in lines:
        for m in _YEARS.finditer(line):
            low = int(m.group(1))
            if _is_ceiling(line, m.start()):
                continue
            if 0 < low <= 20 and re.search(r"experien|years? (of|in)|años", line, re.IGNORECASE):
                best = max(best, low)
    return best


def _years_ceiling(lines: list[str]) -> int:
    """El tope de experiencia que admite el aviso, o 0 si no lo hay."""
    caps = [
        int(m.group(1))
        for line in lines
        for m in _YEARS.finditer(line)
        if _is_ceiling(line, m.start()) and 0 < int(m.group(1)) <= 20
    ]
    return min(caps) if caps else 0


_PREFERRED = re.compile(
    r"\b(or equivalent|o equivalente|preferabl\w*|preferred|deseable|is a plus|ideally|"
    r"valorable|preferiblemente)\b",
    re.IGNORECASE,
)
_REQUIRED_WORD = re.compile(
    r"\b(required|requerid\w*|indispensable|must|obligatori\w*|exige|se requiere)\b", re.IGNORECASE
)
#: Línea que *empieza* con el título exigido: "An advanced university degree in finance..."
#: es requisito aunque no diga "required".
_DEGREE_LEAD = re.compile(
    r"^(an?\s+|holds?\s+an?\s+)?(advanced (university )?degree|master'?s|mba|maestr[ií]a|"
    r"posgrado|graduate degree|t[ií]tulo de (maestr[ií]a|posgrado))",
    re.IGNORECASE,
)
#: "Certificación en Finanzas, Data Science, Gestión de Proyectos...": certificación exigida.
_CERT_LEAD = re.compile(
    r"^(certificaci[oó]n|certificado|certification|certified|professional certification)\b",
    re.IGNORECASE,
)
#: Historial ejecutando inversiones o transacciones: núcleo del cargo, no una brecha menor.
_TRACK_RECORD = re.compile(
    r"track record (of|in) (executing|leading|closing|originating|structuring)\b[^.]{0,80}"
    r"\b(investment|transaction|deal|debt|equity|loan|m&a)",
    re.IGNORECASE,
)


def gaps_and_reasons(
    p: JobPosting, must: list[str], nice: list[str], profile: Profile
) -> tuple[list[str], list[str], int]:
    """(brechas, razones de encaje, peso de brechas indispensables)."""
    gaps: list[str] = []
    reasons: list[str] = []
    weight = 0
    # Los requisitos duros (años, posgrado, certificaciones) a veces van en un párrafo fuera de
    # la sección reconocida: se buscan en toda línea con forma de requisito.
    extra = [
        ln
        for ln in _lines(p.description)
        if _REQ_LINE.search(ln) and not _NICE_INLINE.search(ln) and ln not in nice
    ]
    body = list(dict.fromkeys([*must, *extra]))
    years = _years_required(body)
    ceiling = _years_ceiling(body)
    if ceiling and profile.years > ceiling:
        weight += 3
        gaps.append(f"admite hasta {ceiling} años; el perfil declara {profile.years}")
    elif ceiling:
        reasons.append(f"admite hasta {ceiling} años de experiencia: estás dentro")
    if years > profile.years:
        weight += 2 if years - profile.years >= 3 else 1
        gaps.append(f"pide {years}+ años; el perfil declara {profile.years}")
    elif years:
        reasons.append(f"experiencia pedida ({years}+ años) dentro de lo que tienes")
    joined_must = " ".join(body)
    if _ENGLISH_HIGH.search(joined_must) and not english_covers_fluent(profile):
        weight += 1
        declared = profile.english_level or profile.english  # el campo que decidió
        gaps.append(f"pide inglés fluido/avanzado; el perfil declara {declared}")
    if not profile.has_masters and any(
        _MASTERS.search(ln)
        and (_REQUIRED_WORD.search(ln) or _DEGREE_LEAD.search(ln))
        and not re.search(
            r"\b(or equivalent|o equivalente|preferabl\w*|preferred|deseable)\b.*"
            r"(mba|master|maestr|posgrado)",
            ln,
            re.I,
        )
        and not (_DEGREE_LEAD.search(ln) and _PREFERRED.search(ln))
        for ln in body
    ):
        weight += 2
        gaps.append("exige posgrado (maestría/MBA)")
    named = sorted({m.group(1).lower() for m in _CERTS.finditer(joined_must)})
    for c in named:
        if c not in profile.certifications:
            weight += 2 if c in ("amv", "cpa", "cfa") else 1
            gaps.append(f"certificación {c.upper()} pedida")
    if not named and not profile.certifications:
        generic = next(
            (ln for ln in body if _CERT_LEAD.search(ln) and not _PREFERRED.search(ln)), None
        )
        if generic:
            weight += 1
            gaps.append(f"pide certificación: {generic[:90]}")
    if any(_TRACK_RECORD.search(ln) for ln in _lines(p.description)):
        weight += 3  # es el núcleo del cargo, no se cierra en la carta
        gaps.append("pide historial ejecutando inversiones o transacciones; no lo tienes")
    for t in sorted({m.group(1).lower() for m in _TOOLS.finditer(joined_must)}):
        key = t.replace(" ", "")
        if not any(key.startswith(x.replace(" ", "")) for x in profile.tools):
            weight += 1
            gaps.append(f"herramienta {t} pedida")
    nice_tools = sorted({m.group(1).lower() for m in _TOOLS.finditer(" ".join(nice))})
    missing_nice = [
        t
        for t in nice_tools
        if not any(t.replace(" ", "").startswith(x.replace(" ", "")) for x in profile.tools)
    ]
    if missing_nice:
        gaps.append("deseable: " + ", ".join(missing_nice))
    text = f"{p.title}\n{p.description[:30000]}"
    for pattern, reason in profile.strengths:
        if re.search(pattern, text, re.IGNORECASE):
            reasons.append(reason)
    return gaps[:8], list(dict.fromkeys(reasons))[:5], weight


# ------------------------------------------------------------------ evaluación completa

AUTH_POINTS = {
    Authorization.COLOMBIA: 20,
    Authorization.REMOTO: 18,
    Authorization.PATROCINIO: 12,
    Authorization.MULTILATERAL: 10,
    Authorization.SIN_INFO: 4,
    Authorization.REQUIERE: 0,
}
REL_POINTS = {Relevance.ALTA: 35, Relevance.MEDIA: 22, Relevance.BAJA: 10, Relevance.NULA: 0}
FIT_POINTS = {Fit.COMPLETO: 25, Fit.PARCIAL: 15, Fit.SIN_DATOS: 10, Fit.BAJO: 4}
TIER_POINTS = {1: 15, 2: 8, 3: 0}


def _age(published: str, today: date) -> int | None:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", published or "")
    if not m:
        return None
    try:
        return (today - date(int(m.group(1)), int(m.group(2)), int(m.group(3)))).days
    except ValueError:
        return None


def _salary_note(text: str, gate: CompGate | None, floor_cop: int) -> tuple[str, int]:
    rng = parse_salary(text)
    if rng is None:
        return ("publicado: " + text[:60]) if text else "no publicado", 0
    low = rng.low
    if rng.currency == "USD":
        if gate is None:
            return f"publicado: {text[:60]} (sin TRM para convertir)", 0
        low = low * gate.usd_cop
    monthly = (
        low / 12
        if rng.interval is Interval.ANNUAL
        else (low * 2080 / 12 if rng.interval is Interval.HOURLY else low)
    )
    if floor_cop <= 0:  # sin piso declarado: se informa el monto, no se puntúa
        return f"publicado: {text[:60]} (≈ COP {monthly:,.0f}/mes)", 0
    tag = "≥" if monthly >= floor_cop else "<"
    return f"publicado: {text[:60]} (≈ COP {monthly:,.0f}/mes, {tag} COP {floor_cop:,}/mes)", (
        3 if monthly >= floor_cop else -3
    )


_REQ_EVIDENCE = re.compile(
    r"\b(qualification\w*|requirements?|requisitos?|selection criteria|degree|t[ií]tulo|"
    r"bachelor\w*|master'?s|years|años|education|formaci[oó]n|profesional en|egresad\w*|"
    r"graduate|what you'?ll bring|all about you|you have|perfil)\b",
    re.IGNORECASE,
)
#: Fuentes cuyo listado trae solo el resumen del cargo: el aviso completo vive en otra página.
_SUMMARY_SOURCES = {"cornerstone", "impactpool"}
_REQ_HEADER = re.compile(
    r"(minimum |required |key )?(selection criteria|qualifications|requirements|requisitos|"
    r"what you'?ll bring|ideal candidate)\b",
    re.IGNORECASE,
)


def _publishes_requirements(p: JobPosting) -> bool:
    if p.source in _SUMMARY_SOURCES:
        # Encabezado real, no la palabra suelta dentro de las funciones ("clearance
        # requirements").
        return any(len(ln) < 60 and _REQ_HEADER.match(ln) for ln in _lines(p.description))
    return bool(_REQ_EVIDENCE.search(p.description))


_OPS = re.compile(
    r"\b(onboarding|support|soporte|operations?|operaciones|back.?office|kyc|aml|"
    r"client service|servicio|recruit\w*|it\b|tech|ai scientist|architect|data engineer)\b",
    re.IGNORECASE,
)
_NO_EXPERIENCE = re.compile(
    r"(with\s+no\s+prior\s+work\s+experience|no\s+prior\s+(work|professional)\s+experience|"
    r"(?<!con o )sin\s+experiencia\s+laboral)",
    re.IGNORECASE,
)
_MDB_PRO = re.compile(
    r"\b(economist|officer|specialist|especialista|economista|advis[eo]r|asesor)\b", re.IGNORECASE
)
_MDB_ENTRY = re.compile(
    r"\b(associate|junior|analyst|analista|consultant|consultor\w*|young professional|"
    r"assistant|asistente|intern|pasant\w*|research analyst)\b",
    re.IGNORECASE,
)
_ENTRY_TRACK = re.compile(
    r"\b(analyst|associate|consultant|consultor\w*|junior associate|program|programa|"
    r"business analyst|analista)\b",
    re.IGNORECASE,
)
#: En estos sectores, "Analyst" o "Associate" a secas es la puerta de entrada a la carrera
#: objetivo (consultoría estratégica, banca de inversión), aunque el título no diga "finanzas".
#: Valor por defecto; el perfil lo reemplaza con `preferences.entry_track_sectors`.
_TRACK_SECTORS = {
    "consultoria": "consultoría (carrera de entrada)",
    "banca_global": "banca (programa de entrada)",
    "banca_inversion": "banca (programa de entrada)",
    "gestor_activos": "banca (programa de entrada)",
}


def evaluate(
    p: JobPosting,
    *,
    company_id: str,
    sector: str,
    tier: int,
    profile: Profile,
    today: date,
    gate: CompGate | None = None,
    floor_cop: int | None = None,
) -> Evaluation:
    # El piso sale del perfil (`floor_monthly_cop`); antes se ignoraba y se usaba un valor fijo.
    floor = profile.floor_monthly_cop if floor_cop is None else floor_cop
    rel, family = relevance_of(p)
    track = (
        _TRACK_SECTORS
        if profile.entry_track_sectors is None
        else dict(profile.entry_track_sectors)
    )
    if (
        not _FUNC_OFF.search(p.title)
        and rel in (Relevance.NULA, Relevance.BAJA)
        and sector in track
        and tier == 1
        and _ENTRY_TRACK.search(p.title)
        and not _OFF_PROFILE.search(p.title)
        and not _OPS.search(p.title)
    ):
        rel, family = Relevance.ALTA, track[sector]
    auth = authorization_of(p, sector)
    must, nice = split_requirements(p.description) if p.description else ([], [])
    gaps, reasons, gap_weight = (
        gaps_and_reasons(p, must, nice, profile) if p.description else ([], [], 0)
    )
    no_experience = bool(p.description and _NO_EXPERIENCE.search(p.description))
    if no_experience:
        # Requisito de elegibilidad, no una brecha que se cierre en la carta (programa de entrada
        # de un banco global): con experiencia previa, la postulación no procede.
        gap_weight += 10
        gaps.insert(0, "no elegible: exige no tener experiencia laboral previa y tú la tienes")
    if (
        sector == "multilateral"
        and _MDB_PRO.search(p.title)
        and not _MDB_ENTRY.search(p.title)
    ):
        gap_weight += 3
        gaps.insert(0, "nivel profesional internacional: suele exigir maestría y 5+ años")
    if not p.description or (gap_weight == 0 and not _publishes_requirements(p)):
        # Sin requisitos publicados no hay con qué afirmar "cumple completamente" (hay
        # listados que traen solo la fecha de cierre o el resumen del cargo).
        fit = Fit.SIN_DATOS if not gaps else Fit.PARCIAL
    elif gap_weight == 0:
        fit = Fit.COMPLETO
    elif gap_weight <= 2:
        fit = Fit.PARCIAL
    else:
        fit = Fit.BAJO
    age = _age(p.published_at, today)
    if age is not None and age < 0:
        # Un día de adelanto es la zona horaria del portal; más es un dato malo: no se estima.
        age = 0 if age == -1 else None
    if age is None:
        recency, rec_pts = "sin fecha", 1
    elif age <= 15:
        recency, rec_pts = f"{age} días", 5
    elif age <= 20:
        recency, rec_pts = f"{age} días", 3
    elif age <= 90:
        recency, rec_pts = f"{age} días (más antigua)", 0
    elif age <= 180:
        recency, rec_pts = f"{age} días: confirmar que siga activa", -5
    else:
        # Avisos "siempre abiertos" de hace meses: bajan para dejar sitio.
        recency, rec_pts = f"{age} días: aviso viejo, confirmar que siga activa", -15
    seniority, sen_pts = "", 0
    if _OVER.search(p.title):
        seniority, sen_pts = "nivel directivo: por encima del perfil", -12
    elif _SENIOR_MGR.search(p.title):
        seniority, sen_pts = "gerencia senior: apuesta alta", -4
    elif _JUNIOR.search(p.title):
        seniority, sen_pts = "práctica o programa para estudiantes", -15
    # Un programa con año en el título ya pasado ("2025 LATAM Markets Analyst Program" leído en
    # 2026) es una convocatoria vencida que el portal no ha retirado.
    old_year = next((y for y in re.findall(r"\b(20\d\d)\b", p.title) if int(y) < today.year), None)
    if old_year:
        sen_pts -= 40
        fit = Fit.BAJO
        gaps.insert(0, f"convocatoria restringida (programa {old_year}, ya pasado)")
    restricted = _RESTRICTED.search(p.title)
    if restricted:
        sen_pts -= 30
        gaps.insert(0, f"convocatoria restringida ({restricted.group(0)})")
    if no_experience:
        sen_pts -= 60
    salary_note, sal_pts = _salary_note(p.salary_text, gate, floor)
    if auth is Authorization.REQUIERE:
        gaps.insert(0, "exige autorización de trabajo que no está documentada")
    pref_pts, pref_note = preference_points(p, sector, tier, family, profile)
    score = (
        REL_POINTS[rel]
        + FIT_POINTS[fit]
        + AUTH_POINTS[auth]
        + TIER_POINTS.get(tier, 0)
        + rec_pts
        + sen_pts
        + sal_pts
        + pref_pts
    )
    ev = Evaluation(
        p,
        company_id,
        sector,
        tier,
        rel,
        family,
        auth,
        fit,
        must[:6],
        nice[:4],
        reasons,
        gaps,
        age,
        recency,
        salary_note,
        seniority,
        max(0, min(150, score)),
    )
    if pref_note:
        ev.reasons.append(pref_note)
    ev.next_step = next_step(ev)
    return ev


#: Sectores a los que aplica `cpg_exception`; el perfil lo reemplaza con
#: `preferences.cpg_exception.sectors`.
_CPG = frozenset({"consumo_masivo", "retail"})


def preference_points(
    p: JobPosting, sector: str, tier: int, family: str, profile: Profile
) -> tuple[int, str]:
    """Puntos por los gustos declarados en el perfil (perfil.yaml → preferences).

    Consumo masivo y retail se penalizan salvo "un cargo muy bueno": empresa de élite y cargo
    de estrategia, finanzas corporativas o pricing con nivel (regla cpg_exception)."""
    sectors = dict(profile.sector_points)
    families = dict(profile.family_points)
    pts = sectors.get(sector, 0)
    cpg = _CPG if profile.cpg_sectors is None else profile.cpg_sectors
    if sector in cpg and pts < 0:
        min_tier, fams, title_re = profile.cpg_exception
        if tier <= min_tier and family in fams and title_re and re.search(title_re, p.title, re.I):
            pts = -5
    pts += families.get(family, 0)
    if not pts:
        return 0, ""
    return pts, f"preferencia del perfil ({sector or 'sin sector'}/{family}): {pts:+d}"


def next_step(ev: Evaluation) -> str:
    if ev.gaps and ev.gaps[0].startswith("no elegible"):
        return "No postular: no cumples un requisito de elegibilidad"
    if ev.gaps and ev.gaps[0].startswith("convocatoria restringida"):
        return "No postular: convocatoria restringida o vencida"
    if ev.fit is Fit.BAJO:
        return "Apuesta alta: postular solo si te interesa mucho el empleador"
    if ev.authorization is Authorization.REQUIERE:
        return "No postular salvo que el empleador confirme patrocinio"
    if ev.authorization is Authorization.SIN_INFO:
        return "Confirmar si contrata desde Colombia o patrocina antes de preparar"
    if ev.authorization is Authorization.MULTILATERAL:
        return "Revisar elegibilidad por nacionalidad en el aviso y preparar"
    if ev.fit is Fit.SIN_DATOS:
        return "Abrir el aviso, leer requisitos y decidir"
    if ev.fit is Fit.COMPLETO:
        return "Preparar postulación y enviar en el portal"
    if ev.fit is Fit.PARCIAL:
        return "Preparar postulación cerrando las brechas en la carta"
    return "Apuesta alta: postular solo si te interesa mucho el empleador"


def needs_detail(p: JobPosting) -> bool:
    """Vale la pena bajar el detalle: afinidad por título o cargo de entrada de carrera."""
    if _FUNC_OFF.search(p.title):
        return False
    return relevance_of(p)[0] in (Relevance.ALTA, Relevance.MEDIA) or bool(_ENTRY_TRACK.search(p.title))
