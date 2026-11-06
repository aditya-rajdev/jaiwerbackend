from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


PaymentMode = Literal["item_wise", "total"]

PaymentMethod = Literal[
    "cash",
    "upi",
    "card",
    "bank_transfer",
]


class CustomerItemFields(BaseModel):
    item_name: str = Field(min_length=1, max_length=150)

    weight_grams: Decimal = Field(
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

    amount_paid: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=12,
        decimal_places=2,
    )

    reminder_date: date | None = None
    sms_reminder_enabled: bool = False


class PurchasePaymentFields(BaseModel):
    payment_mode: PaymentMode

    payment_method: PaymentMethod | None = None

    # Required only when payment_mode == "total"
    total_payment: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=12,
        decimal_places=2,
    )

    @model_validator(mode="after")
    def validate_payment_details(self):
        items = self.items

        if self.payment_mode == "item_wise":
            if self.total_payment is not None:
                raise ValueError(
                    "total_payment is not allowed in item_wise mode"
                )

            if any(item.amount_paid is None for item in items):
                raise ValueError(
                    "amount_paid is required for every item "
                    "in item_wise mode"
                )

            payment_amount = sum(
                (item.amount_paid for item in items),
                Decimal("0"),
            )

        else:
            if self.total_payment is None:
                raise ValueError(
                    "total_payment is required in total mode"
                )

            if any(item.amount_paid is not None for item in items):
                raise ValueError(
                    "Do not provide item-level amount_paid "
                    "in total mode"
                )

            purchase_total = sum(
                (item.total_amount for item in items),
                Decimal("0"),
            )

            if self.total_payment > purchase_total:
                raise ValueError(
                    "Payment cannot exceed the total purchase amount"
                )

            payment_amount = self.total_payment

        if payment_amount > 0 and self.payment_method is None:
            raise ValueError(
                "payment_method is required when payment is positive"
            )

        return self


class AddCustomerRequest(PurchasePaymentFields):
    name: str = Field(min_length=2, max_length=100)
    phone_number: str
    address: str | None = None

    items: list[CustomerItemFields] = Field(min_length=1)


class ExistingCustomerRequest(PurchasePaymentFields):
    phone_number: str
    receiver: str | None = None

    items: list[CustomerItemFields] = Field(min_length=1)


class PaymentAllocationRequest(BaseModel):
    item_id: UUID

    amount: Decimal = Field(
        gt=0,
        max_digits=12,
        decimal_places=2,
    )


class CustomerPaymentRequest(BaseModel):
    payment_method: PaymentMethod
    allocations: list[PaymentAllocationRequest] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_allocations(self):
        item_ids = [
            allocation.item_id
            for allocation in self.allocations
        ]

        if len(item_ids) != len(set(item_ids)):
            raise ValueError(
                "An item can appear only once in payment allocations"
            )

        return self

class CustomerResponse(BaseModel):
    id: UUID
    name: str
    phone_number: str
    address: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CustomerItemResponse(BaseModel):
    id: UUID
    customer_id: UUID
    owner_user_id: UUID
    item_name: str
    weight_grams: Decimal
    rate_per_gram: Decimal | None
    total_amount: Decimal
    amount_paid: Decimal
    due_amount: Decimal
    receiver: str | None
    reminder_date: date | None
    sms_reminder_enabled: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class CustomerTransactionResponse(BaseModel):
    id: UUID
    customer_id: UUID
    owner_user_id: UUID
    amount: Decimal
    payment_method: PaymentMethod
    transaction_date: datetime
    notes: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class PaymentAllocationResponse(BaseModel):
    item_id: UUID
    allocated_amount: Decimal


class CustomerPaymentResponse(BaseModel):
    transaction: CustomerTransactionResponse
    payment_amount: Decimal
    items: list[CustomerItemResponse]
    allocations: list[PaymentAllocationResponse]


class AddCustomerResponse(BaseModel):
    customer: CustomerResponse
    items: list[CustomerItemResponse]
    transaction_id: UUID | None = None
    payment_amount: Decimal


class ExistingCustomerResponse(BaseModel):
    customer: CustomerResponse
    items: list[CustomerItemResponse]
    transaction_id: UUID | None = None
    payment_amount: Decimal


class CustomerSummaryResponse(BaseModel):
    customer_id: UUID
    name: str
    phone_number: str
    total_paid_amount: Decimal
    total_due_amount: Decimal


class CustomerFinancialSummaryResponse(BaseModel):
    total_purchase_amount: Decimal = Field(ge=0)
    total_paid_amount: Decimal = Field(ge=0)
    total_due_amount: Decimal = Field(ge=0)
    items_count: int = Field(ge=0)


class CustomerDetailsResponse(BaseModel):
    customer: CustomerResponse
    items: list[CustomerItemResponse]
    summary: CustomerFinancialSummaryResponse

class CustomerListResponse(BaseModel):
    customers: list[CustomerSummaryResponse]
    total: int
    limit: int
    offset: int

class TransactionListItemResponse(BaseModel):
    transaction_id: UUID
    customer_id: UUID
    customer_name: str
    customer_phone_number: str
    amount: Decimal
    payment_method: PaymentMethod
    transaction_date: datetime
    notes: str | None = None


class TransactionListResponse(BaseModel):
    transactions: list[TransactionListItemResponse]
    total: int
    total_collected_amount: Decimal
    limit: int
    offset: int

class TransactionAllocationDetailResponse(BaseModel):
    item_id: UUID
    item_name: str
    weight_grams: Decimal
    total_amount: Decimal
    allocated_amount: Decimal


class TransactionDetailInfoResponse(BaseModel):
    id: UUID
    receipt_number: str
    customer_id: UUID
    customer_name: str
    customer_phone_number: str
    amount: Decimal
    payment_method: PaymentMethod
    transaction_date: datetime
    notes: str | None = None


class TransactionDetailResponse(BaseModel):
    transaction: TransactionDetailInfoResponse
    allocations: list[TransactionAllocationDetailResponse]