"""The Postman collection is a shared artifact — keep it from rotting.

It is how the non-developers on the team run the engine and how the frontend devs read the
contract, so a renamed route that only breaks Postman still breaks two people's day. These
tests check the collection against the service's real routes, and against the rules that make
it usable by someone who does not write code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leash.service.app import app

ROOT = Path(__file__).resolve().parent.parent
COLLECTION = json.loads((ROOT / "postman" / "leash.postman_collection.json").read_text())
ENVIRONMENT = json.loads((ROOT / "postman" / "leash.postman_environment.json").read_text())

VARIABLES = {v["key"] for v in COLLECTION["variable"]}
# Collection variable -> the FastAPI path parameter it fills, where the names differ.
PARAMETER_NAMES = {"stepup_run_id": "run_id", "step_up_id": "authorization_id"}
ENGINE_ROUTES = {
    (method, route.path)
    for route in app.routes
    for method in getattr(route, "methods", set())
    if hasattr(route, "path")
}


def requests() -> list[tuple[str, dict[str, Any]]]:
    return [(f["name"], r) for f in COLLECTION["item"] for r in f["item"]]


def _path(request: dict[str, Any]) -> str:
    return "/" + "/".join(request["request"]["url"]["path"])


def _host(request: dict[str, Any]) -> str:
    return request["request"]["url"]["host"][0]


CASES = requests()
IDS = [f"{folder} :: {r['name']}" for folder, r in CASES]


@pytest.mark.parametrize(("folder", "request_"), CASES, ids=IDS)
def test_every_engine_request_hits_a_route_that_exists(
    folder: str, request_: dict[str, Any]
) -> None:
    """A typo here is only discovered by a teammate, mid-demo, if a test does not catch it."""
    if _host(request_) != "{{engine_url}}":
        return  # folder 6 targets the organizers' API, which we do not define
    method = request_["request"]["method"]
    path = _path(request_)
    for variable in VARIABLES:
        # {{stepup_run_id}} carries a run id; {{step_up_id}} carries an authorization id.
        # Everything else is named after the path parameter it fills.
        declared = PARAMETER_NAMES.get(variable, variable)
        path = path.replace("{{" + variable + "}}", "{" + declared + "}")

    assert (method, path) in ENGINE_ROUTES, (
        f"{method} {path} is not a route on the service — "
        f"the collection and specs/service-contract.md have drifted"
    )


@pytest.mark.parametrize(("folder", "request_"), CASES, ids=IDS)
def test_every_variable_used_is_declared(folder: str, request_: dict[str, Any]) -> None:
    """An undeclared variable renders as an empty path segment and 404s."""
    blob = json.dumps(request_)
    used = {m.split("}}")[0] for m in blob.split("{{")[1:] if "}}" in m}
    undeclared = used - VARIABLES
    assert not undeclared, f"{sorted(undeclared)} is used but not declared in the collection"


@pytest.mark.parametrize(("folder", "request_"), CASES, ids=IDS)
def test_every_request_explains_itself(folder: str, request_: dict[str, Any]) -> None:
    """The collection is for people who do not read the source. A bare URL helps nobody."""
    description = request_["request"].get("description", "")
    assert len(description) > 40, f"{request_['name']} needs a description a non-developer can use"


def test_the_key_is_not_baked_into_the_shared_file() -> None:
    """This file gets shared. A key in it is a key leaked (AGENTS.md §1)."""
    blob = json.dumps(COLLECTION) + json.dumps(ENVIRONMENT)
    assert "team_api_key" in blob, "the variable should exist"
    for entry in COLLECTION["variable"] + ENVIRONMENT["values"]:
        if entry["key"] == "team_api_key":
            assert entry["value"] == "", "the team key must ship empty and be set locally"


def test_the_environment_declares_what_the_collection_needs() -> None:
    env_keys = {v["key"] for v in ENVIRONMENT["values"]}
    assert {"engine_url", "sandbox_url", "team_api_key"} <= env_keys


def test_the_five_scenarios_are_named_where_someone_will_look() -> None:
    """A non-developer has to be able to find the scenario list without reading the repo."""
    blob = json.dumps(COLLECTION)
    for scenario_id in ("SCEN0000", "SCEN0001", "SCEN0002", "SCEN0003", "SCEN0004"):
        assert scenario_id in blob
