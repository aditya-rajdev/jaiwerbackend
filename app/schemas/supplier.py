from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


PaymentMethod = Literal[
    "cash",
    "upi",
    "card",
    "bank_transfer",
]

PaymentMode = Literal[
    "item_wise",
    "total",
]


# -----------------------------------------
# Supplier purchase item
# -----------------------------------------

class SupplierItemRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")

    item_name: str = Field(min_length=1, max_length=150)

    weight_grams: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=10,
        decimal_places=3,
    )

    rate_per_gram: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=12,
        decimal_places=2,
    )

    total_amount: Decimal = Field(
        gt=0,
        max_digits=12,
        decimal_places=2,
    )

    amount_paid: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        max_digits=12,
        decimal_places=2,
    )

    @field_validator("item_name")
    @classmethod
    def validate_item_name(cls, value: str) -> str:
        value = value.strip()

        if not value:
            raise ValueError("Item name cannot be empty")

        return value

    @field_validator("amount_paid")
    @classmethod
    def validate_amount_paid(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))


# -----------------------------------------
# Shared initial purchase/payment validation
# -----------------------------------------

class SupplierPurchaseRequest(BaseModel):

    items: list[SupplierItemRequest] = Field(min_length=1)

    payment_mode: PaymentMode

    total_payment: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        max_digits=12,
        decimal_places=2,
    )

    payment_method: PaymentMethod | None = None

    notes: str | None = Field(
        default=None,
        max_length=500,
    )

    @model_validator(mode="after")
    def validate_payment(self):

        if self.payment_mode == "item_wise":

            if self.total_payment != 0:
                raise ValueError(
                    "total_payment must be zero in item_wise mode"
                )

            payment = sum(
                (item.amount_paid for item in self.items),
                Decimal("0"),
            )

        else:

            if any(item.amount_paid != 0 for item in self.items):
                raise ValueError(
                    "Do not provide item-level amount_paid "
                    "in total payment mode"
                )

            payment = self.total_payment

            total_amount = sum(
                (item.total_amount for item in self.items),
                Decimal("0"),
            )

            if payment > total_amount:
                raise ValueError(
                    "Payment cannot exceed total purchase amount"
                )

        if payment > 0 and self.payment_method is None:
            raise ValueError(
                "Payment method is required when payment is positive"
            )

        return self


# -----------------------------------------
# Create supplier
# -----------------------------------------

class CreateSupplierRequest(SupplierPurchaseRequest):

    supplier_name: str = Field(min_length=1, max_length=150)

    phone_number: str = Field(
        pattern=r"^\+[1-9]\d{7,14}$"
    )

    address: str | None = Field(
        default=None,
        max_length=500,
    )

    @field_validator("supplier_name")
    @classmethod
    def validate_supplier_name(cls, value: str) -> str:
        value = value.strip()

        if not value:
            raise ValueError("Supplier name cannot be empty")

        return value

    @field_validator("phone_number")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        return value.strip()


# -----------------------------------------
# Add purchases to existing supplier
# -----------------------------------------

class AddSupplierItemsRequest(SupplierPurchaseRequest):

    supplier_id: UUID


# -----------------------------------------
# Subsequent supplier payment
# -----------------------------------------

class SupplierPaymentAllocation(BaseModel):

    model_config = ConfigDict(extra="forbid")

    item_id: UUID

    allocated_amount: Decimal = Field(
        gt=0,
        max_digits=12,
        decimal_places=2,
    )


class RecordSupplierPaymentRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")

    supplier_id: UUID

    amount: Decimal = Field(
        gt=0,
        max_digits=12,
        decimal_places=2,
    )

    payment_method: PaymentMethod

    allocations: list[SupplierPaymentAllocation] = Field(
        min_length=1
    )

    notes: str | None = Field(
        default=None,
        max_length=500,
    )

    @model_validator(mode="after")
    def validate_allocations(self):

        item_ids = [
            allocation.item_id
            for allocation in self.allocations
        ]

        if len(item_ids) != len(set(item_ids)):
            raise ValueError(
                "Duplicate item allocations are not allowed"
            )

        allocated_total = sum(
            (
                allocation.allocated_amount
                for allocation in self.allocations
            ),
            Decimal("0"),
        )

        if allocated_total != self.amount:
            raise ValueError(
                "Payment amount must equal allocation total"
            )

        return self


# -----------------------------------------
# Supplier listing filters
# -----------------------------------------

class SupplierListQuery(BaseModel):

    filter: Literal["all", "baki", "paid"] = "all"

    search: str | None = None

    limit: int = Field(default=20, ge=1, le=100)

    offset: int = Field(default=0, ge=0)


# -----------------------------------------
# Supplier transaction listing filters
# -----------------------------------------

class SupplierTransactionListQuery(BaseModel):

    supplier_id: UUID | None = None

    search: str | None = None

    start_date: datetime | None = None

    end_date: datetime | None = None

    limit: int = Field(default=20, ge=1, le=100)

    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_date_range(self):

        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError(
                "Start date cannot be after end date"
            )

        return self