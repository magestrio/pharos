// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ILBRouter} from "./interfaces/ILBRouter.sol";

/// @notice Internal swap helper — NOT an IStrategyAdapter.
///         Exposes USDC↔sUSDe round-trip via Merchant Moe Liquidity Book v2.2
///         along a fixed path (verified live 2026-05-19, see notes/architecture.md
///         "USDC→sUSDe swap path"):
///             USDC ─(bs=1)─→ USDe ─(bs=5)─→ sUSDe
///         and the reverse for swapFromSusde.
contract MerchantMoeRouter is Ownable {
    using SafeERC20 for IERC20;

    // ─── Immutables ──────────────────────────────────────────────────────────

    ILBRouter public immutable lbRouter;
    IERC20    public immutable usdc;
    IERC20    public immutable usde;
    IERC20    public immutable susde;

    // ─── Storage ─────────────────────────────────────────────────────────────

    uint256 public maxSlippageBps = 200; // 2% default; advisory only — actual slippage gate is minOut

    // ─── Constants (path) ────────────────────────────────────────────────────

    uint256 private constant BIN_USDC_USDE = 1;
    uint256 private constant BIN_USDE_SUSDE = 5;

    constructor(
        address _lbRouter,
        address _usdc,
        address _usde,
        address _susde,
        address _owner
    ) Ownable(_owner) {
        lbRouter = ILBRouter(_lbRouter);
        usdc     = IERC20(_usdc);
        usde     = IERC20(_usde);
        susde    = IERC20(_susde);
    }

    function setMaxSlippageBps(uint256 bps) external onlyOwner {
        require(bps <= 1000, "max 10%");
        maxSlippageBps = bps;
    }

    // ─── External ────────────────────────────────────────────────────────────

    /// @notice Swap USDC → USDe → sUSDe via Merchant Moe LB v2.2.
    /// @return susdeOut Amount of sUSDe received by `to`.
    function swapToSusde(
        uint256 usdcIn,
        uint256 minSusdeOut,
        uint256 deadline,
        address to
    ) external returns (uint256 susdeOut) {
        usdc.safeTransferFrom(msg.sender, address(this), usdcIn);
        usdc.forceApprove(address(lbRouter), usdcIn);

        susdeOut = lbRouter.swapExactTokensForTokens(
            usdcIn,
            minSusdeOut,
            _buildPath(usdc, usde, susde, BIN_USDC_USDE, BIN_USDE_SUSDE),
            to,
            deadline
        );

        usdc.forceApprove(address(lbRouter), 0);
    }

    /// @notice Swap sUSDe → USDe → USDC via Merchant Moe LB v2.2.
    /// @return usdcOut Amount of USDC received by `to`.
    function swapFromSusde(
        uint256 susdeIn,
        uint256 minUsdcOut,
        uint256 deadline,
        address to
    ) external returns (uint256 usdcOut) {
        susde.safeTransferFrom(msg.sender, address(this), susdeIn);
        susde.forceApprove(address(lbRouter), susdeIn);

        usdcOut = lbRouter.swapExactTokensForTokens(
            susdeIn,
            minUsdcOut,
            _buildPath(susde, usde, usdc, BIN_USDE_SUSDE, BIN_USDC_USDE),
            to,
            deadline
        );

        susde.forceApprove(address(lbRouter), 0);
    }

    // ─── Internal ────────────────────────────────────────────────────────────

    function _buildPath(
        IERC20 t0,
        IERC20 t1,
        IERC20 t2,
        uint256 binStep0,
        uint256 binStep1
    ) internal pure returns (ILBRouter.Path memory path) {
        uint256[] memory binSteps = new uint256[](2);
        binSteps[0] = binStep0;
        binSteps[1] = binStep1;

        ILBRouter.Version[] memory versions = new ILBRouter.Version[](2);
        versions[0] = ILBRouter.Version.V2_2;
        versions[1] = ILBRouter.Version.V2_2;

        IERC20[] memory tokenPath = new IERC20[](3);
        tokenPath[0] = t0;
        tokenPath[1] = t1;
        tokenPath[2] = t2;

        path = ILBRouter.Path({pairBinSteps: binSteps, versions: versions, tokenPath: tokenPath});
    }
}
