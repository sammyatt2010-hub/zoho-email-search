"""Report builders: turn the canonical detail DataFrame (see email_log.py)
into styled XLSX and PDF exports, matching the look of the reports built for
the Servicevend NW Limited dispute investigation.

Both builders are pure functions of (detail_df, summary_df, meta) -> bytes,
with no dependency on Streamlit or on how the data was fetched — this is
what lets them also be reused by other tooling (e.g. pointing the original
one-off CSV-combining script at these same builders) if that's wanted later.
"""

from __future__ import annotations

from io import BytesIO

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

FONT = "Arial"


def build_summary_df(detail_df: pd.DataFrame) -> pd.DataFrame:
    """Per-recipient-address counts plus bounced/opened rollups, matching
    the "Breakdown by Recipient Address" table from today's report."""

    if detail_df.empty:
        return pd.DataFrame(columns=["sent_to", "entries", "bounced", "opened"])

    grouped = (
        detail_df.groupby("sent_to")
        .agg(entries=("sent_to", "size"), bounced=("bounced", "sum"), opened=("opened", "sum"))
        .reset_index()
        .sort_values("entries", ascending=False)
        .reset_index(drop=True)
    )
    return grouped


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------


def build_xlsx_report(detail_df: pd.DataFrame, summary_df: pd.DataFrame, meta: dict) -> BytesIO:
    """meta expects: search_value, search_mode ('Exact'/'Domain'),
    date_from, date_to, matched_record_count, exported_on (str),
    failed_record_count (optional)."""

    wb = Workbook()

    title_font = Font(name=FONT, size=14, bold=True, color="FFFFFF")
    subtitle_font = Font(name=FONT, size=10, italic=True, color="404040")
    header_font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
    body_font = Font(name=FONT, size=10)
    bold_font = Font(name=FONT, size=10, bold=True)
    note_font = Font(name=FONT, size=9, italic=True, color="808080")

    title_fill = PatternFill(start_color="C00000", end_color="C00000", fill_type="solid")
    header_fill = PatternFill(start_color="404040", end_color="404040", fill_type="solid")
    bounced_fill = PatternFill(start_color="FCE4E4", end_color="FCE4E4", fill_type="solid")
    opened_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")

    thin = Side(style="thin", color="B7B7B7")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # --- Summary sheet ---
    ws = wb.active
    ws.title = "Summary"

    ws.merge_cells("A1:D1")
    ws["A1"] = "Zoho CRM Email Log Search"
    ws["A1"].font = title_font
    ws["A1"].fill = title_fill
    ws["A1"].alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 28

    ws.merge_cells("A2:D2")
    ws["A2"] = f"Exported: {meta.get('exported_on', '')}"
    ws["A2"].font = subtitle_font
    ws.row_dimensions[2].height = 18

    record_count_label = meta.get("record_count_label", "Matching Contacts/Leads/Accounts found:")
    meta_rows = [
        ("Search mode:", meta.get("search_mode", "")),
        ("Search value:", meta.get("search_value", "")),
        ("Date range:", f"{meta.get('date_from', '')} to {meta.get('date_to', '')}"),
        (record_count_label, str(meta.get("matched_record_count", ""))),
        ("Unique email rows after de-duplication:", str(len(detail_df))),
        ("Of which bounced:", str(int(detail_df["bounced"].sum()) if not detail_df.empty else 0)),
        ("Of which opened:", str(int(detail_df["opened"].sum()) if not detail_df.empty else 0)),
    ]
    if meta.get("failed_record_count"):
        meta_rows.append(("Records that failed to fetch:", str(meta["failed_record_count"])))

    r = 4
    ws.cell(row=r, column=1, value="Search Summary").font = Font(
        name=FONT, size=12, bold=True, color="C00000"
    )
    r += 1
    for label, val in meta_rows:
        ws.cell(row=r, column=1, value=label).font = bold_font
        ws.cell(row=r, column=2, value=val).font = body_font
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="Breakdown by Recipient Address").font = Font(
        name=FONT, size=12, bold=True, color="C00000"
    )
    r += 1
    header_row = r
    headers = ["Recipient Address", "Entries", "Bounced", "Opened"]
    for col, h in enumerate(headers, start=1):
        c = ws.cell(row=header_row, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.border = border
        c.alignment = Alignment(horizontal="left", vertical="center")
    r += 1

    for _, row in summary_df.iterrows():
        ws.cell(row=r, column=1, value=row["sent_to"]).font = body_font
        ws.cell(row=r, column=2, value=int(row["entries"])).font = body_font
        ws.cell(row=r, column=3, value=int(row["bounced"])).font = body_font
        ws.cell(row=r, column=4, value=int(row["opened"])).font = body_font
        for col in range(1, 5):
            ws.cell(row=r, column=col).border = border
        r += 1

    for i, w in enumerate([46, 14, 12, 12], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # --- Detail sheet ---
    ws2 = wb.create_sheet("Email Log Detail")
    ws2.merge_cells("A1:H1")
    ws2["A1"] = f"Email Log Detail ({len(detail_df)} records)"
    ws2["A1"].font = title_font
    ws2["A1"].fill = title_fill
    ws2["A1"].alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws2.row_dimensions[1].height = 26

    headers2 = [
        "Sent On",
        "Subject",
        "Sent To",
        "Direction",
        "Source Module",
        "Source Record",
        "Opened",
        "Bounced",
    ]
    hr = 3
    for col, h in enumerate(headers2, start=1):
        c = ws2.cell(row=hr, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.border = border
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws2.row_dimensions[hr].height = 22

    rr = hr + 1
    for _, row in detail_df.iterrows():
        sent_on = row["sent_on"].strftime("%Y-%m-%d %H:%M") if pd.notna(row["sent_on"]) else ""
        vals = [
            sent_on,
            row["subject"],
            row["sent_to"],
            row["direction"],
            row["source_module"],
            row["source_record_name"],
            "Yes" if row["opened"] else "",
            "Yes" if row["bounced"] else "",
        ]
        for col, v in enumerate(vals, start=1):
            c = ws2.cell(row=rr, column=col, value=v)
            c.font = body_font
            c.border = border
            c.alignment = Alignment(horizontal="left", vertical="top", wrap_text=(col in (2, 3)))
        if row["bounced"]:
            for col in range(1, 9):
                ws2.cell(row=rr, column=col).fill = bounced_fill
        elif row["opened"]:
            ws2.cell(row=rr, column=7).fill = opened_fill
        rr += 1

    for i, w in enumerate([16, 34, 30, 14, 14, 24, 8, 9], start=1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes = "A4"

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def build_pdf_report(detail_df: pd.DataFrame, summary_df: pd.DataFrame, meta: dict) -> BytesIO:
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TitleCustom",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=17,
        textColor=colors.HexColor("#C00000"),
        spaceAfter=4,
        alignment=TA_LEFT,
    )
    sub_style = ParagraphStyle(
        "SubCustom",
        parent=styles["Normal"],
        fontName="Helvetica-Oblique",
        fontSize=9.5,
        textColor=colors.HexColor("#555555"),
        spaceAfter=14,
    )
    section_style = ParagraphStyle(
        "Section",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=12,
        textColor=colors.HexColor("#C00000"),
        spaceBefore=14,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "BodyCustom", parent=styles["Normal"], fontName="Helvetica", fontSize=9.5, leading=13
    )
    cell_style = ParagraphStyle(
        "Cell", parent=styles["Normal"], fontName="Helvetica", fontSize=7.6, leading=9.8
    )
    cell_bold = ParagraphStyle(
        "CellBold", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=7.6, leading=9.8
    )
    note_style = ParagraphStyle(
        "Note",
        parent=styles["Normal"],
        fontName="Helvetica-Oblique",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#777777"),
        spaceBefore=8,
    )

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        topMargin=16 * mm,
        bottomMargin=14 * mm,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        title="Zoho CRM Email Log Search",
    )

    story = []
    story.append(Paragraph("Zoho CRM Email Log Search", title_style))
    story.append(
        Paragraph(
            f"Search: {meta.get('search_mode', '')} — {meta.get('search_value', '')}",
            sub_style,
        )
    )
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#C00000")))
    story.append(Spacer(1, 8))

    intro = (
        "This report was generated by the Zoho CRM Email Log Search tool. It combines the email "
        "history of every Contact, Lead, and Account whose email address matched the search "
        "criteria below, deduplicated and filtered to the chosen date range."
    )
    story.append(Paragraph(intro, body_style))
    story.append(Spacer(1, 6))

    bounced_n = int(detail_df["bounced"].sum()) if not detail_df.empty else 0
    opened_n = int(detail_df["opened"].sum()) if not detail_df.empty else 0

    record_count_label = meta.get("record_count_label", "Matching Contacts/Leads/Accounts found:")
    meta_rows = [
        ["Search mode:", meta.get("search_mode", "")],
        ["Search value:", meta.get("search_value", "")],
        ["Date range:", f"{meta.get('date_from', '')} to {meta.get('date_to', '')}"],
        [record_count_label, str(meta.get("matched_record_count", ""))],
        ["Unique email rows after de-duplication:", str(len(detail_df))],
        ["Of which bounced:", str(bounced_n)],
        ["Of which opened:", str(opened_n)],
        ["Export date:", meta.get("exported_on", "")],
    ]
    meta_table = Table(meta_rows, colWidths=[85 * mm, 92 * mm])
    meta_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (0, -1), 8),
            ]
        )
    )
    story.append(meta_table)
    story.append(Spacer(1, 4))

    story.append(Paragraph("Breakdown by Recipient Address", section_style))
    addr_rows = [["Recipient Address", "Entries", "Bounced", "Opened"]]
    for _, row in summary_df.iterrows():
        addr_rows.append(
            [row["sent_to"], str(int(row["entries"])), str(int(row["bounced"])), str(int(row["opened"]))]
        )
    addr_table = Table(addr_rows, colWidths=[95 * mm, 27 * mm, 27 * mm, 28 * mm], repeatRows=1)
    addr_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#404040")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B7B7B7")),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("ALIGN", (1, 0), (-1, -1), "CENTER"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F7F7")]),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(addr_table)

    note = (
        "Rows highlighted in red in the detail table below show a bounced delivery status. "
        "This export reflects Contacts, Leads, and Accounts only (not Deals/Vendors/Cases)."
    )
    story.append(Paragraph(note, note_style))
    story.append(PageBreak())

    story.append(Paragraph("Email Log Detail", section_style))
    story.append(Spacer(1, 4))

    def P(text, style=cell_style):
        if text is None:
            text = ""
        text = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return Paragraph(text, style)

    header = [
        P("Sent On", cell_bold),
        P("Subject", cell_bold),
        P("Sent To", cell_bold),
        P("Source", cell_bold),
        P("Opened", cell_bold),
        P("Bounced", cell_bold),
    ]
    table_rows = [header]
    bounced_flags = []
    for _, row in detail_df.iterrows():
        sent_on = row["sent_on"].strftime("%Y-%m-%d %H:%M") if pd.notna(row["sent_on"]) else ""
        table_rows.append(
            [
                P(sent_on),
                P(row["subject"]),
                P(row["sent_to"]),
                P(f"{row['source_module']}: {row['source_record_name']}"),
                P("Yes" if row["opened"] else ""),
                P("Yes" if row["bounced"] else "", cell_bold if row["bounced"] else cell_style),
            ]
        )
        bounced_flags.append(bool(row["bounced"]))

    col_widths = [22 * mm, 48 * mm, 42 * mm, 42 * mm, 16 * mm, 18 * mm]
    tbl = Table(table_rows, colWidths=col_widths, repeatRows=1)
    tbl_style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#404040")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C7C7C7")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]
    for i, is_bounced in enumerate(bounced_flags, start=1):
        if is_bounced:
            tbl_style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#FCE4E4")))
        elif i % 2 == 0:
            tbl_style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#F7F7F7")))
    tbl.setStyle(TableStyle(tbl_style))
    story.append(tbl)

    story.append(Spacer(1, 10))
    footer = meta.get(
        "source_label", "Source: Zoho CRM, via the zoho-email-search tool (live API search)."
    )
    story.append(
        Paragraph(
            footer,
            ParagraphStyle("Footer", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#999999")),
        )
    )

    doc.build(story)
    buf.seek(0)
    return buf
