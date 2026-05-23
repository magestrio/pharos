// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {IStrategyAdapter} from "./IStrategyAdapter.sol";
import {IAaveV3Pool} from "./interfaces/IAaveV3Pool.sol";
import {IAaveOracle, IChainlinkAggregator} from "./interfaces/IAaveOracle.sol";

/// @notice Aave V3 sUSDe strategy adapter — supplies sUSDe into Aave on Mantle
///         and earns the supply APY in asUSDe.
/// @dev `valueInBaseAsset()` prices the asUSDe position in WETH wei via the
///      Aave V3 Oracle on Mantle (0x47a063CfDa980532267970d478EC340C0F80E8df).
///      sUSDe is a listed reserve on Aave V3 Mantle, so the oracle exposes a
///      direct sUSDe→USD source via `getSourceOfAsset(sUSDe)`. WETH→USD comes
///      from its own source.
///
///      Liveness: Mantle's Aave V3 Oracle proxies expose only `latestAnswer()`
///      (no `latestTimestamp` / `latestRoundData` surface), so an on-chain
///      heartbeat check is not possible from this adapter. Liveness is enforced
///      one layer up by `Vault8004._checkSequencer()` plus Aave's own economic
///      incentive to maintain feed freshness. `answer > 0` is the only
///      adapter-side sanity check.
contract AaveV3SusdeAdapter is IStrategyAdapter, Ownable {
    using SafeERC20 for IERC20;

    IAaveV3Pool public immutable aavePool;
    IAaveOracle public immutable aaveOracle;
    IERC20      public immutable sUsde;
    IERC20      public immutable aSusde;
    address     public immutable weth;
    address     public immutable vault;

    modifier onlyVault() {
        require(msg.sender == vault, "not vault");
        _;
    }

    constructor(
        address _aavePool,
        address _aaveOracle,
        address _sUsde,
        address _aSusde,
        address _weth,
        address _vault,
        address _owner
    ) Ownable(_owner) {
        aavePool   = IAaveV3Pool(_aavePool);
        aaveOracle = IAaveOracle(_aaveOracle);
        sUsde      = IERC20(_sUsde);
        aSusde     = IERC20(_aSusde);
        weth       = _weth;
        vault      = _vault;
    }

    function asset() external view returns (address) {
        return address(sUsde);
    }

    function deposit(uint256 amount) external onlyVault {
        // slither-disable-next-line arbitrary-send-erc20
        sUsde.safeTransferFrom(vault, address(this), amount);
        sUsde.forceApprove(address(aavePool), amount);
        aavePool.supply(address(sUsde), amount, address(this), 0);
    }

    function withdraw(uint256 amount) external onlyVault returns (uint256) {
        uint256 received = aavePool.withdraw(address(sUsde), amount, address(this));
        sUsde.safeTransfer(vault, received);
        return received;
    }

    function balance() external view returns (uint256) {
        return aSusde.balanceOf(address(this));
    }

    function valueInBaseAsset() external view returns (uint256) {
        uint256 susdeBalance = aSusde.balanceOf(address(this));
        if (susdeBalance == 0) return 0;

        // sUSDe is 18 decimals, WETH is 18 decimals — no decimal bridging needed.
        // Both Chainlink feeds report at 1e8 (Aave V3 Mantle BASE_CURRENCY_UNIT).
        uint256 susdePrice = _getPriceFresh(address(sUsde));
        uint256 wethPrice  = _getPriceFresh(weth);
        return susdeBalance * susdePrice / wethPrice;
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
