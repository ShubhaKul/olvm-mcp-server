# olvm-mcp

[![tests](https://github.com/ShubhaKul/olvm-mcp-server/actions/workflows/tests.yml/badge.svg)](https://github.com/ShubhaKul/olvm-mcp-server/actions/workflows/tests.yml)

An [MCP](https://modelcontextprotocol.io) server for **Oracle Linux Virtualization Manager (OLVM)** and **oVirt**. It lets AI assistants such as Claude read your virtualization inventory through the engine's REST API.

Tested against OLVM 4.5.5. It is **read-only by default**. In [operator mode](#operator-mode-write-actions) it can also start and shut down VMs and take snapshots, behind dry-run, a cluster allow-list and an audit log.

## Tools

| Tool | What it returns |
|---|---|
| `list_vms(search, max_results)` | VMs with status, cluster, host, CPUs, memory, OS |
| `get_vm(name_or_id)` | One VM in detail, including disks and network interfaces |
| `list_hosts(search, max_results)` | KVM hosts with status, cluster, CPU, memory, running VMs, OS and VDSM version |
| `list_storage_domains(search, max_results)` | Storage domains with type, status, capacity, free space and a low-space flag |
| `list_snapshots(vm_name_or_id)` | One VM's snapshots with date, status and whether memory was saved |
| `list_events(search, max_results)` | Engine events (audit log), newest first, e.g. `severity=error` |
| `get_job_status(job_id, max_results)` | One engine job with its steps, or recent jobs when no id is given |

`search` accepts the engine's search syntax, for example `status=up`, `name=web*` or `cluster=Default and status=down`.

Every tool above is marked read-only (`readOnlyHint`), and results are capped at 200 items.

In operator mode, five write tools are added:

| Tool | What it does |
|---|---|
| `start_vm(vm_name_or_id, dry_run, wait, timeout_seconds)` | Powers on a VM and waits until it is up |
| `shutdown_vm(vm_name_or_id, dry_run, wait, timeout_seconds)` | Graceful shutdown through the guest OS (not a power off), waits until down |
| `create_snapshot(vm_name_or_id, description, include_memory, dry_run, wait, timeout_seconds)` | Snapshots a VM's disks, optionally with memory, and waits until it is ready |
| `migrate_vm(vm_name_or_id, target_host, dry_run, wait, timeout_seconds)` | Live-migrates a running VM to another host in its cluster (or one the engine picks) and waits until it runs there |
| `set_host_maintenance(host_name_or_id, maintenance, dry_run, wait, timeout_seconds)` | Puts a host into maintenance (the engine live-migrates its VMs away) or activates it again; the dry run lists the VMs that would move |

With `OLVM_ALLOW_DESTRUCTIVE=true` as well, three destructive tools are added. They need a [confirmation token](#destructive-actions):

| Tool | What it does |
|---|---|
| `stop_vm(vm_name_or_id, confirm_token, wait, timeout_seconds)` | Powers a VM off immediately, without a guest shutdown |
| `restore_snapshot(vm_name_or_id, snapshot, restore_memory, confirm_token, wait, timeout_seconds)` | Restores a stopped VM to a snapshot; snapshots taken after it are deleted |
| `remove_vm(vm_name_or_id, remove_disks, confirm_token, wait, timeout_seconds)` | Permanently removes a stopped VM, with or without its disks; refuses delete-protected VMs |

## How it works

- Logs in through the engine's SSO token endpoint (`/ovirt-engine/sso/oauth/token`, scope `ovirt-app-api`). The official oVirt SDK uses the same flow, so Keycloak and older logins both work.
- Calls the REST API v4 in JSON over HTTPS with [httpx](https://www.python-httpx.org/). The official `ovirt-engine-sdk-python` isn't used because it has no Windows build.
- Verifies TLS with the engine's CA certificate.
- Refreshes the token automatically when it expires.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Network access to the engine on port 443 (directly, or through an SSH tunnel)
- An engine user with a read-only role. Don't use `admin`.

## Setup

### 1. Create a read-only engine user

For Keycloak installs (the default on OLVM 4.5):
1. In the Keycloak admin console (`https://<engine>/ovirt-engine-auth/admin/`, realm **ovirt-internal**), add a user named `mcp-reader@ovirt`. Set a password with **Temporary** turned off.
2. In the Administration Portal, go to **Administration → Users → Add** and add the user (provider `internalkeycloak-authz`).
3. Go to **Administration → Configure → System Permissions → Add** and assign **ReadOnlyAdmin**.

The API username is then `mcp-reader@ovirt@internalsso`.

### 2. Get the engine's CA certificate

```bash
curl -k -o olvm-ca.pem "https://<engine-fqdn>/ovirt-engine/services/pki-resource?resource=ca-certificate&format=X509-PEM-CA"
```

### 3. Install and test

```bash
uv sync
uv run pytest
```

Then run the smoke test against your engine. It prompts for the password:

```bash
# PowerShell
$env:OLVM_URL = "https://<engine-fqdn>/ovirt-engine"
$env:OLVM_USERNAME = "mcp-reader@ovirt@internalsso"
$env:OLVM_CA_FILE = "C:\path\to\olvm-ca.pem"
uv run python scripts/smoke_test.py vm-test
```

## Configuration

| Variable | Required | Description |
|---|---|---|
| `OLVM_URL` | yes | Engine base URL, e.g. `https://engine.example.com/ovirt-engine` (`/api` optional) |
| `OLVM_USERNAME` | yes | e.g. `mcp-reader@ovirt@internalsso` (Keycloak) or `user@internal` |
| `OLVM_PASSWORD_FILE` | one of | File containing only the password (recommended) |
| `OLVM_PASSWORD` | one of | The password itself |
| `OLVM_CA_FILE` | recommended | Engine CA certificate (PEM). Without it, the system trust store is used |
| `OLVM_TIMEOUT` | no | Seconds per request (default 30) |
| `OLVM_INSECURE` | no | `true` disables TLS verification. Lab use only |
| `OLVM_MODE` | no | `read_only` (default) or `operator`, which adds the write tools |
| `OLVM_ALLOWED_CLUSTERS` | in operator mode | Comma-separated cluster names that write tools may touch, or `*` for all |
| `OLVM_AUDIT_LOG` | no | Audit log file (default `~/.olvm-mcp/audit.jsonl`) |
| `OLVM_ALLOW_DESTRUCTIVE` | no | `true` adds `stop_vm`, `restore_snapshot` and `remove_vm`. Needs `OLVM_MODE=operator` |
| `OLVM_TRANSPORT` | no | `stdio` (default) or `http`. See [Remote access](#remote-access-http) |
| `OLVM_AUTH_TOKENS_FILE` | for `http` | File of client names and token hashes. The server refuses to run over HTTP without it |
| `OLVM_HTTP_HOST` / `OLVM_HTTP_PORT` | no | Address to listen on (default `127.0.0.1:8000`) |
| `OLVM_PUBLIC_URL` | for `http` behind a proxy | The base URL clients use, e.g. `https://mcp.example.com`. Must be `https` unless it is localhost |
| `OLVM_HTTP_ALLOWED_HOSTS` | no | Extra `Host` header values to accept, comma-separated |

See [.env.example](.env.example).

## Operator mode (write actions)

Set `OLVM_MODE=operator` and `OLVM_ALLOWED_CLUSTERS` to turn on `start_vm`, `shutdown_vm`, `create_snapshot`, `migrate_vm` and `set_host_maintenance`. Without them the write tools aren't registered at all.

Every write tool:

1. **Checks the mode and the cluster allow-list.** A VM in a cluster that isn't listed is refused, and the refusal is audited.
2. **Supports `dry_run=true`**, which describes the change without making it. The server instructions tell the assistant to dry-run first and confirm with you.
3. **Writes an audit record before it acts**, and refuses to run if the audit log can't be written. A second record holds the outcome.
4. **Sends a correlation id** (`olvm-mcp-…`) as the engine's `Correlation-Id` header. The same id appears in the audit log and on the engine's events, so `list_events` can show what the engine did.
5. **Waits for the result**: the VM reaching `up` or `down`, or the snapshot reaching `ok`. If that takes longer than `timeout_seconds`, the result is `pending` rather than an error.

Asking to start a VM that is already up, or to shut down one that is already down, returns `no_change` without calling the engine.

The audit log is JSON Lines, one object per line:

```json
{"time": "2026-10-13T10:15:02+00:00", "user": "mcp-operator@internalsso", "client": "shubha-laptop", "action": "start_vm", "vm": "vm-test", "vm_id": "668ea110-…", "cluster": "Default", "correlation_id": "olvm-mcp-3f2a9c1b7d4e", "params": {}, "outcome": "requested"}
{"time": "2026-10-13T10:15:41+00:00", "user": "mcp-operator@internalsso", "client": "shubha-laptop", "action": "start_vm", "vm": "vm-test", "vm_id": "668ea110-…", "cluster": "Default", "correlation_id": "olvm-mcp-3f2a9c1b7d4e", "outcome": "done", "status": "up"}
```

- `user` is the **engine account** the server acts as. It's the same for every client of a server.
- `client` is **who asked**. Over HTTP it's the name of the bearer token that authenticated the request (`olvm-mcp-token <name>`). Over stdio there is no authentication, so it's the operating-system user running the server, as `local:<user>`.
- `correlation_id` also appears on the engine's own events. The engine records only the shared account, so joining the two by correlation id shows who really made each change.

Outcomes are `requested`, `done`, `pending`, `submitted`, `failed` and `denied`. Refused attempts (`denied`) also carry `client`, although the engine never sees them.

**Which account to use.** Operator mode needs an engine user that can start, stop and snapshot VMs. A user with a role scoped to the allowed clusters (for example UserVmManager) keeps the engine as a second line of defence. With an admin account, the allow-list and the audit log are the only limits, so keep `OLVM_ALLOWED_CLUSTERS` narrow and use a password file.

### Destructive actions

`stop_vm`, `restore_snapshot` and `remove_vm` can lose data or can't be undone, so they need a second opt-in (`OLVM_ALLOW_DESTRUCTIVE=true`) and work in two steps instead of offering `dry_run`:

1. **Preview.** A call without `confirm_token` changes nothing. It returns what would happen (for a restore, which newer snapshots would be deleted; for a removal, which disks) and a one-time `confirm_token`.
2. **Confirm.** Only a second call with that token runs the action, after the assistant has shown you the preview and you have agreed.

The server enforces the token. It works **once**, for **5 minutes**, only for the **same VM and arguments**, and only while the VM is **unchanged** since the preview (status, disks or snapshots). Anything else is refused and audited as `denied`, and the assistant has to ask for a new preview. Previews themselves aren't audited, because they change nothing.

Two more rules: snapshots are restored and VMs removed only while the VM is down, and VMs with delete protection are refused. The server never turns delete protection off.

The token proves that a preview was produced, not that a person read it. The server instructions tell the assistant to ask you before confirming, and MCP clients ask for approval before tools marked destructive, so keep that approval prompt turned on for these tools.

Run operator mode as a **separate** MCP server entry next to the read-only one, so you can turn write access on and off independently:

```json
"olvm-operator": {
  "command": "uv",
  "args": ["--directory", "C:\\path\\to\\olvm-mcp-server-repo", "run", "olvm-mcp"],
  "env": {
    "OLVM_URL": "https://<engine-fqdn>/ovirt-engine",
    "OLVM_USERNAME": "admin@ovirt@internalsso",
    "OLVM_PASSWORD_FILE": "C:\\path\\to\\admin.pw",
    "OLVM_CA_FILE": "C:\\path\\to\\olvm-ca.pem",
    "OLVM_MODE": "operator",
    "OLVM_ALLOWED_CLUSTERS": "Default"
  }
}
```

## Connect an MCP client

### Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "olvm": {
      "command": "uv",
      "args": ["--directory", "C:\\path\\to\\olvm-mcp-server-repo", "run", "olvm-mcp"],
      "env": {
        "OLVM_URL": "https://<engine-fqdn>/ovirt-engine",
        "OLVM_USERNAME": "mcp-reader@ovirt@internalsso",
        "OLVM_PASSWORD_FILE": "C:\\path\\to\\mcp-reader.pw",
        "OLVM_CA_FILE": "C:\\path\\to\\olvm-ca.pem"
      }
    }
  }
}
```

Restart Claude Desktop, then ask, for example: *"Which VMs are running, and on which hosts?"*

### Claude Code

```bash
claude mcp add olvm -e OLVM_URL=https://<engine-fqdn>/ovirt-engine -e OLVM_USERNAME=mcp-reader@ovirt@internalsso -e OLVM_PASSWORD_FILE=/path/to/mcp-reader.pw -e OLVM_CA_FILE=/path/to/olvm-ca.pem -- uv --directory /path/to/olvm-mcp-server-repo run olvm-mcp
```

### Engine reachable only through SSH

Tunnel port 443 and map the engine's FQDN to `127.0.0.1` in your hosts file. The engine's login only works with the FQDN given to `engine-setup`:

```bash
ssh -i <key> -L 443:localhost:443 opc@<engine-public-ip>
```

## Remote access (HTTP)

With `OLVM_TRANSPORT=http` the server runs as a long-lived service that clients reach over the network, using MCP's Streamable HTTP transport at `/mcp`. Every request needs a bearer token; the server refuses to start in HTTP mode without a tokens file.

**1. Create a token for each client** (a person or an app). The token is printed once; the server keeps only its SHA-256 hash:

```bash
uv run olvm-mcp-token alice
```

```text
Token for alice (give it to the client; it is shown only once):
  q1Xb...

Line for OLVM_AUTH_TOKENS_FILE:
  alice sha256:5e88...
```

Put the `alice sha256:…` lines in the tokens file, one per client, and keep the file readable only by the service. To revoke a client, delete its line and restart the server.

**2. Run the server** with the engine settings from [Configuration](#configuration) plus:

```bash
OLVM_TRANSPORT=http
OLVM_AUTH_TOKENS_FILE=/etc/olvm-mcp/tokens
OLVM_PUBLIC_URL=https://mcp.example.com     # what clients use
```

It listens on `127.0.0.1:8000` by default. Put a TLS proxy (Caddy, nginx) in front of it, and don't expose the plain-HTTP port. **[deploy/](deploy/README.md) has a ready-made Docker setup**: the server plus Caddy, with TLS from Caddy's private CA or Let's Encrypt. The server checks the `Host` header against `OLVM_PUBLIC_URL` to block DNS-rebinding attacks.

**3. Connect a client** with the URL `https://mcp.example.com/mcp` and the header `Authorization: Bearer <token>`:

```bash
# Claude Code
claude mcp add --transport http olvm https://mcp.example.com/mcp --header "Authorization: Bearer <token>"
```

```toml
# Codex CLI: ~/.codex/config.toml (token read from an environment variable)
[mcp_servers.olvm]
url = "https://mcp.example.com/mcp"
bearer_token_env_var = "OLVM_MCP_TOKEN"
```

ChatGPT and claude.ai custom connectors expect OAuth, which is planned (see the roadmap). Until then, use them with Claude Code, Codex or other clients that can send a header.

**What the token does and doesn't control.** It decides who may use the server. What the server may do on the engine is still set by its engine account, `OLVM_MODE`, `OLVM_ALLOWED_CLUSTERS` and `OLVM_ALLOW_DESTRUCTIVE`, the same for every client of that server. Keep remote servers read-only or operator, and leave destructive actions off until confirmations can come from the user directly.

## Troubleshooting

| Error | Cause |
|---|---|
| `Login to the engine failed` | Wrong username format or password, or the password is still marked Temporary in Keycloak |
| `HTML error page ... no permissions` | The user has no role in OLVM. Assign ReadOnlyAdmin (system-wide) |
| `CERTIFICATE_VERIFY_FAILED` | Set `OLVM_CA_FILE` to the engine's CA certificate |
| `Cannot reach the engine` | Network, tunnel or hosts-file problem. Check `https://<fqdn>/ovirt-engine/services/health` in a browser |
| HTTP 401 from `/mcp` | Missing or unknown bearer token. Check the client's `Authorization` header and the tokens file |
| HTTP 421 or 403 from `/mcp` | The `Host` header doesn't match `OLVM_PUBLIC_URL`. Fix the URL, or add the host to `OLVM_HTTP_ALLOWED_HOSTS` |

## Development

```text
src/olvm_mcp/
  config.py      settings from environment variables
  client.py      REST client: SSO login, token refresh, errors
  formatting.py  compact summaries of engine JSON
  server.py      MCP server and tools
  safety.py      audit log and correlation ids for write actions
  actions.py     write tools, registered only in operator mode
  remote.py      HTTP transport settings, bearer tokens, olvm-mcp-token
tests/           unit tests against a mocked engine (respx)
deploy/          Docker Compose deployment with Caddy (TLS)
scripts/         smoke test against a real engine
```

Logs go to stderr, because stdout carries the MCP protocol.

## Roadmap

- **Phase 2 (done):** operator actions with dry run, confirmation tokens and an audit log: modes, cluster allow-list, `start_vm`, `shutdown_vm`, `create_snapshot`, `migrate_vm`, `set_host_maintenance`, and the destructive `stop_vm`, `restore_snapshot` and `remove_vm`
- **Phase 3 (in progress):** remote access. Done: Streamable HTTP with bearer-token authentication, Docker image and deployment, per-user audit records (`client` field). Next: OAuth via Keycloak (ChatGPT and claude.ai connectors), MCP elicitation for destructive actions
- **Phase 4:** agents built on top (triage, capacity reports, provisioning)

## License

[Apache License 2.0](LICENSE)
