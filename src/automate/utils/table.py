"""
utils/table.py — Colorful terminal table rendering for backtest reports,
built on `rich` (https://github.com/Textualize/rich).
"""
from typing import Optional, Sequence

from rich.console import Console
from rich.table import Table

_console = Console(width=200)


def render_stats_table(
    title: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    pnl_columns: Optional[Sequence[int]] = None,
) -> None:
    """
    Print a colorful rich table to the terminal.

    Args:
        title: Table title.
        headers: Column headers (first column is treated as the metric-name
            label column and left-aligned/bold; the rest are right-aligned).
        rows: List of rows, each a list of string cells.
        pnl_columns: Indices of columns whose cell values should be colored
            green (positive, has a leading '+') / red (negative, has a
            leading '-') based on the leading sign character — used for
            P&L figures. Defaults to every column except the first.
    """
    table = Table(title=title, title_style="bold cyan", header_style="bold white on dark_blue", show_lines=False)
    table.add_column(headers[0], style="bold", no_wrap=True)
    for h in headers[1:]:
        table.add_column(h, justify="right", no_wrap=True)

    if pnl_columns is None:
        pnl_columns = list(range(1, len(headers)))

    for row in rows:
        styled_row = [str(row[0])]
        for i, cell in enumerate(row[1:], start=1):
            text = str(cell)
            if i in pnl_columns and text and text[0] in "+-" and text not in ("-", "—"):
                color = "green" if text[0] == "+" else "red"
                styled_row.append(f"[{color}]{text}[/{color}]")
            else:
                styled_row.append(text)
        table.add_row(*styled_row)

    _console.print(table)


def print_meta_line(parts: dict) -> None:
    """Print a dim single-line summary of run metadata below a table."""
    joined = "  [dim]|[/dim]  ".join(f"[bold]{k}:[/bold] {v}" for k, v in parts.items())
    _console.print(f"\n{joined}\n")
