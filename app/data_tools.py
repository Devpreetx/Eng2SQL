import io
import os
import re
import sqlite3
import pandas as pd

MAX_EXPORT_ROWS = int(os.getenv("MAX_EXPORT_ROWS", "50000"))

def validate_identifier(name: str) -> str:
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("Invalid table name. Use letters, numbers and underscores only.")
    return name

def export_query(path, sql, fmt="csv"):
    conn=sqlite3.connect(path)
    try:
        df=pd.read_sql_query(sql, conn)
    finally:
        conn.close()
    if len(df)>MAX_EXPORT_ROWS:
        df=df.head(MAX_EXPORT_ROWS)
    if fmt=="csv":
        out=io.BytesIO(); df.to_csv(out,index=False); out.seek(0); return out
    if fmt=="xlsx":
        out=io.BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as writer: df.to_excel(writer,index=False,sheet_name="results")
        out.seek(0); return out
    raise ValueError("Unsupported export format.")

def export_table(path, table_name, fmt="csv"):
    table_name=validate_identifier(table_name)
    conn=sqlite3.connect(path)
    try:
        df=pd.read_sql_query(f'SELECT * FROM "{table_name}" LIMIT {MAX_EXPORT_ROWS}', conn)
    finally: conn.close()
    if fmt=="csv":
        out=io.BytesIO(); df.to_csv(out,index=False); out.seek(0); return out
    if fmt=="xlsx":
        out=io.BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as writer: df.to_excel(writer,index=False,sheet_name=table_name[:31])
        out.seek(0); return out
    raise ValueError("Unsupported export format.")

def import_dataframe(path, fileobj, filename, table_name, mode):
    table_name=validate_identifier(table_name)
    lower=filename.lower()
    if lower.endswith(".csv"):
        df=pd.read_csv(fileobj)
    elif lower.endswith(".xlsx"):
        df=pd.read_excel(fileobj)
    else:
        raise ValueError("Only CSV and XLSX import is supported for existing databases.")
    conn=sqlite3.connect(path)
    try:
        if_exists = {"append":"append","replace":"replace","new":"fail"}.get(mode)
        if not if_exists: raise ValueError("Invalid import mode.")
        df.to_sql(table_name, conn, if_exists=if_exists, index=False)
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally: conn.close()
    return len(df)
