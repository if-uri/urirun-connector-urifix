# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.

"""Repair connector for urirun URI-chain failures.

`urifix://` is intentionally an ecosystem repair surface, not a flow planner. It
reads a failed prompt/flow/result and returns deterministic patches and retry
instructions for the host/dashboard/node manager.
"""

from __future__ import annotations

import os
import re
from typing import Any

import urirun
from urirun.runtime import errors as uri_errors

CONNECTOR_ID = "urifix"
conn = urirun.connector(CONNECTOR_ID, scheme="urifix")

ROUTE_DIAGNOSE = "urifix://host/chain/query/diagnose"
ROUTE_REPAIR = "urifix://host/chain/command/repair"


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _error(result: dict) -> dict:
    direct = result.get("error")
    if isinstance(direct, dict):
        return dict(direct)
    if direct:
        return {"type": "Error", "message": str(direct)}
    for step in _as_list(result.get("timeline")):
        if isinstance(step, dict) and step.get("ok") is False:
            step_error = step.get("error")
            if isinstance(step_error, dict):
                return dict(step_error)
            if step_error:
                return {"type": "Error", "message": str(step_error)}
    return {}


_PRECONDITION_MSGS = ("node_url is required", "urirun_llm_model", "llm_model")


def _classify_category(out: dict) -> str:
    msg = str(out.get("message") or "").casefold()
    if any(p in msg for p in _PRECONDITION_MSGS):
        return "FAILED_PRECONDITION"
    return uri_errors.classify(str(out.get("type") or ""), str(out.get("message") or ""))


def _normalize_error(error: dict) -> dict:
    out = dict(error or {})
    out.setdefault("type", "Error")
    out.setdefault("message", "")
    uri = str(out.get("uri") or "")
    if not out.get("category"):
        out["category"] = _classify_category(out)
    scheme = uri.split("://", 1)[0] if "://" in uri else ""
    out.setdefault("code", uri_errors.error_code(str(out.get("type") or ""), str(out.get("message") or ""), scheme))
    status, severity, _ = uri_errors.category_meta(str(out.get("category") or "UNKNOWN"))
    out.setdefault("status", status)
    out.setdefault("severity", severity)
    out.setdefault("errorUri", uri_errors.address(str(out.get("code") or "")))
    out.setdefault("help", uri_errors.help_url(str(out.get("code") or ""), str(out.get("category") or "")))
    return out


def _parse_node_urls(value: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(value, dict):
        items = value.items()
    else:
        items = []
        for item in _as_list(value):
            text = str(item).strip()
            if not text:
                continue
            if "=" in text:
                name, url = text.split("=", 1)
            else:
                name, url = "", text
            items.append((name, url))
    for name, url in items:
        clean_name = str(name).strip()
        if isinstance(url, dict):
            url = url.get("url") or url.get("nodeUrl") or url.get("node_url") or ""
        clean_url = str(url).strip().rstrip("/")
        if clean_name and clean_url:
            out[clean_name] = clean_url
    return out


def _node_alias_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,;|]", value) if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _add_node_aliases(out: dict[str, str], name: str, aliases: Any = None) -> None:
    clean_name = str(name or "").strip()
    if not clean_name:
        return
    out.setdefault(clean_name.casefold(), clean_name)
    for alias in _node_alias_values(aliases):
        out.setdefault(alias.casefold(), clean_name)


