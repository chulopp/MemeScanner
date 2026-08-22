import logging
import sys
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

# Ensure UTF-8 output on Windows console
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

console = Console(force_terminal=True, highlight=False)

logging.basicConfig(
    level="INFO",
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)]
)

import re

logger = logging.getLogger("memescanner")


def mask_url(url: str) -> str:
    """Masks sensitive query parameters like api-key in URLs."""
    if not url:
        return ""
    return re.sub(r"(api[-_]?key=)[^&\s]+", r"\1[REDACTED]", url, flags=re.IGNORECASE)


def print_token_table(tokens_data: list[dict]):
    """Print formatted summary table of processed tokens."""
    table = Table(title="[Solana Meme Coin Safety Filter Results]", show_lines=True)

    table.add_column("Symbol", style="cyan", no_wrap=True)
    table.add_column("Mint Address", style="magenta")
    table.add_column("Venue", style="yellow")
    table.add_column("Status", style="bold")
    table.add_column("Dev Buy %", justify="right")
    table.add_column("LP Lock %", justify="right")
    table.add_column("Scalp Flags", justify="center")
    table.add_column("Reason / Notes", style="dim")

    for t in tokens_data:
        status = t.get("status", "UNKNOWN")
        if status == "PASSED_SAFETY":
            status_formatted = "[green]PASS[/green]"
        elif status == "REJECTED":
            status_formatted = "[red]REJECT[/red]"
        else:
            status_formatted = f"[yellow]{status}[/yellow]"

        table.add_row(
            t.get("symbol", "N/A"),
            t.get("token_address", "")[:12] + "...",
            t.get("launch_venue", ""),
            status_formatted,
            f"{t.get('dev_holding_pct', 0.0):.1f}%",
            f"{t.get('lp_lock_pct', 0.0):.1f}%",
            str(t.get("instant_scalp_flags_count", 0)),
            t.get("rejection_reason", "-") or "-"
        )

    console.print(table)
