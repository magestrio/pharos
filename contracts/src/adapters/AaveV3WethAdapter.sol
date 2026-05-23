// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {IStrategyAdapter} from "./IStrategyAdapter.sol";
import {IAaveV3Pool} from "./interfaces/IAaveV3Pool.sol";

contract AaveV3WethAdapter is IStrategyAdapter, Ownable {
    using SafeERC20 for IERC20;

    IAaveV3Pool public immutable aavePool;
    IERC20      public immutable weth;
    IERC20      public immutable aWeth;
    address     public immutable vault;

    modifier onlyVault() {
        require(msg.sender == vault, "not vault");
        _;
    }

    constructor(address _aavePool, address _weth, address _aWeth, address _vault, address _owner)
        Ownable(_owner)
    {
        aavePool = IAaveV3Pool(_aavePool);
        weth     = IERC20(_weth);
        aWeth    = IERC20(_aWeth);
        vault    = _vault;
    }

    function asset() external view returns (address) {
        return address(weth);
    }

    function deposit(uint256 amount) external onlyVault {
        // `vault` is immutable and callers are restricted by `onlyVault`, so
        // `safeTransferFrom(vault, ...)` is safe — only the vault itself can
        // invoke this path. Slither reports false-positive otherwise.
        // slither-disable-next-line arbitrary-send-erc20
        weth.safeTransferFrom(vault, address(this), amount);
        weth.forceApprove(address(aavePool), amount);
        aavePool.supply(address(weth), amount, address(this), 0);
    }

    function withdraw(uint256 amount) external onlyVault returns (uint256) {
        uint256 received = aavePool.withdraw(address(weth), amount, address(this));
        weth.safeTransfer(vault, received);
        return received;
    }

    function balance() external view returns (uint256) {
        return aWeth.balanceOf(address(this));
    }
}
