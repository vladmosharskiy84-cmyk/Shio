"""Вывод результатов в консоль (rich) и в CSV."""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

from .calc import Opportunity

console = Console()


def _pct(x, digits=3):
    return "—" if x is None else f"{x:.{digits}f}%"


def _stability(o: Opportunity) -> str:
    """Сравниваем текущую ставку со средней за историю."""
    if o.hist_rate_per_day is None:
        return "[dim]нет данных[/]"
    ratio = o.hist_rate_per_day / o.rate_per_day if o.rate_per_day else 0
    pos = o.hist_positive_share
    pos_s = f", {pos*100:.0f}% выплат >0" if pos is not None else ""
    if o.hist_rate_per_day <= 0:
        return f"[red]история против{pos_s}[/]"
    if ratio < 0.4:
        return f"[yellow]всплеск{pos_s}[/]"
    return f"[green]стабильно{pos_s}[/]"


def print_table(title: str, ops: list[Opportunity], top_n: int, hold_days: float):
    t = Table(title=title, show_lines=False, header_style="bold cyan")
    t.add_column("Монета", style="bold")
    t.add_column("Связка")
    t.add_column("Ставка/день", justify="right")
    t.add_column("Ср. за историю/день", justify="right")
    t.add_column("APR брутто", justify="right")
    t.add_column("APR на капитал\n(после комиссий)", justify="right")
    t.add_column(f"Прибыль за {hold_days:g} дн.", justify="right")
    t.add_column("Комиссии", justify="right")
    t.add_column("Разница цен", justify="right")
    t.add_column("Оценка истории")
    for o in ops[:top_n]:
        color = "green" if o.profit_hold_usd > 0 else "red"
        t.add_row(
            o.base, o.description,
            _pct(o.rate_per_day * 100),
            _pct(o.hist_rate_per_day * 100 if o.hist_rate_per_day is not None else None),
            f"{o.apr_gross:.1f}%",
            f"[{color}]{o.apr_net_capital:.1f}%[/]",
            f"[{color}]${o.profit_hold_usd:.2f}[/]",
            f"${o.fees_usd:.2f}",
            _pct(o.price_diff_pct, 2),
            _stability(o),
        )
    if not ops:
        console.print(f"[dim]{title}: подходящих связок нет[/]")
    else:
        console.print(t)


def print_report(spot_perp, perp_perp, stats, cfg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    console.rule(f"[bold]Скан funding-арбитража — {now}")
    for name, (np_, ns) in stats.items():
        mark = "[green]OK[/]" if np_ else "[red]нет данных[/]"
        console.print(f"  {name:10s} {mark}  фьючерсов: {np_}, спот-пар: {ns}")
    console.print(f"  Капитал ${cfg['capital_usd']}, плечо {cfg['perp_leverage']}x, "
                  f"удержание {cfg['hold_days']} дн., фильтр APR ≥ {cfg['min_net_apr']}%\n")
    print_table("1) Спот + шорт фьючерса на одной бирже (рекомендуется для старта)",
                spot_perp, cfg["top_n"], cfg["hold_days"])
    print_table("2) Фьючерс против фьючерса на разных биржах",
                perp_perp, cfg["top_n"], cfg["hold_days"])
    console.print("[dim]Это не торговые рекомендации. Ставка funding меняется каждый период; "
                  "«всплеск» = текущая ставка намного выше средней, скорее всего упадёт.[/]")


def save_csv(spot_perp, perp_perp, path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not os.path.exists(path)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_utc", "kind", "base", "description", "rate_per_day_pct",
                        "hist_rate_per_day_pct", "apr_gross", "apr_net_capital",
                        "profit_hold_usd", "fees_usd", "price_diff_pct"])
        for o in spot_perp + perp_perp:
            w.writerow([ts, o.kind, o.base, o.description, round(o.rate_per_day * 100, 5),
                        None if o.hist_rate_per_day is None else round(o.hist_rate_per_day * 100, 5),
                        round(o.apr_gross, 2), round(o.apr_net_capital, 2),
                        round(o.profit_hold_usd, 4), round(o.fees_usd, 4), round(o.price_diff_pct, 4)])
