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


def _to_decimal(number: str, suffix: str) -> Decimal | None:
    cleaned = number.replace(",", "")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        value = Decimal(cleaned)
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
    named = {code for code, pattern in _CODES if pattern.search(text)}
    if len(named) > 1:
        return None
    seen_symbol = False
    millions = False
    amounts: list[Decimal] = []
    for match in _AMOUNT.finditer(text):
        symbol, number, suffix = match.group(1), match.group(2), match.group(3) or ""
        seen_symbol = seen_symbol or bool(symbol)
        value = _to_decimal(number, suffix)
        # Tras ver la moneda, "8.000.000 - 10.000.000" comparte símbolo.
        if value is not None and value > 0 and (
            symbol or suffix or ((named or seen_symbol) and value >= 1000)
        ):
            amounts.append(value)
            millions = millions or (not suffix and value >= _COP_MAGNITUDE)
    if not amounts or not (named or seen_symbol):
        return None
    currency = next(iter(named)) if named else ("COP" if millions else "USD")
    if re.search(r"(/\s*h(ou)?r|per hour|hourly|por hora)", lowered):
        interval = Interval.HOURLY
    elif re.search(r"(/\s*mo(nth)?|per month|monthly|mensual|al mes|por mes|/\s*mes)", lowered):
        interval = Interval.MONTHLY
    else:
        interval = Interval.ANNUAL
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
