"""Сканер funding-арбитража.

    python main.py            # один скан
    python main.py --loop     # сканировать каждые N минут (см. config.yaml)
"""
import argparse
import asyncio
import logging

import yaml

from scanner.engine import scan
from scanner.report import console, print_report, save_csv


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


async def run(cfg: dict, loop: bool, csv_path: str):
    while True:
        try:
            spot_perp, perp_perp, stats = await scan(cfg)
            print_report(spot_perp, perp_perp, stats, cfg)
            save_csv(spot_perp, perp_perp, csv_path)
            console.print(f"[dim]Сохранено в {csv_path}[/]")
        except Exception as e:
            console.print(f"[red]Ошибка скана: {e}[/]")
        if not loop:
            return
        await asyncio.sleep(cfg["scan_interval_minutes"] * 60)


def main():
    ap = argparse.ArgumentParser(description="Сканер funding-арбитража")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--loop", action="store_true", help="сканировать по расписанию")
    ap.add_argument("--csv", default="data/opportunities.csv")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(load_config(args.config), args.loop, args.csv))
    except KeyboardInterrupt:
        console.print("\nОстановлено.")


if __name__ == "__main__":
    main()
