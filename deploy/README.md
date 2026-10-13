# Deploying olvm-mcp with Docker

This runs the server as two containers on a Linux VM:

```text
MCP client ──HTTPS :443──▶ caddy (TLS) ──HTTP, internal network──▶ olvm-mcp :8000 ──HTTPS──▶ OLVM engine
```

- **olvm-mcp** serves MCP over Streamable HTTP and needs a bearer token on every request. It isn't published on the host.
- **caddy** terminates TLS on port 443. By default it uses its **own private CA** to issue a certificate for the address clients use, such as the VM's IP. Clients must trust that CA's root certificate (step 6). With a DNS name, Caddy can use a public Let's Encrypt certificate instead; see [Using a DNS name](#using-a-dns-name).

The VM must reach the engine on port 443, ideally inside the same private network. These steps were written for Oracle Linux 8 on x86_64.

## 1. Install Docker

```bash
sudo dnf install -y dnf-utils git
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER     # then log out and back in
docker compose version
```

## 2. Open port 443

On the VM:

```bash
sudo firewall-cmd --permanent --add-service=https && sudo firewall-cmd --reload
```

In the cloud network (for OCI, the subnet's security list or the VM's network security group), allow **TCP 443 only from the addresses of your clients**, for example `<your-ip>/32`. Nothing else needs to be open.

## 3. Get the code and build the image

```bash
git clone https://github.com/ShubhaKul/olvm-mcp-server.git
cd olvm-mcp-server/deploy
echo "PUBLIC_HOST=<public-ip>" > .env      # the address clients will use
docker compose build
```

## 4. Configuration files

Everything secret lives in `/etc/olvm-mcp` on the VM and is mounted read-only into the container:

| File | Contents |
|---|---|
| `olvm-mcp.env` | Settings, from [olvm-mcp.env.example](olvm-mcp.env.example) |
| `engine-ca.pem` | The engine's CA certificate |
| `engine.pw` | The engine account's password, alone on one line |
| `tokens` | One `name sha256:<hash>` line per client |

```bash
sudo mkdir -p /etc/olvm-mcp
sudo cp olvm-mcp.env.example /etc/olvm-mcp/olvm-mcp.env
sudo vi /etc/olvm-mcp/olvm-mcp.env        # engine URL, account, OLVM_PUBLIC_URL=https://<public-ip>

# The engine's CA certificate, fetched from the engine itself:
curl -k -o /tmp/engine-ca.pem "https://<engine-fqdn>/ovirt-engine/services/pki-resource?resource=ca-certificate&format=X509-PEM-CA"
sudo mv /tmp/engine-ca.pem /etc/olvm-mcp/engine-ca.pem

# The engine account's password (typed, not echoed):
read -rsp "Engine password: " PW; echo; printf '%s\n' "$PW" | sudo tee /etc/olvm-mcp/engine.pw >/dev/null; unset PW
```

Create a token for each client with the image you just built. The token is printed once; only its hash goes in the file:

```bash
docker run --rm --entrypoint olvm-mcp-token olvm-mcp:local alice
# copy the "alice sha256:..." line into the tokens file:
sudo vi /etc/olvm-mcp/tokens
```

The container runs as user id 10001. The password and tokens files are readable only by root and that group. `olvm-mcp.env` holds no secrets, only settings and file paths, and must stay readable by you, because `docker compose` reads it as the user running it:

```bash
sudo chown -R root:10001 /etc/olvm-mcp
sudo sh -c 'chmod 755 /etc/olvm-mcp && chmod 640 /etc/olvm-mcp/* && chmod 644 /etc/olvm-mcp/olvm-mcp.env'
```

Never put a password in `olvm-mcp.env` (use `OLVM_PASSWORD_FILE`).

**Engine account:** use a dedicated account with a role scoped to the clusters in `OLVM_ALLOWED_CLUSTERS`, never `admin`. For remote use, keep `OLVM_ALLOW_DESTRUCTIVE` off.

## 5. Start

```bash
docker compose up -d
docker compose logs olvm-mcp     # expect "Serving MCP over HTTP ... N client token(s)"
```

If the server can't start, for example because the tokens file is missing or `OLVM_PUBLIC_URL` is invalid, the log says why and the container restarts until it's fixed.

## 6. Trust Caddy's root certificate on the clients

Copy the root certificate of Caddy's private CA off the VM:

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./olvm-mcp-root.crt
```

Then, on each client machine:
- **Claude Code** (Node.js): set `NODE_EXTRA_CA_CERTS` to the file before starting it.
- **Other clients**, and command-line checks on Windows: import it into your user's trusted roots, `certutil -user -addstore Root olvm-mcp-root.crt`. Remove it with `certutil -user -delstore Root <name>` when you take the server down. This CA can only vouch for this server, but it's still a trusted root on that machine.

The root stays the same as long as the `caddy-data` volume exists. If you delete that volume, Caddy creates a new CA and clients must trust the new root.

## 7. Connect and check

Without a token the server must answer **401**:

```bash
curl --cacert olvm-mcp-root.crt -i https://<public-ip>/mcp
```

Claude Code:

```bash
claude mcp add --transport http olvm https://<public-ip>/mcp --header "Authorization: Bearer <token>"
```

Codex CLI (`~/.codex/config.toml`, token in an environment variable):

```toml
[mcp_servers.olvm]
url = "https://<public-ip>/mcp"
bearer_token_env_var = "OLVM_MCP_TOKEN"
```

## Operating it

| Task | Command |
|---|---|
| Logs | `docker compose logs -f olvm-mcp` |
| Audit log | `docker compose exec olvm-mcp tail -n 20 /var/lib/olvm-mcp/audit.jsonl` |
| Add or revoke a client | edit `/etc/olvm-mcp/tokens`, then `docker compose restart olvm-mcp` |
| Update | `git pull && docker compose up -d --build` |
| Stop | `docker compose down` (volumes, and so the CA and audit log, are kept) |

## Using a DNS name

With a DNS name pointing at the VM, Caddy can get a certificate from Let's Encrypt that every client already trusts. ChatGPT and claude.ai connectors need this.

1. Set `PUBLIC_HOST=mcp.example.com` in `deploy/.env` and `OLVM_PUBLIC_URL=https://mcp.example.com` in the env file.
2. Remove the `tls internal` line from the `Caddyfile`.
3. Open TCP **80** as well as 443 in the cloud network and in `firewalld`; Let's Encrypt checks that you control the name over port 80. Then publish port 80 on the `caddy` service in `docker-compose.yml`.
