from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, Field


class AddWorkerRequest(BaseModel):
    phone_number: str = Field(
        pattern=r"^\+[1-9]\d{7,14}$",
        description="Worker's phone number in E.164 format",
    )


class AddWorkerResponse(BaseModel):
    id: UUID
    worker_user_id: UUID
    status: Literal["approved"]
    requested_at: datetime
