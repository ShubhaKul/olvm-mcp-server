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

In operator mode, three write tools are added:

| Tool | What it does |
|---|---|
| `start_vm(vm_name_or_id, dry_run, wait, timeout_seconds)` | Powers on a VM and waits until it is up |
| `shutdown_vm(vm_name_or_id, dry_run, wait, timeout_seconds)` | Graceful shutdown through the guest OS (not a power off), waits until down |
| `create_snapshot(vm_name_or_id, description, include_memory, dry_run, wait, timeout_seconds)` | Snapshots a VM's disks, optionally with memory, and waits until it is ready |
| `migrate_vm(vm_name_or_id, target_host, dry_run, wait, timeout_seconds)` | Live-migrates a running VM to another host in its cluster (or one the engine picks) and waits until it runs there |

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

See [.env.example](.env.example).

## Operator mode (write actions)

Set `OLVM_MODE=operator` and `OLVM_ALLOWED_CLUSTERS` to turn on `start_vm`, `shutdown_vm`, `create_snapshot` and `migrate_vm`. Without them the write tools aren't registered at all.

Every write tool:

1. **Checks the mode and the cluster allow-list.** A VM in a cluster that isn't listed is refused, and the refusal is audited.
2. **Supports `dry_run=true`**, which describes the change without making it. The server instructions tell the assistant to dry-run first and confirm with you.
3. **Writes an audit record before it acts**, and refuses to run if the audit log can't be written. A second record holds the outcome.
4. **Sends a correlation id** (`olvm-mcp-…`) as the engine's `Correlation-Id` header. The same id appears in the audit log and on the engine's events, so `list_events` can show what the engine did.
5. **Waits for the result**: the VM reaching `up` or `down`, or the snapshot reaching `ok`. If that takes longer than `timeout_seconds`, the result is `pending` rather than an error.

Asking to start a VM that is already up, or to shut down one that is already down, returns `no_change` without calling the engine.

The audit log is JSON Lines, one object per line:

```json
{"time": "2026-09-30T10:15:02+00:00", "user": "admin@ovirt@internalsso", "action": "start_vm", "vm": "vm-test", "vm_id": "668ea110-…", "cluster": "Default", "correlation_id": "olvm-mcp-3f2a9c1b7d4e", "params": {}, "outcome": "requested"}
{"time": "2026-09-30T10:15:41+00:00", "user": "admin@ovirt@internalsso", "action": "start_vm", "vm": "vm-test", "vm_id": "668ea110-…", "cluster": "Default", "correlation_id": "olvm-mcp-3f2a9c1b7d4e", "outcome": "done", "status": "up"}
```

Outcomes are `requested`, `done`, `pending`, `submitted`, `failed` and `denied`.

**Which account to use.** Operator mode needs an engine user that can start, stop and snapshot VMs. A user with a role scoped to the allowed clusters (for example UserVmManager) keeps the engine as a second line of defence. With an admin account, the allow-list and the audit log are the only limits, so keep `OLVM_ALLOWED_CLUSTERS` narrow and use a password file.

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

## Troubleshooting

| Error | Cause |
|---|---|
| `Login to the engine failed` | Wrong username format or password, or the password is still marked Temporary in Keycloak |
| `HTML error page ... no permissions` | The user has no role in OLVM. Assign ReadOnlyAdmin (system-wide) |
| `CERTIFICATE_VERIFY_FAILED` | Set `OLVM_CA_FILE` to the engine's CA certificate |
| `Cannot reach the engine` | Network, tunnel or hosts-file problem. Check `https://<fqdn>/ovirt-engine/services/health` in a browser |

## Development

```text
src/olvm_mcp/
  config.py      settings from environment variables
  client.py      REST client: SSO login, token refresh, errors
  formatting.py  compact summaries of engine JSON
  server.py      MCP server and tools
  safety.py      audit log and correlation ids for write actions
  actions.py     write tools, registered only in operator mode
tests/           unit tests against a mocked engine (respx)
scripts/         smoke test against a real engine
```

Logs go to stderr, because stdout carries the MCP protocol.

## Roadmap

- **Phase 2:** operator actions with dry-run, confirmation and an audit log. Done so far: modes, cluster allow-list, audit log, `start_vm`, `shutdown_vm`, `create_snapshot`, `migrate_vm`. Next: host maintenance, and destructive actions (power off, remove, restore) behind confirmation tokens
- **Phase 3:** Streamable HTTP transport with authentication, for remote clients
- **Phase 4:** agents built on top (triage, capacity reports, provisioning)

## License

[Apache License 2.0](LICENSE)
