"""Request bodies of the REST API (SPEC-v4 4.8). Money values are strings or whole numbers, never floats."""

from decimal import Decimal
from typing import Annotated, Any

from pydantic import BeforeValidator, ConfigDict, Field

from api.schemas.base import Schema


def no_float(value: Any) -> Any:
    if isinstance(value, float):  # 0.1 as a JSON number is not 0.1
        raise ValueError("send the value as a string")
    return value


Amount = Annotated[Decimal, BeforeValidator(no_float), Field(ge=0, max_digits=60, decimal_places=18)]


class Input(Schema):
    model_config = ConfigDict(extra="forbid")


class PoolCreate(Input):
    wallet_id: str = Field(min_length=1)
    chain: str
    store: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")  # store key of the signer config
    enabled: bool = False
    min_withdraw: Amount = Decimal(0)


class PoolUpdate(Input):
    """wallet_id, chain and store are fixed: addresses were given out for them."""

    enabled: bool | None = None
    min_withdraw: Amount | None = None


class Settings(Input):
    ready_target: int | None = Field(None, ge=0, le=1000)
    max_invoice_usd: Amount | None = None
    speed_factor: Annotated[Decimal, BeforeValidator(no_float), Field(ge=1, le=5)] | None = None
