"""Fallos de red y respuestas raras, simulados sin red: ninguno puede tumbar la corrida ni
pasar por "sin vacantes" (una lectura vacía cierra en el historial lo que había antes)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from radar_vacantes.engine import Company, run_search
from radar_vacantes.evaluate import Profile
from radar_vacantes.sources import HttpError, Outcome, SourceSpec, UrllibHttp, read_source

ROOT = Path(__file__).resolve().parents[1]
PROFILE = Profile.from_config(yaml.safe_load((ROOT / "config/perfil.ejemplo.yaml").read_text()))

#: Conector JSON → (ref de ejemplo, respuesta vacía legítima de esa API).
JSON_SOURCES: dict[str, tuple[str, Any]] = {
    "greenhouse": ("acme", {"jobs": [], "meta": {"total": 0}}),
    "ashby": ("acme", {"jobs": []}),
    "lever": ("acme", []),
    "smartrecruiters": ("acme", {"totalFound": 0, "content": []}),
    "workable": ("acme", {"name": "Acme", "jobs": []}),
    "recruitee": ("acme", {"offers": []}),
    "workday": ("https://acme.wd1.myworkdayjobs.com/Ext", {"total": 0, "jobPostings": []}),
    "oracle": (
        "https://abc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
        {"items": [{"requisitionList": [], "TotalJobsCount": 0}]},
    ),
    "amazon": ("COL", {"hits": 0, "jobs": []}),
    "hirehive": ("https://acme.hirehive.com", {"jobs": []}),
    "phenom": (
        "https://careers.example.com/global/en",
        {"refineSearch": {"totalHits": 0, "data": {"jobs": []}}},
    ),
}


class Fixed:
    """Toda petición devuelve `value` (o lo lanza si es una excepción)."""

    def __init__(self, value: Any) -> None:
        self.value = value
        self.calls = 0

    def _get(self) -> Any:
        self.calls += 1
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value

    def get_json(self, url: str) -> Any:
        return self._get()

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        return self._get()

    def get_text(self, url: str) -> str:
        value = self._get()
        return value if isinstance(value, str) else json.dumps(value)


def _read(method: str, value: Any) -> Any:
    ref, _ = JSON_SOURCES[method]
    spec = SourceSpec("acme", "Acme", method, ref)
    return read_source(spec, Fixed(value), "2026-10-10T00:00:00+00:00", sleep=lambda _: None)


@pytest.mark.parametrize("method", sorted(JSON_SOURCES))
def test_real_empty_response_is_empty(method: str) -> None:
    assert _read(method, JSON_SOURCES[method][1]).outcome is Outcome.EMPTY


@pytest.mark.parametrize("method", sorted(JSON_SOURCES))
@pytest.mark.parametrize(
    "payload",
    [{"error": "maintenance"}, None, 5, "<html><body>nuevo diseño</body></html>"],
    ids=["dict-de-error", "null", "numero", "html"],
)
def test_unexpected_shape_is_an_error_not_empty(method: str, payload: Any) -> None:
    res = _read(method, payload)
    assert res.outcome is Outcome.ERROR
    assert "formato inesperado" in res.detail


def test_unexpected_shape_is_not_retried() -> None:
    http = Fixed({"error": "maintenance"})
    read_source(SourceSpec("a", "Acme", "greenhouse", "acme"), http, "x", sleep=lambda _: None)
    assert http.calls == 1


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [
        (TimeoutError("timed out"), Outcome.ERROR),
        (HttpError(429), Outcome.BLOCKED),
        (HttpError(500), Outcome.ERROR),
        (HttpError(503), Outcome.ERROR),
        (json.JSONDecodeError("Expecting value", "<html>", 0), Outcome.ERROR),
    ],
    ids=["timeout", "429", "500", "503", "json-malformado"],
)
def test_network_failures_are_explicit(failure: BaseException, outcome: Outcome) -> None:
    for method in JSON_SOURCES:
        assert _read(method, failure).outcome is outcome


def _company() -> list[Company]:
    spec = SourceSpec("acme", "Acme", "greenhouse", "acme")
    return [Company("acme", "Acme", "pagos", "CO", 1, (), "", (spec,), ())]


def test_a_200_with_an_error_body_does_not_close_previous_postings() -> None:
    ok = {"jobs": [{"id": 1, "title": "Financial Analyst", "absolute_url": "https://x.co/1",
                    "location": {"name": "Bogotá"}, "updated_at": "2026-10-01T00:00:00Z"}]}
    first = run_search(_company(), Fixed(ok), profile=PROFILE, today=date(2026, 10, 10),
                       seen_at="2026-10-10T00:00:00+00:00", stamp="a", previous={})
    assert len(first.snapshot) == 1
    second = run_search(_company(), Fixed({"error": "maintenance"}), profile=PROFILE,
                        today=date(2026, 10, 11), seen_at="2026-10-11T00:00:00+00:00",
                        stamp="b", previous=first.snapshot)
    assert second.results[0].outcome is Outcome.ERROR
    assert second.history["closed"] == []


class _Json:
    """Cliente que decodifica el texto recibido como lo hace el cliente real."""

    def __init__(self, ok: str, broken: str) -> None:
        self.ok, self.broken = ok, broken

    def get_json(self, url: str) -> Any:
        return json.loads(self.broken if "/broken/" in url else self.ok)

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        raise HttpError(404)

    def get_text(self, url: str) -> str:
        raise HttpError(404)


@pytest.mark.parametrize("failure", [IndexError("boom"), RecursionError(), ZeroDivisionError()])
def test_unexpected_exception_is_an_error_not_a_crash(failure: BaseException) -> None:
    http = Fixed(failure)
    res = read_source(SourceSpec("a", "Acme", "greenhouse", "acme"), http, "x",
                      sleep=lambda _: None)
    assert res.outcome is Outcome.ERROR and type(failure).__name__ in res.detail
    assert http.calls == 1


def test_one_hostile_source_does_not_stop_the_run() -> None:
    good = SourceSpec("acme", "Acme", "greenhouse", "acme")
    bad = SourceSpec("rota", "Rota", "greenhouse", "broken")
    companies = [
        Company("acme", "Acme", "pagos", "CO", 1, (), "", (good,), ()),
        Company("rota", "Rota", "pagos", "CO", 1, (), "", (bad,), ()),
    ]
    ok = json.dumps({"jobs": [{"id": 1, "title": "Financial Analyst",
                               "absolute_url": "https://x.co/1", "location": {"name": "Bogotá"}}]})
    run = run_search(companies, _Json(ok, "[" * 200_000), profile=PROFILE,
                     today=date(2026, 10, 10), seen_at="2026-10-10T00:00:00+00:00", stamp="a",
                     previous={})
    outcomes = {r.spec.company_id: r.outcome for r in run.results}
    assert outcomes == {"acme": Outcome.OK, "rota": Outcome.ERROR}
    assert len(run.evaluations) == 1


#: Dónde va la lista de vacantes en la respuesta vacía de cada conector.
def _with_list(method: str, items: Any) -> Any:
    payload = json.loads(json.dumps(JSON_SOURCES[method][1]))
    if method == "lever":
        return items
    if method == "oracle":
        payload["items"][0]["requisitionList"] = items
    elif method == "phenom":
        payload["refineSearch"]["data"]["jobs"] = items
    else:
        key = {"smartrecruiters": "content", "recruitee": "offers", "workday": "jobPostings"}
        payload[key.get(method, "jobs")] = items
    return payload


@pytest.mark.parametrize("method", sorted(JSON_SOURCES))
@pytest.mark.parametrize(
    "items",
    [None, [1, "x", None], [{"identifier": 9, "name": "Financial Analyst", "link": "https://x.co/9"}]],
    ids=["contenedor-null", "elementos-no-dict", "campos-renombrados"],
)
def test_items_that_yield_no_posting_are_an_error(method: str, items: Any) -> None:
    res = _read(method, _with_list(method, items))
    assert res.outcome is Outcome.ERROR and "formato inesperado" in res.detail


def test_one_bad_item_does_not_hide_the_good_ones() -> None:
    good = {"id": 1, "title": "Financial Analyst", "absolute_url": "https://x.co/1",
            "location": {"name": "Bogotá"}}
    res = _read("greenhouse", {"jobs": [good, 7, {"foo": "bar"}]})
    assert res.outcome is Outcome.OK and len(res.postings) == 1


def test_oracle_without_requisition_list_is_an_error() -> None:
    for payload in ({"items": []}, {"items": [{"foo": 1}]}):
        assert _read("oracle", payload).outcome is Outcome.ERROR


class _Cornerstone(UrllibHttp):
    """Cornerstone exige el cliente real (token anónimo); aquí sin red."""

    def __init__(self, search: Any) -> None:
        super().__init__(respect_robots=False, per_host_interval=0)
        self.search = search

    def get_text(self, url: str) -> str:
        return '{"token":"anon"} https://eu.api.csod.com/x'

    def post_json(
        self, url: str, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> Any:
        return self.search


@pytest.mark.parametrize(
    ("search", "outcome"),
    [({"data": {"totalCount": 0, "requisitions": []}}, Outcome.EMPTY),
     ({"data": {"requisitions": None}}, Outcome.ERROR),
     ({"data": {"requisitions": [1, None]}}, Outcome.ERROR),
     ({"error": "x"}, Outcome.ERROR)],
)
def test_cornerstone_empty_and_unexpected_shapes(search: Any, outcome: Outcome) -> None:
    spec = SourceSpec("a", "Acme", "cornerstone",
                      "https://acme.csod.com/ux/ats/careersite/1/home?c=acme")
    res = read_source(spec, _Cornerstone(search), "x", sleep=lambda _: None)
    assert res.outcome is outcome


def test_retry_count_reads_well() -> None:
    spec = SourceSpec("a", "Acme", "greenhouse", "acme")
    two = read_source(spec, Fixed(TimeoutError()), "x", sleep=lambda _: None)
    assert two.detail.endswith("(tras 2 reintentos)")
    none = read_source(spec, Fixed(TimeoutError()), "x", retries=0, sleep=lambda _: None)
    assert "reintento" not in none.detail
