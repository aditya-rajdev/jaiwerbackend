from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ShopProfileUpdateRequest(BaseModel):
    shop_name: str | None = Field(default=None, min_length=2, max_length=150)
    shop_address: str | None = Field(default=None, min_length=5, max_length=300)
    city: str | None = Field(default=None, min_length=2, max_length=100)
    state: str | None = Field(default=None, min_length=2, max_length=100)
    pincode: str | None = Field(default=None, max_length=6)

    gstin: str | None = Field(default=None, max_length=15)
    shop_logo_url: str | None = None
    receipt_footer: str | None = Field(default=None, max_length=250)

    @field_validator(
        "shop_name",
        "shop_address",
        "city",
        "state",
        "pincode",
        "gstin",
        "shop_logo_url",
        "receipt_footer",
    )
    @classmethod
    def strip_values(cls, value):
        if value is None:
            return None

        value = value.strip()

        if not value:
            return None

        return value

    @field_validator("pincode")
    @classmethod
    def validate_pincode(cls, value):
        if value is not None and not (
            value.isdigit() and len(value) == 6
        ):
            raise ValueError("PIN code must contain exactly 6 digits.")

        return value

    @field_validator("gstin")
    @classmethod
    def validate_gstin(cls, value):
        if value is not None:
            value = value.upper()

            if len(value) != 15:
                raise ValueError("GSTIN must contain exactly 15 characters.")

        return value

    @model_validator(mode="after")
    def validate_update(self):
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update.")

        return self


class ShopProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    shop_name: str | None = None
    shop_address: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    gstin: str | None = None
    shop_logo_url: str | None = None
    receipt_footer: str | None = None

class ShopProfileCompletionResponse(BaseModel):
    is_complete: bool
    missing_fields: list[str]