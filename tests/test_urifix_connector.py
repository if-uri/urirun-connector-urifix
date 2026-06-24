from __future__ import annotations

import json

import urirun
from urirun import v2
from urirun_connector_urifix import (
    connector_manifest,
    diagnose_chain,
    main,
    repair_chain,
    urirun_bindings,
)

ROUTE_DIAGNOSE = "urifix://host/chain/query/diagnose"
ROUTE_REPAIR = "urifix://host/chain/command/repair"
ALL_ROUTES = {ROUTE_DIAGNOSE, ROUTE_REPAIR}
MODULE = "urirun_connector_urifix.core"


def _failed_document_sync_result() -> dict:
    return {
        "ok": False,
        "prompt": "wyślij wszystkie folery z artifacts z /home/tom/.urirun/documents/* do lenovo laptop do fodleru downloads usera",
        "execute": True,
        "selectedNodes": ["lenovo"],
        "selectedTargets": ["host", "service:phone-scanner", "node:lenovo"],
        "flow": {
            "task": {"id": "document-sync-to-node"},
            "steps": [{
                "id": "sync-documents-to-node",
                "uri": "document://host/archive/command/sync-to-node",
                "payload": {"node": "lenovo", "dest_root": "~/Downloads/urirun-scans"},
                "depends_on": [],
            }],
        },
        "timeline": [{
            "id": "sync-documents-to-node",
            "uri": "document://host/archive/command/sync-to-node",
            "target": "lenovo",
            "ok": False,
            "status": "failed",
        }],
        "results": {},
        "error": {
            "type": "ValueError",
            "message": "node_url is required when the target node is not present in host config",
            "uri": "document://host/archive/command/sync-to-node",
        },
    }


def test_bindings_are_isolated_handlers() -> None:
    bindings = urirun_bindings()["bindings"]
    assert set(bindings) == ALL_ROUTES
    for route, export in ((ROUTE_DIAGNOSE, "diagnose_chain"), (ROUTE_REPAIR, "repair_chain")):
        entry = bindings[route]
        assert entry["adapter"] == "local-function-subprocess"
        assert entry["python"]["module"] == MODULE
        assert entry["python"]["export"] == export
        assert "argv" not in entry
    json.dumps(urirun_bindings())


def test_bindings_compile_and_routes_present() -> None:
    registry = v2.compile_registry(urirun_bindings())
    uris = {route["uri"] for route in v2.list_routes(registry)}
    assert ALL_ROUTES <= uris


def test_repair_chain_patches_missing_node_url() -> None:
    request = {"nodes": [], "targets": ["host", "service:phone-scanner"], "execute": True, "no_llm": False}
    result = repair_chain(
        prompt=request["targets"][0] + " lenovo",
        request=request,
        result=_failed_document_sync_result(),
        node_urls=["lenovo=http://192.168.188.201:8766"],
    )

    assert result["ok"] is True
    assert result["repaired"] is True
    assert result["diagnosis"]["kind"] == "missing-node-url"
    assert result["patch"]["request"]["nodes"] == ["lenovo"]
    assert "node:lenovo" in result["patch"]["request"]["targets"]
    assert result["patch"]["stepPayload"]["node_url"] == "http://192.168.188.201:8766"
    assert result["retry"] == {
        "uri": "document://host/archive/command/sync-to-node",
        "mode": "execute",
        "payload": {
            "node": "lenovo",
            "dest_root": "~/Downloads/urirun-scans",
            "node_url": "http://192.168.188.201:8766",
        },
    }


def test_repair_chain_requires_node_url_when_unknown() -> None:
    result = repair_chain(
        prompt="wyślij dokumenty do lenovo",
        request={"nodes": [], "targets": ["host"], "execute": True},
        result=_failed_document_sync_result(),
    )

    assert result["ok"] is True
    assert result["repaired"] is False
    assert result["retry"]["payload"]["node"] == "lenovo"
    assert "node_url" not in result["retry"]["payload"]
    assert result["recovery"][0]["id"] == "provide-node-url"


def test_diagnose_missing_llm_model() -> None:
    result = diagnose_chain(result={"ok": False, "error": "URIRUN_LLM_MODEL or LLM_MODEL is not set"})

    assert result["ok"] is True
    assert result["diagnosis"]["kind"] == "missing-llm-model"
    assert result["diagnosis"]["error"]["category"] == "FAILED_PRECONDITION"
    assert result["recovery"][0]["id"] == "use-known-intent"


def test_runtime_executes_from_compiled_registry() -> None:
    registry = urirun.compile_registry(json.loads(json.dumps(urirun_bindings())))
    env = v2.run(
        ROUTE_REPAIR,
        registry,
        payload={
            "prompt": "wyślij dokumenty do lenovo",
            "request": {"nodes": [], "targets": ["host"], "execute": True},
            "result": _failed_document_sync_result(),
            "node_urls": ["lenovo=http://192.168.188.201:8766"],
        },
        mode="execute",
        policy=urirun.policy(allow=["urifix://*"]),
    )

    assert env["ok"] is True
    data = urirun.result_data(env)
    assert data["repaired"] is True
    assert data["retry"]["payload"]["node_url"] == "http://192.168.188.201:8766"


def test_manifest_and_cli(capsys) -> None:
    manifest = connector_manifest()
    assert manifest["id"] == "urifix"
    assert set(manifest["routes"]) == ALL_ROUTES
    assert manifest["uriSchemes"] == ["urifix"]
    json.dumps(manifest)

    assert main(["bindings"]) == 0
    assert ROUTE_REPAIR in json.loads(capsys.readouterr().out)["bindings"]
    assert main(["manifest"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "urifix"
