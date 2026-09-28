# olvm-mcp

An [MCP](https://modelcontextprotocol.io) server for **Oracle Linux Virtualization Manager (OLVM)** and **oVirt**. It lets AI assistants such as Claude read your virtualization inventory through the engine's REST API.

Tested against OLVM 4.5.5. This is Phase 1: **read-only**.

## Tools

| Tool | What it returns |
|---|---|
| `list_vms(search, max_results)` | VMs with status, cluster, host, CPUs, memory, OS |
| `get_vm(name_or_id)` | One VM in detail, including disks and network interfaces |
| `list_hosts(search, max_results)` | KVM hosts with status, cluster, CPU, memory, running VMs, OS and VDSM version |

`search` accepts the engine's search syntax, for example `status=up`, `name=web*` or `cluster=Default and status=down`.

Every tool is marked read-only (`readOnlyHint`), and results are capped at 200 items.

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

See [.env.example](.env.example).

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
tests/           unit tests against a mocked engine (respx)
scripts/         smoke test against a real engine
```

Logs go to stderr, because stdout carries the MCP protocol.

## Roadmap

- **Phase 2:** operator actions (start/stop, snapshots, migration, host maintenance) with dry-run, confirmation and an audit log
- **Phase 3:** Streamable HTTP transport with authentication, for remote clients
- **Phase 4:** agents built on top (triage, capacity reports, provisioning)
