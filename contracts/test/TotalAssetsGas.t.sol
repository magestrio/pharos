// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test, console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {CapitalManager} from "../src/CapitalManager.sol";
import {IStrategyAdapter} from "../src/adapters/IStrategyAdapter.sol";

contract GasMockERC20 is ERC20 {
    constructor() ERC20("Mock", "MOCK") {}
}

/// @notice Trivial adapter for gas profiling: valueInUsdc() returns a constant,
/// no SLOAD past the function body itself. Establishes a *lower bound* on the cost
/// of totalAssetsUsdc() iteration per whitelisted adapter.
contract NoopAdapter is IStrategyAdapter {
    function deposit(uint256) external pure override {}
    function withdraw(uint256) external pure override returns (uint256) { return 0; }
    function balance() external pure override returns (uint256) { return 0; }
    function asset() external pure override returns (address) { return address(0); }
    function valueInUsdc() external pure override returns (uint256) { return 1e6; }
}

/// @notice Measures gas cost of CapitalManager.totalAssetsUsdc() at increasing
/// whitelist sizes so that we can set a sane soft-cap on adapter count. Each
/// iteration is SLOAD + EXTCALL — bounded but not free.
/// Run: forge test --match-contract TotalAssetsGas -vv
contract TotalAssetsGasTest is Test {
    CapitalManager vault;
    GasMockERC20 token;

    function setUp() public {
        token = new GasMockERC20();
        vault = new CapitalManager(IERC20(address(token)), address(this), address(0));
    }

    function _whitelistN(uint256 n) internal {
        for (uint256 i = 0; i < n; ++i) {
            NoopAdapter a = new NoopAdapter();
            vault.whitelistStrategy(address(a), true);
        }
    }

    function _measure(uint256 n) internal returns (uint256 gasUsed) {
        _whitelistN(n);
        uint256 g = gasleft();
        vault.totalAssetsUsdc();
        gasUsed = g - gasleft();
        console.log("totalAssetsUsdc() at N adapters:", n, "gas:", gasUsed);
    }

    function test_GasProfile_1Adapter() public {
        uint256 g = _measure(1);
        assertLt(g, 50_000, "single adapter should be cheap");
    }

    function test_GasProfile_5Adapters() public {
        _measure(5);
    }

    function test_GasProfile_10Adapters() public {
        _measure(10);
    }

    function test_GasProfile_20Adapters() public {
        uint256 g = _measure(20);
        // Soft cap: under 250k gas at N=20 keeps `totalAssetsUsdc()` viable
        // inside vUSDC paths that callers expect to be cheap.
        assertLt(g, 250_000, "20 adapters too expensive");
    }
}
