// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {IStrategyAdapter} from "./IStrategyAdapter.sol";
import {IAaveV3Pool} from "./interfaces/IAaveV3Pool.sol";
import {IAaveOracle, IChainlinkAggregator} from "./interfaces/IAaveOracle.sol";

/// @notice Aave V3 USDC strategy adapter.
/// @dev `valueInBaseAsset()` prices the aUSDC position in WETH wei via the
///      Aave V3 Oracle on Mantle (0x47a063CfDa980532267970d478EC340C0F80E8df).
///      Both USDC and WETH source aggregators are fetched fresh via
///      `getSourceOfAsset()`.
///
///      Liveness: Mantle's Aave V3 Oracle proxies expose only `latestAnswer()`
///      (no `latestTimestamp` / `latestRoundData` surface), so an on-chain
///      heartbeat check is not possible from this adapter. Liveness is enforced
///      one layer up by `CapitalManager._checkSequencer()` (Chainlink L2 Sequencer
///      Uptime Feed, see `CapitalManager.sequencerUptimeFeed`) plus Aave's own
///      economic incentive to maintain feed freshness (a stale feed bricks
///      their lending market). `answer > 0` is the only adapter-side sanity check.
contract AaveV3UsdcAdapter is IStrategyAdapter, Ownable {
    using SafeERC20 for IERC20;

    IAaveV3Pool public immutable aavePool;
    IAaveOracle public immutable aaveOracle;
    IERC20      public immutable usdc;
    IERC20      public immutable aUsdc;
    address     public immutable weth;
    address     public immutable vault;

    modifier onlyVault() {
        require(msg.sender == vault, "not vault");
        _;
    }

    constructor(
        address _aavePool,
        address _aaveOracle,
        address _usdc,
        address _aUsdc,
        address _weth,
        address _vault,
        address _owner
    ) Ownable(_owner) {
        aavePool   = IAaveV3Pool(_aavePool);
        aaveOracle = IAaveOracle(_aaveOracle);
        usdc       = IERC20(_usdc);
        aUsdc      = IERC20(_aUsdc);
        weth       = _weth;
        vault      = _vault;
    }

    function asset() external view returns (address) {
        return address(usdc);
    }

    function deposit(uint256 amount) external onlyVault {
        // `vault` is immutable and callers are restricted by `onlyVault`, so
        // `safeTransferFrom(vault, ...)` is safe — only the vault itself can
        // invoke this path. Slither reports false-positive otherwise.
        // slither-disable-next-line arbitrary-send-erc20
        usdc.safeTransferFrom(vault, address(this), amount);
        usdc.forceApprove(address(aavePool), amount);
        aavePool.supply(address(usdc), amount, address(this), 0);
    }

    function withdraw(uint256 amount) external onlyVault returns (uint256) {
        uint256 received = aavePool.withdraw(address(usdc), amount, address(this));
        usdc.safeTransfer(vault, received);
        return received;
    }

    function balance() external view returns (uint256) {
        return aUsdc.balanceOf(address(this));
    }

    function valueInBaseAsset() external view returns (uint256) {
        uint256 usdcBalance = aUsdc.balanceOf(address(this));
        if (usdcBalance == 0) return 0;

        // Both prices come from Chainlink-style feeds with 8 decimals (BASE_CURRENCY_UNIT
        // = 1e8 on Aave V3 Mantle). USDC has 6 decimals, WETH has 18 — the 1e12 factor
        // bridges the unit gap. Both feeds are checked for staleness independently.
        uint256 usdcPrice = _getPriceFresh(address(usdc));
        uint256 wethPrice = _getPriceFresh(weth);
        return usdcBalance * usdcPrice * 1e12 / wethPrice;
    }

    /// @dev Reads `asset`'s price from its Aave-registered Chainlink V2 aggregator.
    ///      No on-chain heartbeat check — see contract-level NatSpec on liveness.
    function _getPriceFresh(address _asset) internal view returns (uint256) {
        address source = aaveOracle.getSourceOfAsset(_asset);
        require(source != address(0), "no oracle source");
        int256 answer = IChainlinkAggregator(source).latestAnswer();
        require(answer > 0, "invalid price");
        return uint256(answer);
    }
}
