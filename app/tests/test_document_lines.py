"""Attached-document line items — multi-line GRN / bill parity.

Answers two live defects:

1. A 7-line GRN uploaded as a document was recorded as ONE line carrying the
   first row's amount: the vision-extracted table was never parsed, so every
   other line (and the document's own subtotal) was lost and the model had to
   reconcile the difference in a giant, timeout-prone round trip.
   — FIXED: the attached-document block is parsed into DISTINCT lines whose
   printed totals tie back to the document's stated subtotal.

2. Table furniture ("Line items") could become the party ledger name, so the
   agent kept asking "Create 'line items' as a new supplier?".
   — FIXED: the document's `Supplier:` header wins, and column headings can
   never be a party name.
"""

from app.planner import _extract_document_lines, _line_items_total, plan

# The REAL block from a live failing run (7-line purchase order).
GRN_MESSAGE = """record this purchase

[Attached document information]
[Document 1: image_5_-_purchase_order_template_2.webp]
Document type: purchase_order
Supplier: SM traders
Customer: Thendral Supermarket
Document number: 2024/PO-12
Date: 2024-04-27
Subtotal: 22141.0
Discount: 141.0
Total: 22000.0
Line 1: Surf Excel 5 kg | qty 20 | unit 600.0 | amount 12600.0
Line 2: Rin 1 kg | qty 25 | unit 85.0 | amount 2231.0
Line 3: Hamam soap 150 g | qty 25 | unit 60.0 | amount 1575.0
Line 4: Lux Soap 150 g | qty 30 | unit 53.0 | amount 443.0
Line 5: Dove soap 125 g | qty 25 | unit 75.0 | amount 1968.0
Line 6: Vim bar 200 g | qty 20 | unit 15.0 | amount 315.0
Line 7: Pepsodent 150 g | qty 30 | unit 85.0 | amount 3009.0
Extraction confidence: 0.98"""


class TestDocumentLineParsing:
    def test_seven_lines_are_all_parsed(self):
        lines, supplier = _extract_document_lines(GRN_MESSAGE)
        assert supplier == "SM traders"
        assert lines is not None and len(lines) == 7
        assert [l["description"] for l in lines] == [
            "Surf Excel 5 kg",
            "Rin 1 kg",
            "Hamam soap 150 g",
            "Lux Soap 150 g",
            "Dove soap 125 g",
            "Vim bar 200 g",
            "Pepsodent 150 g",
        ]
        assert all(l["quantity"] > 0 for l in lines)

    def test_printed_line_totals_tie_to_the_document(self):
        """Each line's printed total is honoured (unit = total / qty), so the
        recorded lines reconcile with the document's own subtotal rather than
        Line 1's amount alone."""
        lines, _ = _extract_document_lines(GRN_MESSAGE)
        # First line: 12600.0 printed for 20 units -> unit price 630.0, NOT the
        # document's (contradictory) 600.0 column.
        assert (lines[0]["quantity"], lines[0]["unit_price"]) == (20.0, 630.0)
        # And the derived total lands on the document's stated subtotal.
        assert abs(_line_items_total(lines) - 22141.0) < 0.2

    def test_line_one_amount_is_never_the_whole_bill(self):
        lines, _ = _extract_document_lines(GRN_MESSAGE)
        assert _line_items_total(lines) != 12600.0

    def test_table_headings_are_never_a_party(self):
        _, supplier = _extract_document_lines(
            "[Attached document information]\nSupplier: Line items\n"
        )
        assert supplier is None

    def test_missing_amount_falls_back_to_qty_times_unit(self):
        lines, _ = _extract_document_lines(
            "[Attached document information]\n"
            "Line 1: Chair | qty 4 | unit 1500.0\n"
            "Line 2: Table | qty 1 | unit 9000.0\n"
        )
        assert lines == [
            {"description": "Chair", "quantity": 4.0, "unit_price": 1500.0},
            {"description": "Table", "quantity": 1.0, "unit_price": 9000.0},
        ]

    def test_plain_text_never_invents_lines(self):
        assert _extract_document_lines("record a purchase of 5000") == (None, None)
        assert _extract_document_lines("") == (None, None)

    def test_plan_folds_document_lines_and_supplier(self):
        p = plan(GRN_MESSAGE)
        entities = p.extracted_entities
        assert len(entities.get("line_items") or []) == 7
        assert entities.get("supplier_name") == "SM traders"
        # The header amount is DERIVED from the lines (parity law), and every
        # line rides along so the bill can carry them.
        assert abs(float(entities["amount"]) - _line_items_total(
            entities["line_items"]
        )) <= 0.01

    def test_single_document_line_stays_a_single_item(self):
        lines, supplier = _extract_document_lines(
            "[Attached document information]\nSupplier: Acme\n"
            "Line 1: Laptop | qty 1 | unit 50000.0 | amount 50000.0\n"
        )
        assert supplier == "Acme"
        assert lines is not None and len(lines) == 1
