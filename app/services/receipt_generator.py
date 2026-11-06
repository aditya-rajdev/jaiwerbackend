from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from fpdf import FPDF


FONT_DIR = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "fonts"
)


class PaymentReceiptPDF(FPDF):

    def __init__(self):
        super().__init__(format="A4")
        self.set_margins(15, 15, 15)
        self.set_auto_page_break(auto=True, margin=15)

        self.add_font(
            "DejaVu",
            fname=str(FONT_DIR / "DejaVuSansCondensed.ttf"),
        )

        self.add_font(
            "DejaVu",
            style="B",
            fname=str(FONT_DIR / "DejaVuSans-Bold.ttf"),
        )

    def section_title(self, title: str):
        self.set_font("DejaVu", "B", 11)
        self.cell(0, 9, title, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def label_value(self, label: str, value: str):
        start_x = self.l_margin
        start_y = self.get_y()

        label_width = 45
        value_width = (
            self.w - self.r_margin - start_x - label_width
        )

        self.set_font("DejaVu", "", 9)

        self.set_xy(start_x, start_y)
        self.cell(label_width, 7, label)

        self.set_xy(start_x + label_width, start_y)
        self.multi_cell(
            value_width,
            7,
            str(value),
        )

        self.ln(1)

    def allocation_row(self, item_name: str, amount: str):
        start_y = self.get_y()

        item_width = 125
        amount_width = 55

        self.set_font("DejaVu", "", 9)

        self.set_xy(self.l_margin, start_y)

        self.multi_cell(
            item_width,
            8,
            item_name,
        )

        end_y = self.get_y()

        self.set_xy(
            self.w - self.r_margin - amount_width,
            start_y,
        )

        self.cell(
            amount_width,
            8,
            amount,
            align="R",
        )

        self.set_y(max(end_y, start_y + 8) + 2)


def format_currency(amount) -> str:
    value = Decimal(str(amount))
    return f"INR {value:,.2f}"


def format_transaction_date(value: str) -> str:
    transaction_date = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    transaction_date = transaction_date.astimezone(
        ZoneInfo("Asia/Kolkata")
    )

    return transaction_date.strftime("%d %b %Y, %I:%M %p")


def generate_payment_receipt(
    transaction_data: dict,
    shop_profile: dict,
) -> bytes:

    transaction = transaction_data["transaction"]
    allocations = transaction_data["allocations"]

    pdf = PaymentReceiptPDF()
    pdf.add_page()

    # Shop heading
    pdf.set_font("DejaVu", "B", 20)
    pdf.cell(
        0,
        12,
        shop_profile["shop_name"],
        align="C",
        new_x="LMARGIN",
        new_y="NEXT",
    )

    pdf.set_font("DejaVu", "", 9)

    address = ", ".join(
        filter(
            None,
            [
                shop_profile.get("shop_address"),
                shop_profile.get("city"),
                shop_profile.get("state"),
                shop_profile.get("pincode"),
            ],
        )
    )

    pdf.multi_cell(0, 5, address, align="C")

    pdf.multi_cell(
        0,
        6,
        f"Phone: {shop_profile['phone_number']}",
        align="C",
        new_x="LMARGIN",
        new_y="NEXT",
    )

    if shop_profile.get("email"):
        pdf.multi_cell(
            0,
            6,
            shop_profile["email"],
            align="C",
            new_x="LMARGIN",
            new_y="NEXT",
        )

    pdf.ln(8)

    # Receipt title
    pdf.set_font("DejaVu", "B", 16)
    pdf.cell(
        0,
        10,
        "PAYMENT RECEIPT",
        align="C",
        new_x="LMARGIN",
        new_y="NEXT",
    )

    pdf.ln(4)

    # Receipt information
    pdf.section_title("Receipt Details")

    pdf.label_value(
        "Receipt Number:",
        transaction["receipt_number"],
    )

    pdf.label_value(
        "Date:",
        format_transaction_date(transaction["transaction_date"]),
    )

    pdf.ln(4)

    # Customer information
    pdf.section_title("Customer Details")

    pdf.label_value(
        "Customer Name:",
        transaction["customer_name"],
    )

    pdf.label_value(
        "Phone Number:",
        transaction["customer_phone_number"],
    )

    pdf.ln(4)

    # Payment information
    pdf.section_title("Payment Details")

    pdf.label_value(
        "Payment Method:",
        transaction["payment_method"].replace("_", " ").title(),
    )

    pdf.ln(4)

    # Allocation table
    pdf.section_title("Payment Allocation")

    pdf.set_font("DejaVu", "B", 9)

    pdf.cell(125, 9, "Item", border="B")
    pdf.cell(55, 9, "Amount", border="B", align="R")

    pdf.ln(10)

    for allocation in allocations:
        pdf.allocation_row(
            item_name=allocation["item_name"],
            amount=format_currency(
                allocation["allocated_amount"]
            ),
        )

    pdf.ln(4)

    # Total
    pdf.set_draw_color(100, 100, 100)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())

    pdf.ln(5)

    pdf.set_font("DejaVu", "B", 12)

    pdf.cell(90, 10, "TOTAL RECEIVED")
    pdf.cell(
        90,
        10,
        format_currency(transaction["amount"]),
        align="R",
    )

    # Optional footer
    footer = shop_profile.get("receipt_footer")

    if footer:
        pdf.ln(15)
        pdf.set_font("DejaVu", "", 9)
        pdf.multi_cell(0, 6, footer, align="C")

    pdf.ln(8)

    pdf.set_font("DejaVu", "", 8)
    pdf.cell(
        0,
        6,
        "Thank you for your business!",
        align="C",
    )

    return bytes(pdf.output())