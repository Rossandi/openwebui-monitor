"""
title: PDF Tables to XLSX
author: corporate
version: 0.1.0
required_open_webui_version: 0.4.0
requirements: pdfplumber, openpyxl, pandas
description: Извлекает все таблицы из PDF и сохраняет каждую на отдельный лист Excel. Если таблиц нет — кладёт текст PDF в лист raw_text.
"""

import io
import base64
from typing import Optional
import pdfplumber
import pandas as pd


class Tools:
    def __init__(self):
        pass

    def pdf_tables_to_xlsx(
        self,
        pdf_base64: str,
        output_filename: Optional[str] = "tables.xlsx",
    ) -> dict:
        """
        Извлечь все таблицы из PDF и сохранить как Excel.
        Каждая таблица — отдельный лист.

        :param pdf_base64: Содержимое PDF в base64.
        :param output_filename: Имя выходного xlsx.
        :return: dict с base64 готового файла и метаданными.
        """
        pdf_bytes = base64.b64decode(pdf_base64)
        out = io.BytesIO()

        sheets_meta = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf, \
             pd.ExcelWriter(out, engine="openpyxl") as writer:
            sheet_idx = 0
            for page_num, page in enumerate(pdf.pages, start=1):
                tables = page.extract_tables() or []
                for t_idx, table in enumerate(tables, start=1):
                    if not table or len(table) < 2:
                        continue
                    df = pd.DataFrame(table[1:], columns=table[0])
                    sheet_name = f"p{page_num}_t{t_idx}"[:31]
                    df.to_excel(writer, sheet_name=sheet_name, index=False)
                    sheets_meta.append({
                        "sheet": sheet_name,
                        "rows": len(df),
                        "cols": len(df.columns),
                    })
                    sheet_idx += 1

            if sheet_idx == 0:
                full_text = "\n\n".join(
                    (p.extract_text() or "") for p in pdf.pages
                )
                pd.DataFrame({"text": [full_text]}).to_excel(
                    writer, sheet_name="raw_text", index=False
                )
                sheets_meta.append({"sheet": "raw_text", "fallback": True})

        out.seek(0)
        return {
            "filename": output_filename,
            "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "content_base64": base64.b64encode(out.read()).decode(),
            "sheets": sheets_meta,
        }
