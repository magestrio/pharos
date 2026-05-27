from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from agent.bybit_oracle.bybit_client import FlexibleEarnProduct
from agent.bybit_oracle.product_picker import (
    FlexibleUsdcPicker,
    NoProductAvailable,
    PickedProduct,
)


def _product(
    pid: str, apr: str | None = "1.0", coin: str = "USDC"
) -> FlexibleEarnProduct:
    data: dict[str, object] = {
        "productId": pid,
        "coin": coin,
        "category": "FlexibleSaving",
    }
    if apr is not None:
        data["estimateApr"] = apr
    return FlexibleEarnProduct.model_validate(data)


def _client_returning(products: list[FlexibleEarnProduct]) -> AsyncMock:
    client = AsyncMock()
    client.list_earn_products.return_value = products
    return client


@pytest.mark.asyncio
async def test_picks_highest_apr():
    client = _client_returning(
        [
            _product("low", "2.5"),
            _product("high", "5.7"),
            _product("mid", "4.1"),
        ]
    )
    picked = await FlexibleUsdcPicker().pick(client)
    assert picked.product_id == "high"
    assert picked.target_coin == "USDC"
    assert picked.estimated_apr == Decimal("5.7")


@pytest.mark.asyncio
async def test_picks_single_product():
    client = _client_returning([_product("only", "3.0")])
    picked = await FlexibleUsdcPicker().pick(client)
    assert picked == PickedProduct(
        product_id="only", target_coin="USDC", estimated_apr=Decimal("3.0")
    )


@pytest.mark.asyncio
async def test_missing_apr_treated_as_zero():
    """Bybit sometimes omits estimateApr on new/maintenance products. Those
    must rank LAST, never above a known-rate product.
    """
    client = _client_returning(
        [
            _product("known", "2.0"),
            _product("opaque", None),
        ]
    )
    picked = await FlexibleUsdcPicker().pick(client)
    assert picked.product_id == "known"


@pytest.mark.asyncio
async def test_malformed_apr_treated_as_zero():
    client = _client_returning(
        [
            _product("good", "1.5"),
            _product("garbage", "not-a-number"),
        ]
    )
    picked = await FlexibleUsdcPicker().pick(client)
    assert picked.product_id == "good"


@pytest.mark.asyncio
async def test_empty_list_raises_no_product_available():
    client = _client_returning([])
    with pytest.raises(NoProductAvailable, match="FlexibleSaving"):
        await FlexibleUsdcPicker().pick(client)


@pytest.mark.asyncio
async def test_calls_client_with_correct_filters():
    client = _client_returning([_product("p", "1.0")])
    await FlexibleUsdcPicker().pick(client)
    client.list_earn_products.assert_awaited_once_with(
        category="FlexibleSaving", coin="USDC"
    )


@pytest.mark.asyncio
async def test_only_opaque_products_still_picks_one():
    """If every product has missing APR, the picker should still return ONE
    (deterministically, the first one by Bybit's order) rather than fail —
    the operator gets a stake at the listed product instead of a stall.
    """
    client = _client_returning(
        [
            _product("p1", None),
            _product("p2", None),
        ]
    )
    picked = await FlexibleUsdcPicker().pick(client)
    assert picked.product_id == "p1"
    assert picked.estimated_apr == Decimal(0)
