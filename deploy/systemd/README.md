# Ubuntu systemd deployment

This layout runs both processes privately on the same server:

- web console: `127.0.0.1:8787`
- self-hosted MCP publisher: `127.0.0.1:8788`
- Nginx is the only public entry point

Configure the web process with `MCP_URL=http://127.0.0.1:8788/mcp`. The MCP port
does not need to be exposed publicly.

## 1. Install system packages and clone

```bash
sudo apt-get update
sudo apt-get install -y git nginx python3 python3-venv python3-pip
sudo useradd --system --home /opt/bn-square-agent --shell /usr/sbin/nologin bn-square || true
sudo git clone https://github.com/CarrollWang/bn_square_agent.git /opt/bn-square-agent
sudo chown -R bn-square:bn-square /opt/bn-square-agent
```

## 2. Install Python and browser dependencies

```bash
sudo -u bn-square python3 -m venv /opt/bn-square-agent/.venv
sudo -u bn-square /opt/bn-square-agent/.venv/bin/pip install -r /opt/bn-square-agent/requirements.txt
sudo -u bn-square env PLAYWRIGHT_BROWSERS_PATH=/opt/bn-square-agent/ms-playwright \
  /opt/bn-square-agent/.venv/bin/python -m playwright install chromium
sudo /opt/bn-square-agent/.venv/bin/python -m playwright install-deps chromium
sudo install -d -o bn-square -g bn-square /opt/bn-square-agent/data
sudo install -d -o bn-square -g bn-square /opt/bn-square-agent/chroma_db
```

The repository contains a built frontend under `dist/`. To rebuild on the server,
install Node.js and run `npm ci && npm run build` inside `web/`.

## 3. Create the protected environment file

```bash
sudo install -d -m 750 -o root -g bn-square /etc/bn-square-agent
sudo cp /opt/bn-square-agent/deploy/systemd/env.example /etc/bn-square-agent/env
sudo chown root:bn-square /etc/bn-square-agent/env
sudo chmod 600 /etc/bn-square-agent/env
sudo editor /etc/bn-square-agent/env
```

Generate the shared MCP token with `openssl rand -hex 32`. Use the same value for
`MCP_SERVER_AUTH_TOKEN` and `MCP_AUTH_TOKEN`.

When using GLM Coding Plan, use the subscription-specific API key and the OpenAI
compatible Coding Plan endpoint:

```text
LLM_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4
```

The ordinary MaaS endpoint `/api/paas/v4` uses the standard account balance and
does not consume Coding Plan quota.

## 4. Install and start services

```bash
sudo cp /opt/bn-square-agent/deploy/systemd/bn-square-agent-web.service /etc/systemd/system/
sudo cp /opt/bn-square-agent/deploy/systemd/bn-square-agent-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bn-square-agent-web bn-square-agent-mcp
sudo systemctl status bn-square-agent-web bn-square-agent-mcp --no-pager
```

## 5. Verify locally on the server

```bash
curl -fsS http://127.0.0.1:8787/ >/dev/null
curl -fsS -X POST http://127.0.0.1:8788/mcp \
  -H 'content-type: application/json' \
  -H "authorization: Bearer ${MCP_SERVER_AUTH_TOKEN}" \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"server-check","version":"1"}}}'
```

## 6. Public access

Use `deploy/nginx/bn-square-agent-web.conf.example`. Keep ports 8787 and 8788
closed in the cloud security group. Expose only 80/443 and protect the web console
with HTTPS plus Basic Auth.

## 7. Operations and backup

```bash
sudo journalctl -u bn-square-agent-web -n 100 --no-pager
sudo journalctl -u bn-square-agent-mcp -n 100 --no-pager
sudo systemctl restart bn-square-agent-web bn-square-agent-mcp
```

Back up these files together:

- `/opt/bn-square-agent/data/bn_square.db`
- `/opt/bn-square-agent/data/app_secret.key`

Losing the secret key makes encrypted cookies and API keys unrecoverable.