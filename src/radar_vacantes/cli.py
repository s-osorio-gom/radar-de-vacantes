"""Línea de comandos: `radar buscar`, `radar demo` y `radar descubrir`.

Todo es de solo lectura: el radar lee portales públicos de empleo, evalúa cada vacante contra
un perfil con reglas determinísticas y escribe reportes. No inicia sesión en ningún portal, no
llena formularios y no envía nada.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from radar_vacantes.boards import JobPosting, parse_indeed_transcript
from radar_vacantes.compensation import CompGate
from radar_vacantes.discover import probe_careers_page
from radar_vacantes.engine import SearchStore, coverage, load_universe, run_search
from radar_vacantes.evaluate import Evaluation, Profile, ProfileError, evaluate
from radar_vacantes.report import (
    already_tracked,
    latest_json,
    search_markdown,
    select_top,
    set_goal_notes,
    tablero_docs,
    top_markdown,
    tracked_from_seguimiento,
)
from radar_vacantes.sources import Http, HttpError, UrllibHttp
from radar_vacantes.verify import ABIERTA, NO_VERIFICABLE, verify

#: TRM oficial de Colombia (datos abiertos de la Superintendencia Financiera).
TRM_URL = "https://www.datos.gov.co/resource/32sa-8pi3.json?$order=vigenciadesde%20DESC&$limit=1"
EJEMPLO = Path(__file__).resolve().parents[2] / "ejemplo"


def _yaml(path: Path) -> dict[str, Any]:
    try:
        data: Any = yaml.safe_load(path.read_text("utf-8"))
    except FileNotFoundError:
        hint = ""
        if path.name == "perfil.yaml":
            hint = " Cree el suyo: cp config/perfil.ejemplo.yaml config/perfil.yaml"
        raise SystemExit(f"No existe {path}.{hint}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit(f"No se pudo leer {path}: {exc}") from None
    except yaml.YAMLError as exc:
        raise SystemExit(f"{path} no es YAML válido: {exc}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"{path} no es un diccionario YAML")
    return data


def _profile(path: Path) -> tuple[Profile, dict[str, Any]]:
    cfg = _yaml(path)
    meta: Any = cfg.get("meta") or {}
    notas: Any = meta.get("notas") if isinstance(meta, dict) else None
    if meta and (not isinstance(meta, dict) or not isinstance(notas or {}, dict)):
        raise SystemExit(f"{path}: meta debe ser un diccionario con 'notas' y 'por_defecto'")
    try:
        profile = Profile.from_config(cfg)
    except ProfileError as exc:
        raise SystemExit(f"Perfil inválido en {path}: {exc}") from None
    notes = {str(k): str(v) for k, v in dict(notas or {}).items()}
    if notes:
        set_goal_notes(notes, str(meta.get("por_defecto", "")))
    return profile, cfg


def _json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"No existe {path}") from None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise SystemExit(f"{path} no es JSON válido: {exc}") from None


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"se esperaba un entero, no {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("debe ser 1 o más")
    return value


def _gate(profile: Profile, client: Http) -> CompGate | None:
    """Piso salarial en COP con la TRM oficial vigente. Sin TRM verificable no hay filtro."""
    floor = profile.floor_monthly_cop
    if not floor:
        return None
    try:
        rows: Any = client.get_json(TRM_URL)
        row: dict[str, Any] = rows[0] if isinstance(rows, list) and rows else {}
        usd = Decimal(str(row["valor"]))
        as_of = str(row.get("vigenciadesde", ""))[:10]
    except (HttpError, OSError, KeyError, ValueError, InvalidOperation, TypeError):
        print("TRM no disponible: el filtro salarial queda desactivado.", file=sys.stderr)
        return None
    return CompGate(Decimal(str(floor)), Decimal(str(floor)), usd, as_of)


def buscar(args: argparse.Namespace) -> int:
    profile, _ = _profile(args.perfil)
    universe = _yaml(args.universo)
    try:
        companies = load_universe(universe, universe)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SystemExit(f"{args.universo} tiene una entrada mal formada: {exc!r}") from None
    tz = ZoneInfo(args.zona)
    now = datetime.now(tz)
    stamp = now.strftime("%Y-%m-%dT%H%M%S")
    out: Path = args.salida
    out.mkdir(parents=True, exist_ok=True)
    extra: list[JobPosting] = []
    if args.indeed:
        extra = parse_indeed_transcript(
            _json_file(Path(args.indeed)), now.astimezone(UTC).isoformat()
        )
    client = UrllibHttp(timeout=20)
    store = SearchStore(out / "historial")
    try:
        previous = store.previous_snapshot()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Universo: {len(companies)} empresas.")
    run = run_search(
        companies, client, profile=profile, today=now.date(),
        seen_at=now.astimezone(UTC).isoformat(), stamp=stamp,
        previous=previous, gate=_gate(profile, client), extra=extra,
        workers=args.workers, log=print,
    )
    store.save(run)
    verified = now.strftime("%Y-%m-%d %H:%M")
    (out / f"Busqueda_{stamp}.md").write_text(search_markdown(run, verified=verified), "utf-8")
    (out / "busqueda_latest.json").write_text(
        json.dumps(latest_json(run, verified), ensure_ascii=False, indent=1), "utf-8"
    )
    seg = Path(args.seguimiento) if args.seguimiento else None
    tracked = tracked_from_seguimiento(seg.read_text("utf-8")) if seg and seg.exists() else []
    chosen: list[tuple[Evaluation, Any]] = []
    dropped: list[tuple[Evaluation, Any]] = []
    for ev in select_top(run.evaluations):
        if len(chosen) >= args.top:
            break
        if already_tracked(ev, tracked):
            continue
        chk = verify(ev.posting, client, now.date())
        (chosen if chk.status in (ABIERTA, NO_VERIFICABLE) else dropped).append((ev, chk))
    top_path = out / f"Top{args.top}_{stamp}.md"
    top_path.write_text(top_markdown(chosen, verified=verified, discarded=dropped), "utf-8")
    (out / "tablero_vacantes.json").write_text(
        json.dumps(tablero_docs(chosen, verified=verified, lote=stamp), ensure_ascii=False,
                   indent=1), "utf-8")
    cov = coverage(run)
    print(f"Top {args.top}: {len(chosen)} vacantes ({len(dropped)} cerradas descartadas) → {top_path}")
    print(f"Cobertura: {cov['read']} empresas leídas de {cov['companies']} · "
          f"nuevas {len(run.history['new'])} · cerradas {len(run.history['closed'])}")
    print("Cero formularios enviados. Cero sesiones iniciadas.")
    return 0


def demo(args: argparse.Namespace) -> int:
    """Evalúa vacantes sintéticas sin red: muestra cómo el motor lee requisitos."""
    profile, _ = _profile(args.perfil)
    rows: Any = _json_file(EJEMPLO / "vacantes.json")
    today = date(2026, 10, 10)
    evals: list[Evaluation] = []
    for r in rows:
        p = JobPosting(
            r["fuente"], r["id"], r["empresa"], r["cargo"], r["ciudad"], r["url"],
            r["publicada"], "", description=r["descripcion"],
        )
        evals.append(evaluate(p, company_id=r["id"], sector=r["sector"], tier=r["tier"],
                              profile=profile, today=today))
    evals.sort(key=lambda e: -e.score)
    for e in evals:
        print(f"{e.score:>3}  {e.fit.value:<30} {e.posting.company} — {e.posting.title}")
        for g in e.gaps[:2]:
            print(f"       brecha: {g}")
        print(f"       siguiente paso: {e.next_step}")
    return 0


def descubrir(args: argparse.Namespace) -> int:
    client = UrllibHttp(timeout=20)
    access, dets, others = probe_careers_page("x", args.empresa, args.url, client)
    print(f"Acceso: {access.outcome.value} {access.detail}")
    for d in dets:
        print(f"Sistema con API pública: {d.method} → ref {d.ref}")
    # Estos sí tienen lector, pero su referencia se arma a mano (ver config/universo.yaml).
    manual_ref = {"successfactors", "cornerstone", "phenom", "magneto"}
    with_reader = [o for o in others if o in manual_ref]
    without = [o for o in others if o not in manual_ref]
    if with_reader:
        print("Sistemas con lector (configure la ref a mano): " + ", ".join(with_reader))
    if without:
        print("Sistemas sin API pública (revisión manual): " + ", ".join(without))
    if not dets and not others:
        print("No se detectó un sistema de vacantes conocido.")
    return 0


def main(argv: list[str] | None = None) -> int:
    root = Path.cwd()
    ap = argparse.ArgumentParser(prog="radar", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("buscar", help="Corre el radar completo sobre el universo de empresas.")
    b.add_argument("--perfil", type=Path, default=root / "config" / "perfil.yaml")
    b.add_argument("--universo", type=Path, default=root / "config" / "universo.yaml")
    b.add_argument("--salida", type=Path, default=root / "salida")
    b.add_argument("--indeed", help="Transcripción JSON de resultados de Indeed (opcional).")
    b.add_argument("--seguimiento", help="Tabla Markdown de postulaciones: no ocupan cupo.")
    b.add_argument("--top", type=_positive, default=20)
    b.add_argument("--workers", type=_positive, default=16)
    b.add_argument("--zona", default="America/Bogota")
    b.set_defaults(func=buscar)
    d = sub.add_parser("demo", help="Evalúa vacantes sintéticas sin conexión.")
    d.add_argument("--perfil", type=Path, default=EJEMPLO.parent / "config" / "perfil.ejemplo.yaml")
    d.set_defaults(func=demo)
    s = sub.add_parser("descubrir", help="Detecta el sistema de vacantes de una página de empleo.")
    s.add_argument("empresa")
    s.add_argument("url")
    s.set_defaults(func=descubrir)
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
