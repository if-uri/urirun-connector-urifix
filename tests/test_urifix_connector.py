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


def test_repair_chain_requires_node_url_when_unknown(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("URIRUN_NODES_FILE", str(tmp_path / "missing-nodes.json"))
    monkeypatch.delenv("URIRUN_NODES", raising=False)
    monkeypatch.delenv("URIRUN_NODE_ALIASES", raising=False)
    monkeypatch.delenv("URIRUN_NODE_URL_LENOVO", raising=False)
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


def test_laptop_prompt_hint_does_not_create_second_node_when_concrete_node_exists() -> None:
    result = repair_chain(
        prompt="wyślij dokumenty do lenovo laptop",
        request={"nodes": [], "targets": ["host", "service:phone-scanner"], "execute": True},
        result=_failed_document_sync_result(),
    )

    assert result["diagnosis"]["input"]["selectedNodes"] == ["lenovo"]
    assert result["patch"]["request"]["nodes"] == ["lenovo"]


def test_prompt_node_aliases_come_from_known_nodes_not_code() -> None:
    result_without_node = {
        **_failed_document_sync_result(),
        "selectedNodes": [],
        "selectedTargets": ["host"],
        "flow": {
            "steps": [{
                "id": "sync-documents-to-node",
                "uri": "document://host/archive/command/sync-to-node",
                "payload": {"dest_root": "~/Downloads/urirun-scans"},
            }],
        },
    }

    result = repair_chain(
        prompt="wyślij dokumenty do notebooka",
        request={"nodes": [], "targets": ["host"], "execute": True},
        result=result_without_node,
        known_nodes={"workstation": {"url": "http://node.local:8766", "aliases": ["notebooka"]}},
    )

    assert result["diagnosis"]["input"]["selectedNodes"] == ["workstation"]
    assert result["patch"]["request"]["nodes"] == ["workstation"]
    assert result["retry"]["payload"]["node"] == "workstation"
    assert result["retry"]["payload"]["node_url"] == "http://node.local:8766"


def test_diagnose_missing_llm_model() -> None:
    result = diagnose_chain(result={"ok": False, "error": "URIRUN_LLM_MODEL or LLM_MODEL is not set"})

    assert result["ok"] is True
    assert result["diagnosis"]["kind"] == "missing-llm-model"
    assert result["diagnosis"]["error"]["category"] == "FAILED_PRECONDITION"
    assert result["recovery"][0]["id"] == "use-known-intent"


def test_repair_chain_recovers_missing_fs_transfer_routes() -> None:
    result = {
        "ok": False,
        "execute": True,
        "node": "lenovo",
        "nodeUrl": "http://192.168.188.201:8765",
        "uri": "document://host/archive/command/sync-to-node",
        "destRoot": "~/Downloads/urirun-scans",
        "preflight": {
            "missingAfter": [
                "fs://host/file/command/write-b64",
                "fs://host/file/query/read-b64",
            ],
        },
        "error": {
            "type": "ContractError",
            "message": "remote node is missing required fs transfer route(s): fs://host/file/command/write-b64, fs://host/file/query/read-b64",
            "uri": "document://host/archive/command/sync-to-node",
        },
    }

    repaired = repair_chain(
        prompt="wyślij dokumenty do lenovo",
        request={"targets": ["host", "node:lenovo"], "execute": True},
        result=result,
    )

    assert repaired["ok"] is True
    assert repaired["repaired"] is True
    assert repaired["diagnosis"]["kind"] == "missing-fs-transfer-route"
    assert repaired["diagnosis"]["missingRoutes"] == [
        "fs://host/file/command/write-b64",
        "fs://host/file/query/read-b64",
    ]
    assert repaired["retry"] == {
        "uri": "document://host/archive/command/sync-to-node",
        "mode": "execute",
        "payload": {
            "destRoot": "~/Downloads/urirun-scans",
            "node": "lenovo",
            "node_url": "http://192.168.188.201:8765",
            "ensure_routes": True,
        },
    }
    assert {action["id"] for action in repaired["recovery"]} >= {
        "retry-document-sync-with-route-preflight",
        "provision-fs-file-transfer",
    }


def test_repair_chain_maps_legacy_no_sha256_sync_error_to_fs_route_recovery() -> None:
    result = {
        "ok": False,
        "execute": True,
        "node": "lenovo",
        "nodeUrl": "http://192.168.188.201:8765",
        "uri": "document://host/archive/command/sync-to-node",
        "fsUri": "fs://host/file/command/write-b64",
        "fsReadUri": "fs://host/file/query/read-b64",
        "error": {
            "type": "ContractError",
            "message": "remote write returned no sha256",
            "uri": "document://host/archive/command/sync-to-node",
        },
    }

    repaired = repair_chain(
        prompt="wyślij dokumenty do lenovo",
        request={"targets": ["host", "node:lenovo"], "execute": True},
        result=result,
    )

    assert repaired["diagnosis"]["kind"] == "missing-fs-transfer-route"
    assert repaired["retry"]["payload"]["ensure_routes"] is True
    assert repaired["retry"]["payload"]["node_url"] == "http://192.168.188.201:8765"


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


def test_missing_connector_diagnosis_from_connector_required_error() -> None:
    result = {
        "ok": False,
        "error": "connector_required",
        "message": "camera:// execution needs a dedicated connector; configured API metadata is available",
        "scheme": "camera",
        "node": "lenovo",
        "api": {"url": "http://192.168.188.201:8765"},
    }
    repaired = repair_chain(prompt="pokaż co widzi kamera w lenovo", result=result)
    d = repaired["diagnosis"]
    assert d["kind"] == "missing-connector"
    assert d["scheme"] == "camera"
    assert d["node"] == "lenovo"
    assert "urirun-connector-camera" in d["candidates"]
    assert "pip install urirun-connector-camera" in d["installCommand"]
    assert any(a["id"] == "install-connector" for a in d["actions"])
    assert d["canAutoRetry"] is False
    # Known scheme → NOT speculative.
    assert d["speculative"] is False


def test_missing_connector_diagnosis_unknown_scheme_is_speculative() -> None:
    result = {
        "ok": False,
        "error": "connector_required",
        "message": "ssh:// execution needs a dedicated connector",
        "scheme": "ssh",
        "node": "server",
    }
    d = repair_chain(result=result)["diagnosis"]
    assert d["kind"] == "missing-connector"
    assert d["scheme"] == "ssh"
    assert "ssh" in d["installCommand"]
    # Unknown scheme → speculative:true so the re-planner knows this is a best-guess,
    # not a verified package name.
    assert d["speculative"] is True
    assert d["candidates"] == []
    # The install action itself is also marked speculative.
    install_action = next(a for a in d["actions"] if a["id"] == "install-connector")
    assert install_action.get("speculative") is True


def test_stopped_service_diagnosis() -> None:
    result = {
        "ok": False,
        "error": {
            "type": "ConnectionError",
            "message": "connection refused [Errno 111] connecting to 192.168.188.201:8765",
            "uri": "kvm://laptop/ui/query/find",
        },
    }
    d = repair_chain(result=result)["diagnosis"]
    assert d["kind"] == "stopped-service"
    assert d["canAutoRetry"] is False
    assert any(a["id"] == "start-service" for a in d["actions"])
    assert any(a["id"] == "check-health" for a in d["actions"])


def test_busy_port_diagnosis() -> None:
    result = {
        "ok": False,
        "error": {
            "type": "OSError",
            "message": "OSError: [Errno 98] address already in use :8765",
            "uri": "dashboard://host/service/scanner/command/start",
        },
    }
    d = repair_chain(result=result)["diagnosis"]
    assert d["kind"] == "busy-port"
    assert d["port"] == "8765"
    assert d["canAutoRetry"] is False
    assert any(a["id"] == "identify-port-owner" for a in d["actions"])


def test_failed_verification_diagnosis_from_result_block() -> None:
    result = {
        "ok": False,
        "verification": {
            "ok": False,
            "expected": 3,
            "actual": 1,
            "checks": [
                {"check": "files-transferred", "ok": False},
                {"check": "sha256-match", "ok": True},
            ],
        },
        "error": {"type": "Error", "message": "some other error"},
    }
    d = repair_chain(result=result)["diagnosis"]
    assert d["kind"] == "failed-verification"
    assert "files-transferred" in d["failedChecks"]
    assert d["expected"] == 3
    assert d["actual"] == 1
    assert d["canAutoRetry"] is False
    assert any(a["id"] == "inspect-verification" for a in d["actions"])


def test_failed_verification_diagnosis_from_error_message() -> None:
    result = {
        "ok": False,
        "error": {
            "type": "ContractError",
            "message": "verification failed: expected 3 transferred, got 0",
        },
    }
    d = repair_chain(result=result)["diagnosis"]
    assert d["kind"] == "failed-verification"


def test_connector_candidates_static_map() -> None:
    from urirun_connector_urifix.core import _connector_candidates
    assert "urirun-connector-fs" in _connector_candidates("fs")
    assert "urirun-connector-kvm" in _connector_candidates("kvm")
    assert _connector_candidates("nonexistent-scheme-xyz") == []


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
