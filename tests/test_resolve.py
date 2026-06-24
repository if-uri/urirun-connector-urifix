"""urifix doesn't just diagnose — it RESOLVES: it discovers a missing node URL from the
system's known-nodes registry (so canAutoRetry flips true) and, with apply=True + a registry,
actually runs the repaired retry."""
import json

import pytest

import urirun_connector_urifix.core as c

# the document-sync failure from the dashboard, reduced to its essentials
_RESULT = {
    "ok": False,
    "execute": True,
    "error": {"type": "ValueError",
              "message": "node_url is required when the target node is not present in host config",
              "uri": "demo://host/sync/command/run"},
    "selectedNodes": ["lenovo"],
    "selectedTargets": ["node:lenovo"],
    "flow": {"steps": [{"id": "s", "uri": "demo://host/sync/command/run",
                        "payload": {"node": "lenovo", "dest_root": "~/Downloads/urirun-scans"}}]},
}
_PROMPT = "wyślij artifacts do lenovo"


def test_without_url_only_diagnoses(tmp_path, monkeypatch):
    monkeypatch.setenv("URIRUN_NODES_FILE", str(tmp_path / "missing-nodes.json"))
    monkeypatch.delenv("URIRUN_NODES", raising=False)
    monkeypatch.delenv("URIRUN_NODE_ALIASES", raising=False)
    monkeypatch.delenv("URIRUN_NODE_URL_LENOVO", raising=False)
    d = c.build_diagnosis(_PROMPT, {}, _RESULT)  # no URL anywhere
    assert d["kind"] == "missing-node-url" and d["canAutoRetry"] is False
    assert any(a["id"] == "provide-node-url" for a in d["actions"])


def test_known_nodes_param_resolves_url():
    d = c.build_diagnosis(_PROMPT, {}, _RESULT, known_nodes={"lenovo": "http://192.168.188.201:8765"})
    assert d["canAutoRetry"] is True
    assert d["nodeUrl"] == "http://192.168.188.201:8765"
    assert d["retry"]["payload"]["node_url"] == "http://192.168.188.201:8765"
    assert "lenovo=http://192.168.188.201:8765" in d["patch"]["request"]["node_urls"]
    # now the automatic retry action is offered instead of the manual config one
    assert any(a["id"] == "retry-with-node-url" for a in d["actions"])


def test_known_nodes_file_discovery(tmp_path, monkeypatch):
    nodes = tmp_path / "nodes.json"
    nodes.write_text(json.dumps({"lenovo": "http://192.168.188.201:8765"}))
    monkeypatch.setenv("URIRUN_NODES_FILE", str(nodes))
    monkeypatch.delenv("URIRUN_NODE_URL_LENOVO", raising=False)
    d = c.build_diagnosis(_PROMPT, {}, _RESULT)
    assert d["canAutoRetry"] is True and d["nodeUrl"].endswith(":8765")


def test_nodes_file_list_shape(tmp_path, monkeypatch):
    nodes = tmp_path / "nodes.json"
    nodes.write_text(json.dumps({"nodes": [{"name": "lenovo", "url": "http://192.168.188.201:8765/"}]}))
    monkeypatch.setenv("URIRUN_NODES_FILE", str(nodes))
    monkeypatch.delenv("URIRUN_NODE_URL_LENOVO", raising=False)
    assert c._known_nodes_file_urls()["lenovo"] == "http://192.168.188.201:8765"


def test_unauthorized_node_install_diagnosed_as_missing_auth():
    # the exact (c3) wall: installing a connector on a node needs management auth
    res = {"ok": False, "error": {
        "type": "PermissionError",
        "message": "unauthorized (node:// management requires X-Urirun-Token or an enrolled-key signature)",
        "uri": "node://laptop/connector/command/install"}}
    d = c.build_diagnosis("zainstaluj fs na lenovo", {}, res)
    assert d["kind"] == "missing-auth" and d["canAutoRetry"] is False
    ids = {a["id"] for a in d["actions"]}
    assert "provide-node-token" in ids and "enroll-key" in ids
    assert d["node"] == "laptop"


def test_repair_plan_only_without_registry():
    r = c.repair_chain(_PROMPT, {}, _RESULT, known_nodes={"lenovo": "http://h:1"}, apply=True)
    # apply requested but no registry -> stays a safe plan, no execution
    assert r["applied"] is False and r["applyRequested"] is True
    assert r["repaired"] is True  # could be retried (canAutoRetry)
    assert r["runResult"] is None


def test_repair_executes_retry_with_registry(tmp_path):
    pytest.importorskip("urirun")
    import urirun
    from urirun import v2
    doc = {"version": v2.VERSION, "bindings": {
        "demo://host/sync/command/run": {"uri": "demo://host/sync/command/run", "kind": "command",
            "adapter": "argv-template", "argv": ["true"], "policy": {"allowExecute": True}}}}
    registry = urirun.compile_registry(doc)
    r = c.repair_chain(_PROMPT, {}, _RESULT, known_nodes={"lenovo": "http://h:1"},
                       apply=True, registry=registry)
    assert r["applied"] is True and r["repaired"] is True
    assert r["runResult"]["ok"] is True
