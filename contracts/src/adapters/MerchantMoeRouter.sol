// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ILBRouter} from "./interfaces/ILBRouter.sol";

// Internal swap helper — NOT an IStrategyAdapter
contract MerchantMoeRouter is Ownable {
    using SafeERC20 for IERC20;

    ILBRouter public immutable lbRouter;
    IERC20    public immutable weth;
    IERC20    public immutable usdc;

    uint256 public maxSlippageBps = 200; // 2% default

    constructor(address _lbRouter, address _weth, address _usdc, address _owner)
        Ownable(_owner)
    {
        lbRouter = ILBRouter(_lbRouter);
        weth     = IERC20(_weth);
        usdc     = IERC20(_usdc);
    }

    function setMaxSlippageBps(uint256 bps) external onlyOwner {
        require(bps <= 1000, "max 10%");
        maxSlippageBps = bps;
    }

    function swapWethToUsdc(uint256 amountIn, uint256 minOut, uint256 deadline)
        external returns (uint256)
    {
        weth.safeTransferFrom(msg.sender, address(this), amountIn);
        weth.forceApprove(address(lbRouter), amountIn);

        // TODO: verify bin step via Merchant Moe factory before Week 3 mainnet swap
        uint256[] memory binSteps = new uint256[](1);
        binSteps[0] = 0;

        ILBRouter.Version[] memory versions = new ILBRouter.Version[](1);
        versions[0] = ILBRouter.Version.V2_2;

        IERC20[] memory tokenPath = new IERC20[](2);
        tokenPath[0] = weth;
        tokenPath[1] = usdc;

        return lbRouter.swapExactTokensForTokens(
            amountIn,
            minOut,
            ILBRouter.Path({pairBinSteps: binSteps, versions: versions, tokenPath: tokenPath}),
            msg.sender,
            deadline
        );
    }

    function swapUsdcToWeth(uint256 amountIn, uint256 minOut, uint256 deadline)
        external returns (uint256)
    {
        usdc.safeTransferFrom(msg.sender, address(this), amountIn);
        usdc.forceApprove(address(lbRouter), amountIn);

        uint256[] memory binSteps = new uint256[](1);
        binSteps[0] = 0;

        ILBRouter.Version[] memory versions = new ILBRouter.Version[](1);
        versions[0] = ILBRouter.Version.V2_2;

        IERC20[] memory tokenPath = new IERC20[](2);
        tokenPath[0] = usdc;
        tokenPath[1] = weth;

        return lbRouter.swapExactTokensForTokens(
            amountIn,
            minOut,
            ILBRouter.Path({pairBinSteps: binSteps, versions: versions, tokenPath: tokenPath}),
            msg.sender,
            deadline
        );
    }
}
