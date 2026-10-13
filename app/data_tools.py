"""
Tabular import/export helpers: load CSV/XLSX files into SQLite, and turn
result rows into downloadable CSV/XLSX bytes.
"""

import csv
import io
import os
import re
import sqlite3
from contextlib import closing

import pandas as pd

# Rows per chunk when streaming a CSV into SQLite, so a large upload is
# never held in memory all at once.
CSV_CHUNK_ROWS = 50_000

IMPORT_MODES = {"new": "fail", "append": "append", "replace": "replace"}


def validate_identifier(name: str) -> str:
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("Invalid table name. Use letters, numbers and underscores only.")
    return name


def safe_table_name(label: str, taken: set | None = None) -> str:
    """
    Turn a file or sheet name into a table name that needs no quoting in
    SQL (so generated queries can reference it directly), unique within
    `taken`.
    """
    name = re.sub(r"\W+", "_", (label or "").strip()).strip("_") or "data"
    if name[0].isdigit():
        name = f"t_{name}"

    candidate, n = name, 2
    while taken and candidate in taken:
        candidate = f"{name}_{n}"
        n += 1
    return candidate


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

def load_file_as_database(src_path: str, filename: str, dest_path: str) -> None:
    """
    Build a new SQLite database from a CSV (one table, named after the
    file) or an XLSX workbook (one table per non-empty sheet).
    """
    with closing(sqlite3.connect(dest_path)) as conn:
        if filename.lower().endswith(".csv"):
            table = safe_table_name(os.path.splitext(os.path.basename(filename))[0])
            created = False
            with pd.read_csv(src_path, chunksize=CSV_CHUNK_ROWS) as reader:
                for chunk in reader:
                    chunk.to_sql(table, conn, if_exists="append" if created else "replace", index=False)
                    created = True
            if not created:
                # Header-only file: still create the (empty) table.
                pd.read_csv(src_path).to_sql(table, conn, if_exists="replace", index=False)
            return

        taken = set()
        with pd.ExcelFile(src_path) as workbook:
            for sheet in workbook.sheet_names:
                df = workbook.parse(sheet)
                if df.columns.empty:
                    continue
                table = safe_table_name(str(sheet), taken)
                taken.add(table)
                df.to_sql(table, conn, if_exists="replace", index=False)


def import_into_table(db_path: str, src_path: str, filename: str, table_name: str, mode: str) -> int:
    """
    Load a CSV, or the first sheet of an XLSX, into one table of an
    existing database. Written in a single transaction — the table is
    either fully imported or left untouched. Returns the row count.
    """
    table_name = validate_identifier(table_name)

    if_exists = IMPORT_MODES.get(mode)
    if not if_exists:
        raise ValueError("Invalid import mode. Use new, append or replace.")

    lower = filename.lower()
    if lower.endswith(".csv"):
        df = pd.read_csv(src_path)
    elif lower.endswith(".xlsx"):
        df = pd.read_excel(src_path)
    else:
        raise ValueError("Only CSV and XLSX import is supported for existing databases.")

    if df.columns.empty:
        raise ValueError("The file has no columns to import.")

    with closing(sqlite3.connect(db_path)) as conn:
        df.to_sql(table_name, conn, if_exists=if_exists, index=False)

    return len(df)


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def to_csv_bytes(columns: list, rows: list[dict]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(columns)
    writer.writerows([row.get(c) for c in columns] for row in rows)
    # BOM so Excel opens UTF-8 correctly.
    return buffer.getvalue().encode("utf-8-sig")


def to_xlsx_bytes(columns: list, rows: list[dict], sheet_name: str = "Data") -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    def clean(value):
        return ILLEGAL_CHARACTERS_RE.sub("", value) if isinstance(value, str) else value

    workbook = Workbook(write_only=True)
    title = re.sub(r"[\[\]:*?/\\]", "_", sheet_name)[:31] or "Data"
    sheet = workbook.create_sheet(title=title)
    sheet.append([clean(c) for c in columns])
    for row in rows:
        sheet.append([clean(row.get(c)) for c in columns])

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
