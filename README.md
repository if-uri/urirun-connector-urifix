# urirun-connector-urifix

`urifix://` diagnoses failed URI chains and returns a repair contract that a host,
chat service or node manager can apply or show to a human.

It is intentionally separate from `flow://.../repair`: flow repair fixes a flow
document; urifix fixes the surrounding urirun ecosystem: missing node URLs,
missing routes/connectors, missing model configuration, stale artifacts and
transient node/service failures.

## Routes

- `urifix://host/chain/query/diagnose` — classify a failed prompt/flow/result.
- `urifix://host/chain/command/repair` — return a deterministic patch/retry plan.

## Example

```bash
urirun-urifix bindings | urirun validate /dev/stdin
```

Payload:

```json
{
  "prompt": "wyślij dokumenty do lenovo",
  "request": {"nodes": [], "targets": ["host", "service:phone-scanner"]},
  "result": {
    "ok": false,
    "selectedNodes": ["lenovo"],
    "error": {
      "type": "ValueError",
      "message": "node_url is required when the target node is not present in host config",
      "uri": "document://host/archive/command/sync-to-node"
    }
  },
  "node_urls": ["lenovo=http://192.168.188.201:8766"]
}
```

Output includes `diagnosis`, `patch`, `retry` and `recovery` fields. The connector
does not contact nodes by itself; it creates the safe next action for the host to
execute.

## How it is used by the dashboard

`urirun-service-chat` calls `urifix://` only after a URI step has already failed
and only if this connector is installed. The original failure remains in the
result. The returned `recovery`, `patch` and `retry` are attached to the chat
message, timeline and log detail.

This means `urifix://` is safe to keep deterministic:

- it can add a known `node_url` from request/config,
- it can suggest an install/route/config action,
- it can classify missing LLM configuration,
- it can mark a failure as transient and retryable,
- it must not invent credentials, node addresses or execute side effects.

For the common document-sync case:

```json
{
  "error": {
    "message": "node_url is required when the target node is not present in host config",
    "uri": "document://host/archive/command/sync-to-node"
  }
}
```

with `node_urls=["lenovo=http://192.168.188.201:8766"]`, `repair_chain` returns
a retry contract for:

```text
document://host/archive/command/sync-to-node
```

with `node_url` added to the payload. If the URL is not known, it returns
`provide-node-url` instead of guessing.

## Development note

When testing from the monorepo root, Python can import the top-level `urirun/`
directory instead of the package in `urirun/adapters/python/urirun`. Use:

```bash
cd /tmp
urirun discover --out /tmp/urifix.bindings.json
```

or set:

```bash
export PYTHONPATH=/home/tom/github/if-uri/urirun/adapters/python
```