def _node_alias_map_from_value(value: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(value, dict):
        nodes = value.get("nodes")
        if isinstance(nodes, (dict, list)):
            out.update(_node_alias_map_from_value(nodes))
            return out
        for name, spec in value.items():
            if name == "nodes":
                continue
            if isinstance(spec, dict):
                canonical = str(spec.get("name") or name).strip()
                aliases: list[str] = []
                for key in ("alias", "aliases", "host", "hostname", "label", "labels", "tags"):
                    aliases.extend(_node_alias_values(spec.get(key)))
                _add_node_aliases(out, canonical, aliases)
            else:
                _add_node_aliases(out, str(name))
        return out
    for item in _as_list(value):
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            aliases: list[str] = []
            for key in ("alias", "aliases", "host", "hostname", "label", "labels", "tags"):
                aliases.extend(_node_alias_values(item.get(key)))
            _add_node_aliases(out, name, aliases)
        else:
            text = str(item).strip()
            if not text:
                continue
            name = text.split("=", 1)[0].strip() if "=" in text else text
            _add_node_aliases(out, name)
    return out


def _host_config_node_urls(config: Any) -> dict[str, str]:
    config = _as_dict(config)
    out: dict[str, str] = {}
    for node in _as_list(config.get("nodes")):
        if not isinstance(node, dict):
            continue
        name = str(node.get("name") or "").strip()
        url = str(node.get("url") or "").strip().rstrip("/")
        if name and url:
            out[name] = url
    return out


def _host_config_node_aliases(config: Any) -> dict[str, str]:
    config = _as_dict(config)
    return _node_alias_map_from_value(config.get("nodes") or [])


def _env_node_urls() -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in os.environ.items():
        if not key.startswith("URIRUN_NODE_URL_"):
            continue
        node = key.removeprefix("URIRUN_NODE_URL_").lower().replace("_", "-")
        if node and value:
            out[node] = value.rstrip("/")
    # URIRUN_NODES="node-a=http://host:port,node-b=http://..." — a one-shot inline map.
    out.update(_parse_node_urls(os.environ.get("URIRUN_NODES", "")
                                .replace(";", ",").split(",")) if os.environ.get("URIRUN_NODES") else {})
    return out


def _env_node_aliases() -> dict[str, str]:
    out: dict[str, str] = {}
    for key in os.environ:
        if key.startswith("URIRUN_NODE_URL_"):
            node = key.removeprefix("URIRUN_NODE_URL_").lower().replace("_", "-")
            _add_node_aliases(out, node)
    if os.environ.get("URIRUN_NODES"):
        out.update(_node_alias_map_from_value(os.environ.get("URIRUN_NODES", "").replace(";", ",").split(",")))
    # URIRUN_NODE_ALIASES="node=alias1|alias2,other=desk" keeps deployment-specific
    # vocabulary in configuration instead of connector code.
    for item in os.environ.get("URIRUN_NODE_ALIASES", "").split(","):
        text = item.strip()
        if not text or "=" not in text:
            continue
        name, aliases = text.split("=", 1)
        _add_node_aliases(out, name.strip(), aliases)
    return out


def _known_nodes_file_data() -> Any:
    import json
    path = os.environ.get("URIRUN_NODES_FILE") or os.path.expanduser("~/.urirun/nodes.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _known_nodes_file_urls() -> dict[str, str]:
    """Node URLs the system already knows — the persisted mesh/node registry. This is what lets
    urifix actually RESOLVE a missing node URL instead of only diagnosing it: a node registered
    once (here or by the mesh) is auto-applied on the next failure. Reads ~/.urirun/nodes.json
    (override with URIRUN_NODES_FILE). Accepts {name: url} or [{"name","url"}]; never raises."""
    data = _known_nodes_file_data()
    if not data:
        return {}
    if isinstance(data, dict):
        nodes = data.get("nodes", data)
    else:
        nodes = data
    if isinstance(nodes, dict):
        return _parse_node_urls(nodes)
    out: dict[str, str] = {}
    for node in _as_list(nodes):
        if isinstance(node, dict):
            name = str(node.get("name") or "").strip()
            url = str(node.get("url") or "").strip().rstrip("/")
            if name and url:
                out[name] = url
    return out


def _known_nodes_file_aliases() -> dict[str, str]:
    return _node_alias_map_from_value(_known_nodes_file_data())


def _targets_from_request(request: dict, result: dict) -> list[str]:
    targets = [str(item).strip() for item in _as_list(request.get("targets")) if str(item).strip()]
    for item in _as_list(result.get("selectedTargets")):
        clean = str(item).strip()
        if clean and clean not in targets:
            targets.append(clean)
    return targets


def _append_unique(lst: list[str], item: str) -> None:
    clean = str(item).strip()
    if clean and clean not in lst:
        lst.append(clean)


def _nodes_from_structured(request: dict, result: dict) -> list[str]:
    nodes: list[str] = []
    for source in (_as_list(request.get("nodes")), _as_list(result.get("selectedNodes"))):
        for item in source:
            _append_unique(nodes, item)
    for key in ("node", "targetNode"):
        _append_unique(nodes, str(result.get(key) or ""))
    for target in _targets_from_request(request, result):
        if target.startswith("node:"):
            _append_unique(nodes, target.split(":", 1)[1])
    return nodes


def _nodes_from_flow(result: dict) -> list[str]:
    nodes: list[str] = []
    for step in _as_list(_as_dict(result.get("flow")).get("steps")):
        if not isinstance(step, dict):
            continue
        payload = _as_dict(step.get("payload"))
        for key in ("node", "targetNode"):
            _append_unique(nodes, str(payload.get(key) or ""))
    return nodes


def _nodes_from_aliases(prompt: str, alias_map: dict[str, str]) -> list[str]:
    nodes: list[str] = []
    lowered = prompt.casefold()
    for alias, node in sorted(alias_map.items(), key=lambda item: len(item[0]), reverse=True):
        if not alias or node in nodes:
            continue
        if re.search(rf"(?<![\w.-]){re.escape(alias)}(?![\w.-])", lowered):
            nodes.append(node)
    return nodes


def _nodes_from_request(prompt: str, request: dict, result: dict, alias_map: dict[str, str] | None = None) -> list[str]:
    nodes = _nodes_from_structured(request, result)
    for node in _nodes_from_flow(result):
        _append_unique(nodes, node)
    # Alias-map inference only runs when no concrete nodes came from structured sources.
    # If the result or request already named specific nodes, trust them — don't guess more
    # from prompt keywords that may be generic words (e.g. "laptop" in "lenovo laptop").
    if not nodes:
        nodes.extend(_nodes_from_aliases(prompt, alias_map or {}))
    return nodes


def _flow_step(result: dict, uri: str) -> dict:
    flow = _as_dict(result.get("flow"))
    for step in _as_list(flow.get("steps")):
        if isinstance(step, dict) and (not uri or step.get("uri") == uri):
            return dict(step)
    return {}


# Static scheme → connector package map.  Authoritative for install hints; connectors not yet
# published as standalone packages are omitted (they live in urirun core or are unreleased).
_SCHEME_CONNECTORS: dict[str, list[str]] = {
    "adb": ["urirun-connector-adb"],
    "adopt": ["urirun-connector-adopt"],
    "base64": ["urirun-connector-base64"],
    "browser": ["urirun-connector-browser-control"],
    "cdp": ["urirun-connector-browser-control"],
    "camera": ["urirun-connector-camera", "urirun-connector-camera-web"],
    "webcam": ["urirun-connector-camera-web"],
    "doc": ["urirun-connector-doc"],
    "docid": ["urirun-connector-docid"],
    "domain": ["urirun-connector-domain-monitor"],
    "email": ["urirun-connector-email"],
    "flow": ["urirun-flow"],
    "fs": ["urirun-connector-fs"],
    "get": ["urirun-connector-get-node"],
    "github": ["urirun-connector-github"],
    "hash": ["urirun-connector-hash"],
    "http-check": ["urirun-connector-http-check"],
    "invoice": ["urirun-connector-invoice"],
    "ksef": ["urirun-connector-ksef"],
    "kvm": ["urirun-connector-kvm"],
    "linkedin": ["urirun-connector-linkedin"],
    "llm": ["urirun-connector-llm"],
    "mcp-fs": ["urirun-connector-mcp-filesystem"],
    "mqtt": ["urirun-connector-mqtt"],
    "namecheap": ["urirun-connector-namecheap-dns"],
    "netscan": ["urirun-connector-netscan"],
    "ocr": ["urirun-connector-ocr"],
    "planfile": ["urirun-connector-planfile"],
    "sheet": ["urirun-connector-sheet"],
    "smartcrop": ["urirun-connector-smart-crop"],
    "sqlite": ["urirun-connector-sqlite-context"],
    "time": ["urirun-connector-time-tools"],
    "usb": ["urirun-connector-usb"],
    "uuid": ["urirun-connector-uuid"],
    "webnode": ["urirun-connector-webnode"],
}


def _connector_candidates(scheme: str) -> list[str]:
    return list(_SCHEME_CONNECTORS.get(scheme.casefold(), []))


# Ordered list of (message substrings, kind) — first match wins.
_MSG_KIND: list[tuple[tuple[str, ...], str]] = [
    (("node_url is required",), "missing-node-url"),
    (("urirun_llm_model", "llm_model"), "missing-llm-model"),
    (("connector_required", "needs a dedicated connector"), "missing-connector"),
    (("route not found",), "missing-route"),
    (("unauthorized", "x-urirun-token", "enrolled-key", "requires run auth"), "missing-auth"),
    (("address already in use", "port is already in use", "errno 98"), "busy-port"),
    (("connection refused", "[errno 111]", "service not running"), "stopped-service"),
    (("verification failed", "verification contract"), "failed-verification"),
    (("file not found", "no such file"), "missing-file"),
]

_CATEGORY_KIND: dict[str, str] = {
    "UNAUTHENTICATED": "missing-auth", "PERMISSION_DENIED": "missing-auth",
    "UNAVAILABLE": "transient-target", "DEADLINE_EXCEEDED": "transient-target",
    "INVALID_ARGUMENT": "invalid-payload",
}


def _is_fs_transfer_failure(msg: str) -> bool:
    return ("missing required fs transfer route" in msg
            or "remote write returned no sha256" in msg
            or ("fs.file.command" in msg and "not found" in msg))


def _error_kind(error: dict) -> str:
    msg = str(error.get("message") or "").casefold()
    error_type = str(error.get("type") or "").casefold()
    category = str(error.get("category") or "")
    if _is_fs_transfer_failure(msg):
        return "missing-fs-transfer-route"
    for patterns, kind in _MSG_KIND:
        if any(p in msg for p in patterns):
            return kind
    if category in {"UNAUTHENTICATED", "PERMISSION_DENIED"}:
        return "missing-auth"
    if error_type == "registry":
        return "missing-route"
    return _CATEGORY_KIND.get(category, "unknown")


def _node_url_for(node: str, node_urls: dict[str, str]) -> str:
    if node in node_urls:
        return node_urls[node]
    lowered = node.casefold()
    for name, url in node_urls.items():
        if name.casefold() == lowered:
            return url
    return ""


def _node_url_retry(error_uri: str, step_payload: dict, execute: bool) -> dict | None:
    if not error_uri:
        return None
    return {
        "uri": error_uri,
        "mode": "execute" if execute else "dry-run",
        "payload": step_payload,
    }


def _node_url_actions(node: str, url: str, error_uri: str, retry: dict | None) -> list[dict]:
    actions: list[dict] = []
    if url and retry:
        actions.append({"id": "retry-with-node-url", "kind": "retry", "automatic": True,
                        "label": f"Retry {error_uri} with node_url={url}."})
    if not url:
        placeholder = node or "<node>"
        actions.append({"id": "provide-node-url", "kind": "config", "automatic": False,
                        "label": f"Add node URL for {placeholder}: pass node_urls=['{placeholder}=http://HOST:PORT'] or add it to host config."})
    actions.append({"id": "ensure-node-target", "kind": "payload", "automatic": True,
                    "label": "Keep targets and nodes consistent: node:<name> implies nodes=[<name>]."})
    return actions


def _missing_node_url_diagnosis(prompt: str, request: dict, result: dict, node_urls: dict[str, str],
                                alias_map: dict[str, str]) -> dict:
    nodes = _nodes_from_request(prompt, request, result, alias_map)
    node = nodes[0] if nodes else ""
    url = _node_url_for(node, node_urls) if node else ""
    targets = _targets_from_request(request, result)
    if node and f"node:{node}" not in targets:
        targets.append(f"node:{node}")
    error_uri = str(_error(result).get("uri") or "")
    step = _flow_step(result, error_uri)
    step_payload = dict(step.get("payload") or {})
    if node:
        step_payload.setdefault("node", node)
    if url:
        step_payload["node_url"] = url
    execute = bool(result.get("execute") or request.get("execute"))
    retry = _node_url_retry(error_uri, step_payload, execute)
    patch_request: dict = {"nodes": [node] if node else [], "targets": targets or ["host"]}
    if node and url:
        patch_request["node_urls"] = [f"{node}={url}"]
    return {
        "kind": "missing-node-url",
        "summary": "The flow selected a node name but the host cannot resolve it to a node URL.",
        "node": node,
        "nodeUrl": url,
        "canAutoRetry": bool(url and retry),
        "patch": {"request": patch_request, "stepPayload": step_payload},
        "retry": retry,
        "actions": _node_url_actions(node, url, error_uri, retry),
    }


def _missing_llm_model_diagnosis() -> dict:
    return {
        "kind": "missing-llm-model",
        "summary": "The generic planner needs URIRUN_LLM_MODEL or LLM_MODEL; known intents should bypass LLM.",
        "canAutoRetry": False,
        "patch": {"env": {"URIRUN_LLM_MODEL": "<provider/model or local model>"}},
        "retry": None,
        "actions": [
            {
                "id": "use-known-intent",
                "kind": "planner",
                "automatic": True,
                "label": "Route known prompts through deterministic intent handlers before the LLM planner.",
            },
            {
                "id": "configure-llm-model",
                "kind": "config",
                "automatic": False,
                "label": "Set URIRUN_LLM_MODEL or LLM_MODEL for open-ended prompts.",
            },
        ],
    }


def _missing_route_diagnosis(error: dict) -> dict:
    uri = str(error.get("uri") or "")
    scheme = uri.split("://", 1)[0] if "://" in uri else ""
    return {
        "kind": "missing-route",
        "summary": "The URI route is not present in the current registry or node surface.",
        "scheme": scheme,
        "canAutoRetry": False,
        "patch": {},
        "retry": None,
        "actions": [
            {"id": "refresh-routes", "kind": "discovery", "automatic": False, "label": "Refresh /routes and rebuild the registry."},
            *([{
                "id": "resolve-connector",
                "kind": "provision",
                "automatic": False,
                "uri": f"connector://host/{scheme}/query/resolve",
                "label": f"Resolve or install a connector that serves {scheme}://.",
            }] if scheme else []),
        ],
    }


def _document_sync_payload(result: dict, node: str, node_url: str) -> dict:
    error_uri = str(_error(result).get("uri") or "document://host/archive/command/sync-to-node")
    step = _flow_step(result, error_uri) or _flow_step(result, "document://host/archive/command/sync-to-node")
    payload = dict(step.get("payload") or {})
    for key in ("dest_root", "destRoot", "source_root", "sourceRoot"):
        if result.get(key) is not None and key not in payload:
            payload[key] = result.get(key)
    if node:
        payload.setdefault("node", node)
    if node_url:
        payload["node_url"] = node_url
    payload.setdefault("ensure_routes", True)
    return payload


def _fs_missing_routes(result: dict) -> list[str]:
    preflight = result.get("preflight") if isinstance(result.get("preflight"), dict) else {}
    missing = [str(item) for item in _as_list(preflight.get("missingAfter") or preflight.get("missingBefore"))]
    if not missing:
        missing = [str(result[k]) for k in ("fsUri", "fsReadUri") if result.get(k)]
    return missing or ["fs://host/file/command/write-b64", "fs://host/file/query/read-b64"]


def _fs_patch_request(node: str, node_url: str, targets: list[str]) -> dict:
    patch: dict = {"nodes": [node] if node else [], "targets": targets or ["host"]}
    if node and node_url:
        patch["node_urls"] = [f"{node}={node_url}"]
    return patch


def _fs_actions(node_url: str, node: str) -> list[dict]:
    auto = bool(node_url)
    actions = [
        {"id": "retry-document-sync-with-route-preflight", "kind": "retry", "automatic": auto,
         "label": "Retry document sync with ensure_routes=true; host will provision fs file-transfer routes before copying."},
        {"id": "provision-fs-file-transfer", "kind": "provision", "automatic": auto,
         "label": "Ensure fs://host/file/command/write-b64 and fs://host/file/query/read-b64 on the target node."},
    ]
    if not node_url:
        actions.append({"id": "provide-node-url", "kind": "config", "automatic": False,
                        "label": f"Add node URL for {node or '<node>'} before retrying document sync."})
    return actions


def _missing_fs_transfer_route_diagnosis(prompt: str, request: dict, result: dict,
                                         node_urls: dict[str, str], alias_map: dict[str, str]) -> dict:
    """The document sync reached a node but the node lacks fs file-transfer routes.

    Recoverable by retrying with route preflight enabled: the host will provision the
    narrow fs file-transfer shim via /deploy.  urifix returns the safe retry only.
    """
    nodes = _nodes_from_request(prompt, request, result, alias_map)
    node = nodes[0] if nodes else str(result.get("node") or "").strip()
    node_url = str(result.get("nodeUrl") or result.get("node_url") or "").strip().rstrip("/")
    if not node_url and node:
        node_url = _node_url_for(node, node_urls)
    sync_uri = str(result.get("uri") or _error(result).get("uri") or "document://host/archive/command/sync-to-node")
    if not sync_uri.startswith("document://"):
        sync_uri = "document://host/archive/command/sync-to-node"
    payload = _document_sync_payload(result, node, node_url)
    missing = _fs_missing_routes(result)
    execute = bool(result.get("execute") or request.get("execute"))
    retry: dict | None = {"uri": sync_uri, "mode": "execute" if execute else "dry-run", "payload": payload}
    targets = _targets_from_request(request, result)
    if node and f"node:{node}" not in targets:
        targets.append(f"node:{node}")
    return {
        "kind": "missing-fs-transfer-route",
        "summary": "The node is reachable, but it lacks the fs:// file-transfer routes required by document sync.",
        "node": node,
        "nodeUrl": node_url,
        "missingRoutes": missing,
        "canAutoRetry": bool(node_url),
        "patch": {"request": _fs_patch_request(node, node_url, targets), "stepPayload": payload},
        "retry": retry if node_url else None,
        "actions": _fs_actions(node_url, node),
    }


def _missing_auth_diagnosis(error: dict) -> dict:
    """A node refused the call for lack of management auth (X-Urirun-Token / enrolled key).
    Never auto-recoverable: urifix will not supply or fabricate credentials — it surfaces the
    exact human action instead (a deliberate credential boundary)."""
    uri = str(error.get("uri") or "")
    node = uri.split("://", 1)[1].split("/", 1)[0] if "://" in uri else ""
    return {
        "kind": "missing-auth",
        "summary": "The target node requires management auth (X-Urirun-Token or an enrolled key) for this route; urifix cannot supply credentials.",
        "node": node,
        "canAutoRetry": False,
        "patch": {"env": {"URIRUN_TOKEN": "<node management token>"}},
        "retry": None,
        "actions": [
            {"id": "provide-node-token", "kind": "config", "automatic": False,
             "label": f"Set the node management token (host env URIRUN_TOKEN, or a per-node token) for {node or '<node>'} and retry."},
            {"id": "enroll-key", "kind": "config", "automatic": False,
             "label": "Or run from a host whose ed25519 public key is enrolled on the node (signed runs)."},
        ],
    }


def _missing_connector_diagnosis(result: dict, error: dict) -> dict:
    scheme = str(result.get("scheme") or "").strip()
    if not scheme:
        # Fallback: parse from error URI or message.
        uri = str(error.get("uri") or result.get("uri") or "")
        scheme = uri.split("://", 1)[0] if "://" in uri else ""
    node = str(result.get("node") or "").strip()
    candidates = _connector_candidates(scheme)
    known = bool(candidates)
    # Speculative when scheme not in the static map: the install command is a best-guess
    # fallback ("urirun-connector-<scheme>") that may not exist.  Mark it so the re-planner
    # treats it as a hypothesis, not a fact — same honesty as plausibility=0→HITL.
    speculative = not known
    install_cmd = f"pip install {candidates[0]}" if candidates else (
        f"pip install urirun-connector-{scheme}" if scheme else "pip install <connector-package>"
    )
    return {
        "kind": "missing-connector",
        "summary": (
            f"{scheme}:// requires a dedicated connector that is not installed."
            if scheme else "A required connector is not installed."
        ),
        "scheme": scheme,
        "node": node,
        "candidates": candidates,
        "installCommand": install_cmd,
        "speculative": speculative,
        "canAutoRetry": False,
        "patch": {},
        "retry": None,
        "actions": [
            {
                "id": "install-connector",
                "kind": "provision",
                "automatic": False,
                "label": install_cmd,
                "installCommand": install_cmd,
                "packages": candidates,
                **({"speculative": True} if speculative else {}),
            },
            {
                "id": "retry-after-install",
                "kind": "retry",
                "automatic": False,
                "label": (
                    f"After installing, retry the original {scheme}:// call."
                    if scheme else "After installing the connector, retry the original call."
                ),
            },
        ],
    }


def _stopped_service_diagnosis(error: dict) -> dict:
    uri = str(error.get("uri") or "")
    scheme = uri.split("://", 1)[0] if "://" in uri else ""
    host_part = uri.split("://", 1)[1].split("/", 1)[0] if "://" in uri else ""
    return {
        "kind": "stopped-service",
        "summary": "The target service or node is not running (connection refused).",
        "scheme": scheme,
        "host": host_part,
        "canAutoRetry": False,
        "patch": {},
        "retry": None,
        "actions": [
            {
                "id": "start-service",
                "kind": "lifecycle",
                "automatic": False,
                "label": (
                    f"Start or restart the service at {host_part} and retry."
                    if host_part else "Start the required service and retry."
                ),
            },
            {
                "id": "check-health",
                "kind": "diagnostic",
                "automatic": False,
                "label": "Run health check on the target node to confirm it is reachable.",
            },
        ],
    }


def _busy_port_diagnosis(error: dict) -> dict:
    message = str(error.get("message") or "")
    port = ""
    import re as _re
    m = _re.search(r":(\d{2,5})", message)
    if m:
        port = m.group(1)
    return {
        "kind": "busy-port",
        "summary": "A port required by a service is already in use.",
        "port": port,
        "canAutoRetry": False,
        "patch": {},
        "retry": None,
        "actions": [
            {
                "id": "identify-port-owner",
                "kind": "diagnostic",
                "automatic": False,
                "label": f"Identify what is using port {port} (e.g. lsof -i :{port}) and stop it, or reconfigure the service to use a different port." if port else "Identify the process holding the port and stop it, or reconfigure the service.",
            },
            {
                "id": "port-replace",
                "kind": "lifecycle",
                "automatic": False,
                "label": "Use port-replace mode to restart the service on the same port after clearing the occupant.",
            },
        ],
    }


def _failed_verification_diagnosis(result: dict, error: dict) -> dict:
    verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    checks = [c for c in (verification.get("checks") or []) if isinstance(c, dict) and not c.get("ok")]
    failed_names = [str(c.get("check") or c.get("name") or "?") for c in checks]
    expected = verification.get("expected")
    actual = verification.get("actual")
    return {
        "kind": "failed-verification",
        "summary": "The operation completed but the post-execution verification contract failed.",
        "failedChecks": failed_names,
        "expected": expected,
        "actual": actual,
        "canAutoRetry": False,
        "patch": {},
        "retry": None,
        "actions": [
            {
                "id": "inspect-verification",
                "kind": "diagnostic",
                "automatic": False,
                "label": (
                    f"Inspect failed checks: {', '.join(failed_names)}."
                    if failed_names else "Inspect the verification block for mismatched expected/actual counts."
                ),
            },
            {
                "id": "manual-verify",
                "kind": "diagnostic",
                "automatic": False,
                "label": "Manually verify the side-effecting operation succeeded and re-run or rollback as appropriate.",
            },
        ],
        "verification": verification,
    }


def _generic_diagnosis(kind: str, error: dict) -> dict:
    by_kind = {
        "missing-file": ("A referenced file/artifact is missing.", "mark-stale-artifact"),
        "transient-target": ("The target node/service is unavailable or timed out.", "retry-after-health-check"),
        "invalid-payload": ("The payload does not match the route schema.", "repair-payload"),
    }
    summary, action_id = by_kind.get(kind, ("The failure is not recognized by urifix yet.", "inspect-error"))
    return {
        "kind": kind,
        "summary": summary,
        "canAutoRetry": False,
        "patch": {},
        "retry": None,
        "actions": [{
            "id": action_id,
            "kind": "diagnostic",
            "automatic": False,
            "label": summary,
        }],
        "error": error,
    }


def build_diagnosis(prompt: str = "", request: dict | None = None, result: dict | None = None,
                    node_urls: list[str] | dict | None = None, host_config: dict | None = None,
                    known_nodes: list[str] | dict | None = None) -> dict:
    request = _as_dict(request)
    result = _as_dict(result)
    error = _normalize_error(_error(result))
    # Lowest→highest priority: the system's known nodes (file/registry), env, host config, then
    # whatever the caller passed explicitly. Earlier sources are overridden by later ones.
    url_map = {
        **_known_nodes_file_urls(),
        **_env_node_urls(),
        **_host_config_node_urls(host_config),
        **_parse_node_urls(known_nodes),
        **_parse_node_urls(node_urls),
    }
    alias_map = {
        **_known_nodes_file_aliases(),
        **_env_node_aliases(),
        **_host_config_node_aliases(host_config),
        **_node_alias_map_from_value(known_nodes),
        **_node_alias_map_from_value(node_urls),
    }
    # failed-verification is detected from the result structure, not just the error message.
    verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    if not result.get("ok") and verification and not verification.get("ok"):
        kind = "failed-verification"
    else:
        kind = _error_kind(error)
    if kind == "missing-node-url":
        diagnosis = _missing_node_url_diagnosis(prompt, request, result, url_map, alias_map)
    elif kind == "missing-llm-model":
        diagnosis = _missing_llm_model_diagnosis()
    elif kind == "missing-route":
        diagnosis = _missing_route_diagnosis(error)
    elif kind == "missing-fs-transfer-route":
        diagnosis = _missing_fs_transfer_route_diagnosis(prompt, request, result, url_map, alias_map)
    elif kind == "missing-auth":
        diagnosis = _missing_auth_diagnosis(error)
    elif kind == "missing-connector":
        diagnosis = _missing_connector_diagnosis(result, error)
    elif kind == "stopped-service":
        diagnosis = _stopped_service_diagnosis(error)
    elif kind == "busy-port":
        diagnosis = _busy_port_diagnosis(error)
    elif kind == "failed-verification":
        diagnosis = _failed_verification_diagnosis(result, error)
    else:
        diagnosis = _generic_diagnosis(kind, error)
    diagnosis["error"] = error
    diagnosis["input"] = {
        "prompt": prompt,
        "selectedNodes": _nodes_from_request(prompt, request, result, alias_map),
        "selectedTargets": _targets_from_request(request, result),
        "nodeUrlNames": sorted(url_map),
        "nodeAliasNames": sorted(alias_map),
    }
    return diagnosis


@conn.handler("chain/query/diagnose", isolated=True, meta={"label": "Diagnose a failed URI chain"})
def diagnose_chain(prompt: str = "", request: dict | None = None, result: dict | None = None,
                   node_urls: list[str] | dict | None = None, host_config: dict | None = None,
                   known_nodes: list[str] | dict | None = None) -> dict[str, Any]:
    diagnosis = build_diagnosis(prompt, request, result, node_urls, host_config, known_nodes)
    return urirun.ok(diagnosis=diagnosis, recovery=diagnosis.get("actions") or [])


def _execute_retry(retry: dict, registry: Any) -> dict:
    """Actually run the repaired retry through urirun — this is the 'resolve' half. Side effects
    happen ONLY here, only when the host opts in with apply=True AND a registry."""
    import urirun as _u
    reg = registry
    if isinstance(registry, str):
        from urirun import v2
        reg = v2.load_registry_arg(registry)
    elif isinstance(registry, dict) and "routes" not in registry:
        reg = _u.compile_registry(registry)
    uri = str(retry.get("uri") or "")
    scheme = uri.split("://", 1)[0] if "://" in uri else "*"
    env = _u.run(uri, reg, payload=retry.get("payload") or {},
                 mode=retry.get("mode") or "execute", policy=_u.policy(allow=[f"{scheme}://*"]))
    value = _u.result_data(env)
    ok = bool(env.get("ok")) and (value.get("ok", True) if isinstance(value, dict) else True)
    return {"ok": ok, "envelope": env, "value": value}


@conn.handler("chain/command/repair", isolated=True, meta={"label": "Diagnose and (optionally) resolve a failed URI chain"})
def repair_chain(prompt: str = "", request: dict | None = None, result: dict | None = None,
                 node_urls: list[str] | dict | None = None, host_config: dict | None = None,
                 known_nodes: list[str] | dict | None = None, apply: bool = False,
                 registry: Any = None) -> dict[str, Any]:
    """Diagnose the failure and, when it is auto-recoverable, optionally RESOLVE it: with
    apply=True and a `registry`, run the repaired retry and report the outcome. Without a
    registry it stays a safe plan (no side effects), so the host keeps one schema for both."""
    diagnosis = build_diagnosis(prompt, request, result, node_urls, host_config, known_nodes)
    retry = diagnosis.get("retry")
    can_auto = bool(diagnosis.get("canAutoRetry"))

    run_result = None
    applied = False
    if apply and can_auto and retry and registry is not None:
        try:
            run_result = _execute_retry(retry, registry)
            applied = True
        except Exception as exc:  # noqa: BLE001 - a repair attempt must not mask the diagnosis
            run_result = {"ok": False, "error": str(exc)}
            applied = True

    return urirun.ok(
        # "repaired" now means actually fixed when we executed; else whether it COULD be retried.
        repaired=bool(run_result.get("ok")) if applied else can_auto,
        applied=applied,
        applyRequested=bool(apply),
        diagnosis=diagnosis,
        patch=diagnosis.get("patch") or {},
        retry=retry,
        runResult=run_result,
        recovery=diagnosis.get("actions") or [],
    )


def urirun_bindings() -> dict[str, Any]:
    return conn.bindings()


def connector_manifest() -> dict[str, Any]:
    return conn.manifest(urirun.load_manifest(__package__))


def main(argv: list[str] | None = None) -> int:
    return conn.cli(argv, manifest_prose=urirun.load_manifest(__package__))


if __name__ == "__main__":
    raise SystemExit(main())
