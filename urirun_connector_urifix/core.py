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


def _normalize_error(error: dict) -> dict:
    out = dict(error or {})
    out.setdefault("type", "Error")
    out.setdefault("message", "")
    uri = str(out.get("uri") or "")
    if not out.get("category"):
        message = str(out.get("message") or "").casefold()
        if "node_url is required" in message or "urirun_llm_model" in message or "llm_model" in message:
            out["category"] = "FAILED_PRECONDITION"
        else:
            out["category"] = uri_errors.classify(str(out.get("type") or ""), str(out.get("message") or ""))
    out.setdefault("code", uri_errors.error_code(str(out.get("type") or ""), str(out.get("message") or ""), uri.split("://", 1)[0] if "://" in uri else ""))
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


def _nodes_from_request(prompt: str, request: dict, result: dict, alias_map: dict[str, str] | None = None) -> list[str]:
    nodes: list[str] = []
    for source in (_as_list(request.get("nodes")), _as_list(result.get("selectedNodes"))):
        for item in source:
            clean = str(item).strip()
            if clean and clean not in nodes:
                nodes.append(clean)
    for target in _targets_from_request(request, result):
        if target.startswith("node:"):
            node = target.split(":", 1)[1].strip()
            if node and node not in nodes:
                nodes.append(node)
    flow = _as_dict(result.get("flow"))
    for step in _as_list(flow.get("steps")):
        if not isinstance(step, dict):
            continue
        payload = _as_dict(step.get("payload"))
        for key in ("node", "targetNode"):
            node = str(payload.get(key) or "").strip()
            if node and node not in nodes:
                nodes.append(node)
    alias_map = alias_map or {}
    lowered = prompt.casefold()
    for alias, node in sorted(alias_map.items(), key=lambda item: len(item[0]), reverse=True):
        if not alias or node in nodes:
            continue
        if re.search(rf"(?<![\w.-]){re.escape(alias)}(?![\w.-])", lowered):
            nodes.append(node)
    return nodes


def _flow_step(result: dict, uri: str) -> dict:
    flow = _as_dict(result.get("flow"))
    for step in _as_list(flow.get("steps")):
        if isinstance(step, dict) and (not uri or step.get("uri") == uri):
            return dict(step)
    return {}


def _error_kind(error: dict) -> str:
    message = str(error.get("message") or "").casefold()
    error_type = str(error.get("type") or "").casefold()
    if "node_url is required" in message:
        return "missing-node-url"
    if "urirun_llm_model" in message or "llm_model" in message:
        return "missing-llm-model"
    if "route not found" in message or error_type == "registry":
        return "missing-route"
    if ("unauthorized" in message or "x-urirun-token" in message or "enrolled-key" in message
            or "requires run auth" in message
            or str(error.get("category") or "") in {"UNAUTHENTICATED", "PERMISSION_DENIED"}):
        return "missing-auth"
    if "file not found" in message or "no such file" in message:
        return "missing-file"
    if str(error.get("category") or "") in {"UNAVAILABLE", "DEADLINE_EXCEEDED"}:
        return "transient-target"
    if str(error.get("category") or "") in {"INVALID_ARGUMENT"}:
        return "invalid-payload"
    return "unknown"


def _node_url_for(node: str, node_urls: dict[str, str]) -> str:
    if node in node_urls:
        return node_urls[node]
    lowered = node.casefold()
    for name, url in node_urls.items():
        if name.casefold() == lowered:
            return url
    return ""


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
    retry = None
    if error_uri:
        retry = {
            "uri": error_uri,
            "mode": "execute" if result.get("execute") or request.get("execute") else "dry-run",
            "payload": step_payload,
        }
    return {
        "kind": "missing-node-url",
        "summary": "The flow selected a node name but the host cannot resolve it to a node URL.",
        "node": node,
        "nodeUrl": url,
        "canAutoRetry": bool(url and retry),
        "patch": {
            "request": {
                "nodes": [node] if node else [],
                "targets": targets or ["host"],
                **({"node_urls": [f"{node}={url}"]} if node and url else {}),
            },
            "stepPayload": step_payload,
        },
        "retry": retry,
        "actions": [
            *([{
                "id": "retry-with-node-url",
                "kind": "retry",
                "automatic": True,
                "label": f"Retry {error_uri} with node_url={url}.",
            }] if url and retry else []),
            *([] if url else [{
                "id": "provide-node-url",
                "kind": "config",
                "automatic": False,
                "label": f"Add node URL for {node or '<node>'}: pass node_urls=['{node or '<node>'}=http://HOST:PORT'] or add it to host config.",
            }]),
            {
                "id": "ensure-node-target",
                "kind": "payload",
                "automatic": True,
                "label": "Keep targets and nodes consistent: node:<name> implies nodes=[<name>].",
            },
        ],
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
    kind = _error_kind(error)
    if kind == "missing-node-url":
        diagnosis = _missing_node_url_diagnosis(prompt, request, result, url_map, alias_map)
    elif kind == "missing-llm-model":
        diagnosis = _missing_llm_model_diagnosis()
    elif kind == "missing-route":
        diagnosis = _missing_route_diagnosis(error)
    elif kind == "missing-auth":
        diagnosis = _missing_auth_diagnosis(error)
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
