// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {IStrategyAdapter} from "./IStrategyAdapter.sol";
import {IAaveV3Pool} from "./interfaces/IAaveV3Pool.sol";

contract AaveV3UsdcAdapter is IStrategyAdapter, Ownable {
    using SafeERC20 for IERC20;

    IAaveV3Pool public immutable aavePool;
    IERC20      public immutable usdc;
    IERC20      public immutable aUsdc;
    address     public immutable vault;

    modifier onlyVault() {
        require(msg.sender == vault, "not vault");
        _;
    }

    constructor(address _aavePool, address _usdc, address _aUsdc, address _vault, address _owner)
        Ownable(_owner)
    {
        aavePool = IAaveV3Pool(_aavePool);
        usdc     = IERC20(_usdc);
        aUsdc    = IERC20(_aUsdc);
        vault    = _vault;
    }

    function asset() external view returns (address) {
        return address(usdc);
    }

    function deposit(uint256 amount) external onlyVault {
        usdc.safeTransferFrom(vault, address(this), amount);
        usdc.forceApprove(address(aavePool), amount);
        aavePool.supply(address(usdc), amount, address(this), 0);
    }

    function withdraw(uint256 amount) external onlyVault {
        uint256 received = aavePool.withdraw(address(usdc), amount, address(this));
        usdc.safeTransfer(vault, received);
    }

    function balance() external view returns (uint256) {
        return aUsdc.balanceOf(address(this));
    }
}
