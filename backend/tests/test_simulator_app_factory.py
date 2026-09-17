"""Standalone-app / base-URL-swappability property (issue #11 acceptance #6): the simulator
is importable and servable as its own ASGI app, independent of the main songforge web app —
the same client code targets it or the real MusicGPT API purely by swapping a base URL.
"""

from __future__ import annotations

import ast
import inspect

import httpx
from fastapi import FastAPI

from songforge.simulator.app import create_app
from tests.simulator_helpers import CREATE_PATH, DEFAULT_BODY, make_sim_client


async def test_create_app_returns_a_standalone_fastapi_app_with_its_own_routes() -> None:
    app = create_app()
    assert isinstance(app, FastAPI)

    # Externally observable route wiring, not FastAPI's internal route objects: a 404 would
    # mean the route isn't registered at all, distinct from "registered but not implemented".
    # NB: /byId is probed with a *real* task id — an unknown id legitimately 404s (that's its
    # own contract, see test_simulator_by_id.py), so it can't double as the route-exists probe.
    async with make_sim_client(app) as client:
        create_resp = await client.post(CREATE_PATH, json=DEFAULT_BODY)
        task_id = create_resp.json()["task_id"]
        by_id_resp = await client.get("/byId", params={"task_id": task_id})
    assert create_resp.status_code != 404
    assert by_id_resp.status_code != 404


def test_simulator_module_has_no_import_dependency_on_the_main_web_app() -> None:
    """Check the *import statements*, not prose in the module's docstring/comments."""
    import songforge.simulator.app as sim_app_module

    tree = ast.parse(inspect.getsource(sim_app_module))
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(mod == "songforge.web" or mod.startswith("songforge.web.") for mod in imported_modules)


async def test_same_app_answers_identically_under_different_base_urls() -> None:
    """The create endpoint's behavior doesn't depend on which base URL a client is
    configured with — proving base-URL is purely a client-side selector (the real API vs.
    this simulator is chosen the same way).
    """
    app = create_app()

    async with make_sim_client(app, base_url="http://sim.test") as client_a:
        resp_a = await client_a.post(CREATE_PATH, json=DEFAULT_BODY)

    async with make_sim_client(app, base_url="http://simulator:8080") as client_b:
        resp_b = await client_b.post(CREATE_PATH, json=DEFAULT_BODY)

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    assert set(resp_a.json()) == set(resp_b.json())


def test_sim_client_helper_uses_asgi_transport_no_real_network() -> None:
    """Guard the test-safety policy itself: the shared helper must never open a real socket."""
    client = make_sim_client(create_app())
    assert isinstance(client._transport, httpx.ASGITransport)
