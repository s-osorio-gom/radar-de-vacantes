"""Lectura determinística de salario publicado y comparación con el piso.

Reglas: no se inventa compensación, no se rechaza
por salario desconocido y la moneda y el intervalo son siempre explícitos.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum


class Interval(StrEnum):
    """Periodicidad del salario publicado."""

    ANNUAL = "annual"
    MONTHLY = "monthly"
    HOURLY = "hourly"


@dataclass(frozen=True, slots=True)
class SalaryRange:
    """Rango publicado por el empleador, tal cual, con moneda e intervalo."""

    currency: str
    low: Decimal
    high: Decimal
    interval: Interval


@dataclass(frozen=True, slots=True)
class CompGate:
    """Piso y objetivo mensual en COP, con la TRM usada y su fecha."""

    floor_monthly_cop: Decimal
    target_monthly_cop: Decimal
    usd_cop: Decimal
    usd_cop_as_of: str


# El sufijo k/m no puede ir seguido de letra: "10.000.000 mensual" no es un millón.
_AMOUNT = re.compile(
    r"(us\$|\$|usd|cop)?\s*([0-9][0-9.,]*)\s*(?:([kKmM])(?![a-zA-Z]))?", re.IGNORECASE
)
HOURS_PER_YEAR = Decimal(2080)


#: Miles agrupados con "." o "," (4.500.000 · 80,000), con decimales opcionales en el otro
#: separador (4.500.000,00 · 70,000.00).
_THOUSANDS = re.compile(r"(\d{1,3}(?:([.,])\d{3})(?:\2\d{3})*)(?:([.,])(\d{1,2}))?")
_PLAIN = re.compile(r"(\d+)(?:[.,](\d+))?")


def _to_decimal(number: str, suffix: str) -> Decimal | None:
    """Cifra al estilo latino o anglosajón: "4.500" y "4,500" son cuatro mil quinientos;
    "4.500.000,00" son cuatro millones y medio; "4.5" con sufijo M, cuatro millones y medio."""
    text = number.rstrip(".,")
    grouped = _THOUSANDS.fullmatch(text)
    plain = _PLAIN.fullmatch(text)
    if grouped and grouped.group(3) != grouped.group(2):
        integer, frac = re.sub(r"[.,]", "", grouped.group(1)), grouped.group(4) or ""
    elif plain:
        integer, frac = plain.group(1), plain.group(2) or ""
    else:
        return None
    try:
        value = Decimal(f"{integer}.{frac}" if frac else integer)
    except InvalidOperation:
        return None
    if suffix.lower() == "k":
        value *= 1000
    elif suffix.lower() == "m":
        value *= 1_000_000
    return value


#: Monedas nombradas en el texto. Mandan sobre el símbolo "$", que usan ambas.
_CODES = (
    ("COP", re.compile(r"\b(cop|pesos?)\b|col\$", re.IGNORECASE)),
    ("USD", re.compile(r"\busd\b|us\$|d[oó]lar", re.IGNORECASE)),
)
_MILLONES = re.compile(r"(\d)\s*mill(?:ones|[oó]n)\b", re.IGNORECASE)
_COP_MONTHLY = Decimal(100_000)
_YEAR = re.compile(r"(19|20)\d\d")
_GROUPED = re.compile(r"\d[.,]\d{3}")
#: Con "$" a secas, un monto de millones sin sufijo es un salario en pesos colombianos.
_COP_MAGNITUDE = Decimal(1_000_000)


def parse_salary(text: str) -> SalaryRange | None:
    """Extrae un rango USD o COP. Devuelve None ante cualquier ambigüedad.

    La moneda sale del código escrito (COP, pesos, USD, US$, dólares); si aparecen dos, es
    ambiguo. Con "$" a secas: "$4.500.000" es COP y "$80,000" es USD."""
    if not text:
        return None
    lowered = text.lower()
    if any(sym in text for sym in ("€", "£", "₹", "¥")) or re.search(
        r"\b(eur|gbp|cad|aud|inr|brl|mxn|chf|clp|ars|pen)\b|"
        r"pesos? (mexican|chilen|argentin|uruguay|dominican|filipin)",
        lowered,
    ):
        return None
    named_at = {code: m.start() for code, pattern in _CODES if (m := pattern.search(text))}
    named = set(named_at)
    if len(named) > 1:
        return None
    first_named = min(named_at.values(), default=len(text))
    # "$5 millones" = $5M; "401k" es un plan de retiro, no un monto.
    text = _MILLONES.sub(r"\1M", re.sub(r"\b401\s*\(?k\)?", " ", text, flags=re.IGNORECASE))
    seen_symbol = False
    millions = False
    amounts: list[Decimal] = []
    for match in _AMOUNT.finditer(text):
        symbol, number, suffix = match.group(1), match.group(2), match.group(3) or ""
        seen_symbol = seen_symbol or bool(symbol)
        value = _to_decimal(number, suffix)
        # Tras ver la moneda, "8.000.000 - 10.000.000" comparte símbolo. Una cifra suelta antes
        # de la moneda solo cuenta con separador de miles ("4.500.000 - 6.000.000 COP"), y un
        # año ("Salario 2026: COP 8.000.000") nunca es un monto.
        bare = not symbol and not suffix
        after = seen_symbol or match.start() >= first_named
        if bare and (_YEAR.fullmatch(number) or not (after or _GROUPED.search(number))):
            continue
        if value is not None and value > 0 and (
            symbol or suffix or ((named or seen_symbol) and value >= 1000)
        ):
            amounts.append(value)
            millions = millions or value >= _COP_MAGNITUDE
    if not amounts or not (named or seen_symbol):
        return None
    if re.search(r"(/\s*h(ou)?r|per hour|hourly|por hora)", lowered):
        interval = Interval.HOURLY
    elif re.search(r"(/\s*mo(nth)?|per month|monthly|mensual|al mes|por mes|/\s*mes)", lowered):
        interval = Interval.MONTHLY
    else:
        interval = Interval.ANNUAL
    # "$" a secas: millones, o cientos de miles al mes, solo pueden ser pesos.
    monthly_hundreds = interval is Interval.MONTHLY and max(amounts) >= _COP_MONTHLY
    currency = (
        next(iter(named)) if named else ("COP" if millions or monthly_hundreds else "USD")
    )
    return SalaryRange(currency, min(amounts), max(amounts), interval)


def monthly_cop(value: Decimal, salary: SalaryRange, usd_cop: Decimal) -> Decimal:
    """Convierte un monto del rango a COP mensuales."""
    if salary.interval is Interval.ANNUAL:
        monthly = value / 12
    elif salary.interval is Interval.HOURLY:
        monthly = value * HOURS_PER_YEAR / 12
    else:
        monthly = value
    if salary.currency == "USD":
        monthly *= usd_cop
    return monthly.quantize(Decimal(1))


def compare_to_gate(salary: SalaryRange | None, gate: CompGate | None) -> tuple[int, str]:
    """Ajuste de puntaje y razón. (0, '') si no hay dato: no se castiga lo desconocido."""
    if salary is None or gate is None:
        return 0, ""
    high = monthly_cop(salary.high, salary, gate.usd_cop)
    low = monthly_cop(salary.low, salary, gate.usd_cop)
    if high < gate.floor_monthly_cop:
        return -100, f"salario publicado máx. COP {high:,}/mes < piso (TRM {gate.usd_cop_as_of})"
    if low >= gate.target_monthly_cop:
        return 6, f"salario publicado mín. COP {low:,}/mes ≥ objetivo (+6)"
    return 2, f"salario publicado hasta COP {high:,}/mes, sobre el piso (+2)"
