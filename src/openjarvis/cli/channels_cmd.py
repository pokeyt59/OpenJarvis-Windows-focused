"""``jarvis channels`` — manage messaging channels for the agent.

The native macOS iMessage daemon was removed when this fork went Windows-only.
Inbound iMessage / SMS now goes through the SendBlue channel (configured via
the desktop app or the ``jarvis sendblue`` CLI), and the only native daemon
still managed here is Slack.
"""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table


@click.group("channels")
def channels() -> None:
    """Manage messaging channels (Slack daemon; SendBlue is managed elsewhere)."""


@channels.command("status")
def channels_status() -> None:
    """Show status of all configured native channel daemons."""
    console = Console()
    table = Table(title="Channel Status")
    table.add_column("Channel", style="bold")
    table.add_column("Status")
    table.add_column("Details", style="dim")

    try:
        from openjarvis.channels.slack_daemon import is_running as slack_running

        if slack_running():
            table.add_row("Slack", "[green]running[/green]", "Polling Slack events")
        else:
            table.add_row("Slack", "[dim]stopped[/dim]", "")
    except Exception:
        table.add_row("Slack", "[dim]unavailable[/dim]", "module not importable")

    table.add_row(
        "SendBlue (iMessage / SMS)",
        "[dim]see /v1/channels/sendblue/health[/dim]",
        "Managed via the desktop app or REST API",
    )

    console.print(table)


@channels.command("slack-stop")
def slack_stop() -> None:
    """Stop the Slack daemon, if running."""
    from openjarvis.channels.slack_daemon import stop_daemon

    console = Console()
    if stop_daemon():
        console.print("[green]Slack daemon stopped.[/green]")
    else:
        console.print("[dim]Slack daemon is not running.[/dim]")
