"""
LLM ETL — CLI entry point.

Commands:
  python main.py init-db          Create SQLite DB and run schema.sql
  python main.py test-llm         Check that Ollama is reachable
  python main.py parse <file>     Parse a file and print extracted records
  python main.py ingest <file>    Parse a file AND write to DB
  python main.py status           Show DB row counts
  python main.py train            Run QLoRA fine-tuning
  python main.py ui               Launch Gradio web UI
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Force UTF-8 on Windows consoles (cp1251 chokes on Russian + box-drawing)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import click
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

console = Console()


@click.group()
def cli():
    """LLM ETL Configurator — parse unknown data protocols into a SQLite DB."""


@cli.command("init-db")
def init_db():
    """Initialise SQLite database from config/schema.sql."""
    from core.db_writer import DBWriter

    console.print("[cyan]Initialising database…[/cyan]")
    Path(ROOT / "data").mkdir(exist_ok=True)
    w = DBWriter()
    tables = w.list_tables()
    console.print(f"[green][OK] DB ready — {len(tables)} tables[/green]")
    for t in tables:
        console.print(f"  * {t}")


@cli.command("test-llm")
def test_llm():
    """Send a ping to the configured LLM backend."""
    from core.llm_client import LLMClient

    console.print("[cyan]Testing LLM…[/cyan]")
    if LLMClient().test_connection():
        console.print("[green][OK] LLM responding[/green]")
    else:
        console.print("[red][FAIL] LLM not responding[/red]")
        sys.exit(1)


@cli.command()
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", type=click.Path(), help="Write JSON to this file")
def parse(file, out):
    """Parse a file and print the extracted records as JSON."""
    from core.data_parser import DataParser

    console.print(f"[cyan]Parsing {file}…[/cyan]")
    records = DataParser().parse_file(file)
    payload = json.dumps(records, ensure_ascii=False, indent=2)
    if out:
        Path(out).write_text(payload, encoding="utf-8")
        console.print(f"[green][OK] Wrote {len(records)} records → {out}[/green]")
    else:
        console.print(payload)
        console.print(f"[green][OK] {len(records)} records extracted[/green]")


@cli.command()
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("--dry-run", is_flag=True, help="Parse only, do not write to DB")
def ingest(file, dry_run):
    """Parse a file and write records to the DB."""
    from core.data_parser import DataParser
    from core.db_writer import DBWriter

    console.print(f"[cyan]Ingesting {file}…[/cyan]")
    records = DataParser().parse_file(file)
    console.print(f"  parsed [bold]{len(records)}[/bold] records")
    stats = DBWriter(dry_run=dry_run).write(records)
    console.print(
        f"  inserted=[green]{stats['inserted']}[/green]  "
        f"skipped=[yellow]{stats['skipped']}[/yellow]  "
        f"errors=[red]{len(stats['errors'])}[/red]"
    )
    for err in stats["errors"][:5]:
        console.print(f"  [red][FAIL][/red] {err}")


@cli.command()
def status():
    """Show row counts per table."""
    from core.db_writer import DBWriter

    counts = DBWriter().row_counts()
    table = Table(title="DB row counts")
    table.add_column("Table", style="cyan")
    table.add_column("Rows", justify="right", style="green")
    for name, n in sorted(counts.items()):
        table.add_row(name, str(n))
    console.print(table)


@cli.command()
def train():
    """Run QLoRA fine-tuning on examples/training_data/*.jsonl."""
    from training.finetune import run_training

    run_training()


@cli.command("train-continual")
def train_continual():
    """Continual learning cycle: gather new examples, mix with replay buffer, fine-tune, eval-gated promote."""
    from training.continual import run_continual_training

    run_continual_training()


@cli.command()
@click.option("--adapter", default=None, help="Adapter directory (defaults to current production)")
@click.option("--holdout", default="examples/eval_holdout", help="Holdout JSONL dir")
def evaluate(adapter, holdout):
    """Run holdout evaluation and print F1 per table."""
    from training.evaluate import evaluate as run_eval

    metrics = run_eval(
        adapter_path=Path(adapter) if adapter else None,
        examples_dir=Path(holdout) if holdout else None,
    )
    console.print_json(data=metrics)


@cli.command("bootstrap-1c")
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--out", default="examples/training_data/bootstrap_1c.jsonl",
              help="Output JSONL path")
def bootstrap_1c(source_dir, out):
    """Bootstrap training examples from a folder of 1C CommerceML import.xml files."""
    from core.parsers.commerceml import parse_import_xml
    from training.annotation import bootstrap_from_parser

    n = bootstrap_from_parser(source_dir, parse_import_xml, out)
    console.print(f"[green][OK] wrote {n} examples to {out}[/green]")


@cli.command()
@click.option("--port", default=7860, help="Port for Gradio server")
@click.option("--share", is_flag=True, help="Create a public share URL")
def ui(port, share):
    """Launch the Gradio web UI."""
    from ui.app import launch

    launch(share=share, port=port)


@cli.command("count-examples")
def count_examples():
    """Show how many training examples are queued."""
    from training.dataset_builder import example_count, load_examples

    examples = load_examples(ROOT / "examples" / "training_data")
    console.print(f"[green]{len(examples)}[/green] training examples")
    for ex in examples[:3]:
        console.print(
            f"  * input: [dim]{ex['input'][:80].replace(chr(10), ' ')}…[/dim]"
            f" → [yellow]{len(ex['output'])}[/yellow] records"
        )


if __name__ == "__main__":
    cli()
