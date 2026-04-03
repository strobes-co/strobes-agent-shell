# Strobes Shell Agent

A lightweight daemon that connects your machine to the [Strobes](https://strobes.co) platform, enabling AI agents to execute commands remotely — without SSH, firewall rules, or inbound ports.

Think of it as "Local Browser" but for shell access: the agent runs on your machine, connects **outbound** to Strobes via WebSocket, and the AI agent sends commands through that tunnel.

```
┌──────────────────────┐          WebSocket          ┌──────────────────────┐
│   Strobes Platform   │◄──── outbound connection ───│  Your Machine        │
│                      │                              │                      │
│  AI Agent calls      │   ── shell_execute ───►      │  strobes-shell-agent │
│  workspace_execute   │   ◄── stdout/stderr ──       │  (this daemon)       │
│  _shell_command()    │                              │                      │
└──────────────────────┘                              └──────────────────────┘
```

## Why?

| | SSH Shell | Shell Agent (Bridge) |
|---|---|---|
| **Setup** | Need hostname, port, SSH keys, firewall rules | Just run the daemon |
| **Network** | Platform connects _to_ your machine (inbound) | Daemon connects _to_ platform (outbound) |
| **Credentials** | SSH keys stored on platform | API key only |
| **Firewall** | Port 22 must be open inbound | Only outbound HTTPS needed |
| **NAT/VPN** | Needs port forwarding or VPN | Works behind NAT, VPN, anything |

## Quick Start

### 1. Create a Bridge Shell in Strobes

Go to **AI > Shells > Create Shell**, select **Bridge** type, and note the `bridge_id`.

### 2. Get your API Key

Go to **Settings > API Keys** and copy your key.

### 3. Run the agent

**Option A: Pre-built binary (recommended, no Python needed)**

Download from [Releases](https://github.com/strobes-co/strobes-agent-shell/releases):

```bash
# Linux
curl -L -o strobes-shell-agent https://github.com/strobes-co/strobes-agent-shell/releases/latest/download/strobes-shell-agent-linux-amd64
chmod +x strobes-shell-agent

# macOS (Apple Silicon)
curl -L -o strobes-shell-agent https://github.com/strobes-co/strobes-agent-shell/releases/latest/download/strobes-shell-agent-macos-arm64
chmod +x strobes-shell-agent

# Run
./strobes-shell-agent connect \
  --url https://app.strobes.co \
  --api-key sk-your-api-key \
  --org-id your-org-uuid \
  --bridge-id your-bridge-id \
  --name "my-server"
```

**Option B: Using .env file**

```bash
# Download the binary (see above), then:
cat > .env << EOF
STROBES_URL=https://app.strobes.co
STROBES_API_KEY=sk-your-api-key
STROBES_ORG_ID=your-org-uuid
STROBES_BRIDGE_ID=your-bridge-id
STROBES_SHELL_NAME=my-server
EOF

./strobes-shell-agent connect
```

**Option C: Docker**

```bash
cat > .env << EOF
STROBES_URL=https://app.strobes.co
STROBES_API_KEY=sk-your-api-key
STROBES_ORG_ID=your-org-uuid
STROBES_BRIDGE_ID=your-bridge-id
STROBES_SHELL_NAME=my-server
EOF

docker run --rm --env-file .env ghcr.io/strobes-co/strobes-agent-shell:latest connect
```

**Option D: Docker Compose**

```bash
cp .env.example .env
# Edit .env with your values
docker compose up -d
```

**Option E: From source (development)**

```bash
git clone https://github.com/strobes-co/strobes-agent-shell.git
cd strobes-agent-shell
pip install .
strobes-shell-agent connect --url https://app.strobes.co --api-key sk-xxx --org-id xxx
```

### 4. Attach to a Workspace

In Strobes, go to your workspace settings and attach the bridge shell. All AI agent code execution in that workspace now routes through your machine.

## Configuration

All options can be set via CLI flags, environment variables, or a `.env` file.

| CLI Flag | Env Variable | Required | Description |
|---|---|---|---|
| `--url` | `STROBES_URL` | Yes | Strobes platform URL |
| `--api-key` | `STROBES_API_KEY` | Yes | API key from Settings |
| `--org-id` | `STROBES_ORG_ID` | Yes | Organization UUID |
| `--bridge-id` | `STROBES_BRIDGE_ID` | No | Auto-generated on first run |
| `--name` | `STROBES_SHELL_NAME` | No | Display name (defaults to hostname) |
| `--cwd` | `STROBES_CWD` | No | Working directory for commands |
| `-v` | `STROBES_VERBOSE` | No | Enable debug logging |

The `.env` file is loaded from the current directory or `~/.strobes-shell-agent/.env`.

### Example .env

```env
STROBES_URL=https://app.strobes.co
STROBES_API_KEY=sk-xxxxxxxxxxxx
STROBES_ORG_ID=your-org-uuid
STROBES_SHELL_NAME=prod-server
```

## What it supports

The AI agent can use these existing tools transparently through the bridge:

- **`workspace_execute_shell_command`** — Run any shell command (`nmap`, `curl`, `nuclei`, etc.)
- **`workspace_execute_code`** — Execute Python, JavaScript, or bash code
- **File operations** — Read, write, list, upload, and download files
- **Environment discovery** — OS, architecture, installed tools

No new tools are needed — the existing Strobes agent tools route through the bridge automatically when a bridge shell is attached.

## How it works

1. The daemon connects to Strobes via WebSocket (`wss://your-instance/ws/{org_id}/shell-bridge/`)
2. Authenticates with your API key
3. Sends an `identify` message with machine metadata
4. Waits for commands from the platform
5. Executes commands via subprocess, returns stdout/stderr/exit_code
6. Auto-reconnects with exponential backoff on disconnect
7. Periodic ping/pong keepalive (every 30s)

## Docker

### Build locally

```bash
docker build -t strobes/shell-agent .
```

### Run

```bash
docker run --rm --env-file .env strobes/shell-agent connect
```

### Docker Compose

```yaml
services:
  shell-agent:
    image: ghcr.io/strobes-co/strobes-agent-shell:latest
    env_file: .env
    restart: unless-stopped
    volumes:
      - ./workspace:/workspace  # Optional: mount files into the agent
```

## Development

```bash
git clone https://github.com/strobes-co/strobes-agent-shell.git
cd strobes-agent-shell
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run locally
strobes-shell-agent connect --url http://localhost:8001 --api-key sk-xxx --org-id xxx -v
```

## Commands

```bash
# Connect to Strobes (main command)
strobes-shell-agent connect [OPTIONS]

# Show the persistent bridge ID for this machine
strobes-shell-agent show-id

# Show version
strobes-shell-agent --version
```

## Security

- The daemon only accepts commands from the authenticated Strobes platform
- Commands execute with the permissions of the user running the daemon
- No inbound ports are opened — all connections are outbound
- API key authentication via the existing Strobes credentials system
- Consider running in a Docker container or as a limited user for isolation

## License

Proprietary - Strobes Security
