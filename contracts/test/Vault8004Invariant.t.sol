// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {Vault8004} from "../src/Vault8004.sol";
import {IStrategyAdapter} from "../src/adapters/IStrategyAdapter.sol";
import {Vault8004Handler, IMintable} from "./Vault8004Handler.sol";

contract InvMockERC20 is ERC20, IMintable {
    constructor() ERC20("Mock mETH", "mETH") {}
    function mint(address to, uint256 amount) external override { _mint(to, amount); }
}

contract InvMockStrategy is IStrategyAdapter {
    IERC20 private immutable _asset;

    constructor(address asset_) {
        _asset = IERC20(asset_);
    }

    function deposit(uint256 amount) external override {
        _asset.transferFrom(msg.sender, address(this), amount);
    }

    function withdraw(uint256 amount) external override returns (uint256) {
        _asset.transfer(msg.sender, amount);
        return amount;
    }

    function balance() external view override returns (uint256) {
        return _asset.balanceOf(address(this));
    }

    function asset() external view override returns (address) {
        return address(_asset);
    }
}

contract Vault8004InvariantTest is Test {
    Vault8004 vault;
    InvMockERC20 token;
    InvMockStrategy strategy;
    Vault8004Handler handler;

    address owner = address(0xBEEF);
    address agent = address(0xCAFE);

    function setUp() public {
        token    = new InvMockERC20();
        vault    = new Vault8004(IERC20(address(token)), owner, "Vault mETH", "vmETH");
        strategy = new InvMockStrategy(address(token));

        vm.startPrank(owner);
        vault.whitelistStrategy(address(strategy), true);
        vault.setCurrentStrategy(address(strategy));
        vault.setAgent(agent);
        vm.stopPrank();

        address[] memory users = new address[](3);
        users[0] = address(0xA11CE);
        users[1] = address(0xB0B);
        users[2] = address(0xCA42);

        handler = new Vault8004Handler(vault, token, strategy, owner, agent, users);

        // Direct fuzzer at the handler only.
        targetContract(address(handler));

        bytes4[] memory selectors = new bytes4[](5);
        selectors[0] = Vault8004Handler.deposit.selector;
        selectors[1] = Vault8004Handler.withdraw.selector;
        selectors[2] = Vault8004Handler.allocate.selector;
        selectors[3] = Vault8004Handler.deallocate.selector;
        selectors[4] = Vault8004Handler.accrueYield.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: selectors}));
    }

    // ── invariants ───────────────────────────────────────────────────────────

    /// totalAssets() always equals on-vault cash + strategy balance.
    function invariant_TotalAssetsConsistency() public view {
        uint256 cash = token.balanceOf(address(vault));
        uint256 strat = strategy.balance();
        assertEq(vault.totalAssets(), cash + strat, "totalAssets != cash + strategy");
    }

    /// currentStrategy must be address(0) or a whitelisted address.
    function invariant_WhitelistEnforcement() public view {
        address cs = vault.currentStrategy();
        if (cs != address(0)) {
            assertTrue(vault.whitelistedStrategies(cs), "currentStrategy not whitelisted");
        }
    }

    /// If the strategy is empty, totalAllocated must be zero (post-.5 bookkeeping).
    function invariant_EmptyStrategyZeroAllocated() public view {
        if (strategy.balance() == 0) {
            assertEq(vault.totalAllocated(), 0, "phantom allocation when strategy empty");
        }
    }

    /// Outstanding shares imply non-zero assets (no inflation-to-zero attack).
    function invariant_SharesBackedByAssets() public view {
        if (vault.totalSupply() > 0) {
            assertGt(vault.totalAssets(), 0, "shares exist but assets are zero");
        }
    }

    /// Conservation: totalAssets() + cash already paid out to users must cover
    /// all deposits ever made (since MockStrategy has no losses, only gains).
    /// Avoids the underflow that would happen if withdrawals > deposits due to yield.
    function invariant_PrincipalConservation() public view {
        assertGe(
            vault.totalAssets() + handler.ghost_totalWithdrawals(),
            handler.ghost_totalDeposits(),
            "system lost principal"
        );
    }
}
