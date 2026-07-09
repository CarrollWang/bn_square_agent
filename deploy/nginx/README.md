# Nginx Deployment Notes

## Recommended setup

For a self-use public server, the safer layout is:

- `python -B run.py serve --host 127.0.0.1 --port 8787`
- `python -B run.py serve-mcp --host 127.0.0.1 --port 8788`
- Nginx exposes only the web console on `443`
- The web app uses `MCP_URL=http://127.0.0.1:8788/mcp`

This means the browser can reach only Nginx, while the MCP server stays private on localhost.

## Files

- `bn-square-agent-web.conf.example`: public web console with HTTPS and Basic Auth
- `bn-square-agent-mcp.conf.example`: optional public MCP reverse proxy

## Create the Basic Auth password file

On Debian/Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd_bn_square_agent admin
```

If you prefer `openssl`:

```bash
printf "admin:$(openssl passwd -apr1 'your-strong-password')\n" | sudo tee /etc/nginx/.htpasswd_bn_square_agent >/dev/null
```

## Enable the site

```bash
sudo cp deploy/nginx/bn-square-agent-web.conf.example /etc/nginx/sites-available/bn-square-agent.conf
sudo ln -s /etc/nginx/sites-available/bn-square-agent.conf /etc/nginx/sites-enabled/bn-square-agent.conf
sudo nginx -t
sudo systemctl reload nginx
```

## Firewall

If you use Nginx, keep these app ports private:

- do not expose `8787`
- do not expose `8788` unless you really need public MCP
- expose only `80/443`

## MCP exposure

If the MCP server is only used by this same app instance, do not publish it through Nginx.

Use:

```text
MCP_URL=http://127.0.0.1:8788/mcp
```

If you must expose MCP separately, use `bn-square-agent-mcp.conf.example`, keep `MCP_SERVER_AUTH_TOKEN` enabled, and add an IP allowlist.
