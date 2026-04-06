"""CLI entry point for the Strobes Shell Bridge Agent."""

import asyncio
import logging
import signal
import sys

import click

from strobes_shell_agent.config import get_or_create_bridge_id, get_env
from strobes_shell_agent.client import ShellBridgeClient


def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
@click.version_option(version="0.1.0")
def main():
    """Strobes Shell Bridge Agent — connect your machine to Strobes."""
    pass


@main.command()
@click.option("--url", default=None, envvar="STROBES_URL",
              help="Strobes platform URL (env: STROBES_URL)")
@click.option("--api-key", default=None, envvar="STROBES_API_KEY",
              help="Strobes API key (env: STROBES_API_KEY)")
@click.option("--org-id", default=None, envvar="STROBES_ORG_ID",
              help="Organization ID (env: STROBES_ORG_ID)")
@click.option("--bridge-id", default=None, envvar="STROBES_BRIDGE_ID",
              help="Bridge ID — auto-generated if not provided (env: STROBES_BRIDGE_ID)")
@click.option("--name", default=None, envvar="STROBES_SHELL_NAME",
              help="Display name for this shell (env: STROBES_SHELL_NAME)")
@click.option("--cwd", default=None, envvar="STROBES_CWD",
              help="Working directory for commands (env: STROBES_CWD)")
@click.option("--ssl-verify/--no-ssl-verify", default=True, envvar="STROBES_SSL_VERIFY",
              help="Verify SSL certificates (env: STROBES_SSL_VERIFY)")
@click.option("-v", "--verbose", is_flag=True, envvar="STROBES_VERBOSE",
              help="Enable debug logging (env: STROBES_VERBOSE)")
def connect(url, api_key, org_id, bridge_id, name, cwd, ssl_verify, verbose):
    """Connect to Strobes and start accepting commands.

    All options can be set via environment variables or a .env file.
    Place a .env file in the current directory or ~/.strobes-shell-agent/.env

    \b
    Example .env:
        STROBES_URL=https://app.strobes.co
        STROBES_API_KEY=sk-xxxxxxxxxxxx
        STROBES_ORG_ID=your-org-uuid
        STROBES_SHELL_NAME=my-server
    """
    setup_logging(verbose)
    logger = logging.getLogger(__name__)

    if not url:
        click.echo("Error: --url or STROBES_URL is required", err=True)
        sys.exit(1)
    if not api_key:
        click.echo("Error: --api-key or STROBES_API_KEY is required", err=True)
        sys.exit(1)
    if not org_id:
        click.echo("Error: --org-id or STROBES_ORG_ID is required", err=True)
        sys.exit(1)

    # Use persistent bridge_id if not provided
    if not bridge_id:
        bridge_id = get_or_create_bridge_id()

    client = ShellBridgeClient(
        url=url,
        api_key=api_key,
        org_id=org_id,
        bridge_id=bridge_id,
        name=name or "",
        cwd=cwd,
        ssl_verify=ssl_verify,
    )

    click.echo("Strobes Shell Bridge Agent v0.1.0")
    click.echo(f"  Bridge ID:  {bridge_id}")
    click.echo(f"  Name:       {client.name}")
    click.echo(f"  Org:        {org_id}")
    click.echo(f"  Server:     {url}")
    click.echo(f"  CWD:        {client.cwd}")
    click.echo()

    # Handle graceful shutdown
    loop = asyncio.new_event_loop()

    def shutdown_handler():
        logger.info("Shutting down...")
        client.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_handler)
        except NotImplementedError:
            signal.signal(sig, lambda s, f: shutdown_handler())

    try:
        loop.run_until_complete(client.connect_forever())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
        click.echo("Disconnected.")


@main.command()
def show_id():
    """Show the persistent bridge ID for this machine."""
    bridge_id = get_or_create_bridge_id()
    click.echo(bridge_id)


if __name__ == "__main__":
    main()
