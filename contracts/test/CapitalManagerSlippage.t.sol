// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {CapitalManager} from "../src/CapitalManager.sol";
import {IStrategyAdapter} from "../src/adapters/IStrategyAdapter.sol";

contract SlipMockERC20 is ERC20 {
    constructor() ERC20("Mock USDC", "mUSDC") {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

/// @notice Adapter that holds the underlying asset 1:1 but reports a haircut
/// in `valueInUsdc()` — simulates an oracle-mismatch or slippage scenario.
/// `lossBps` is the % of the held balance treated as "lost" for accounting.
contract LossyAdapter is IStrategyAdapter {
    IERC20  public immutable underlying;
    uint16  public lossBps;

    constructor(address _asset, uint16 _lossBps) {
        underlying = IERC20(_asset);
        lossBps = _lossBps;
    }

    function setLossBps(uint16 _bps) external { lossBps = _bps; }

    function deposit(uint256 amount) external override {
        underlying.transferFrom(msg.sender, address(this), amount);
    }
    function withdraw(uint256 amount) external override returns (uint256) {
        underlying.transfer(msg.sender, amount);
        return amount;
    }
    function balance() external view override returns (uint256) {
        return underlying.balanceOf(address(this));
    }
    function asset() external view override returns (address) {
        return address(underlying);
    }
    function valueInUsdc() external view override returns (uint256) {
        uint256 bal = underlying.balanceOf(address(this));
        return bal * (10000 - lossBps) / 10000;
    }
}

/// @notice Honest adapter — `valueInUsdc()` matches `balance()`.
contract HonestAdapter is IStrategyAdapter {
    IERC20 public immutable underlying;

    constructor(address _asset) { underlying = IERC20(_asset); }

    function deposit(uint256 amount) external override {
        underlying.transferFrom(msg.sender, address(this), amount);
    }
    function withdraw(uint256 amount) external override returns (uint256) {
        underlying.transfer(msg.sender, amount);
        return amount;
    }
    function balance() external view override returns (uint256) {
        return underlying.balanceOf(address(this));
    }
    function asset() external view override returns (address) {
        return address(underlying);
    }
    function valueInUsdc() external view override returns (uint256) {
        return underlying.balanceOf(address(this));
    }
}

contract CapitalManagerSlippageTest is Test {
    CapitalManager vault;
    SlipMockERC20 token;
    HonestAdapter honest;
    LossyAdapter lossy;

    address owner     = address(this);
    address agent     = address(0xCAFE);
    address vusdcRole = address(this);
    address user      = address(0xBEEF);

    function setUp() public {
        token  = new SlipMockERC20();
        vault  = new CapitalManager(IERC20(address(token)), owner, address(0));
        honest = new HonestAdapter(address(token));
        lossy  = new LossyAdapter(address(token), 2000); // 20% reported loss

        vault.whitelistStrategy(address(honest), true);
        vault.whitelistStrategy(address(lossy),  true);
        vault.setAgent(agent);
        vault.setVusdc(vusdcRole);

        // Seed the vault with 100e6 USDC via recordDeposit.
        token.mint(vusdcRole, 100e6);
        token.approve(address(vault), 100e6);
        vault.recordDeposit(100e6);
    }

    // ─── defaults ────────────────────────────────────────────────────────────

    function test_Defaults_AreConservative() public view {
        assertEq(vault.maxSlippageBps(), 100, "default global cap = 1%");
        assertEq(vault.maxPerCallLossBps(), 100, "default per-call cap = 1%");
    }

    // ─── setter access + bounds ──────────────────────────────────────────────

    function test_SetMaxSlippageBps_OnlyOwner() public {
        vm.expectRevert();
        vm.prank(user);
        vault.setMaxSlippageBps(500);
    }

    function test_SetMaxPerCallLossBps_OnlyOwner() public {
        vm.expectRevert();
        vm.prank(user);
        vault.setMaxPerCallLossBps(500);
    }

    function test_SetMaxSlippageBps_BoundedAtFullScale() public {
        vault.setMaxSlippageBps(10000); // boundary OK
        vm.expectRevert("bps > max");
        vault.setMaxSlippageBps(10001);
    }

    function test_SetMaxPerCallLossBps_BoundedAtFullScale() public {
        vault.setMaxPerCallLossBps(10000);
        vm.expectRevert("bps > max");
        vault.setMaxPerCallLossBps(10001);
    }

    // ─── happy path: honest adapter passes default caps ──────────────────────

    function test_Allocate_Honest_PassesGuards() public {
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = CapitalManager.AllocationCall({
            adapter: address(honest),
            kind: CapitalManager.AllocationCallKind.Deposit,
            amount: 50e6
        });

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);

        // No loss — totalAssetsUsdc preserved (50 free + 50 in honest adapter).
        assertEq(vault.totalAssetsUsdc(), 100e6);
    }

    // ─── per-call cap: lossy adapter is rejected by default 1% cap ──────────

    function test_PerCallCap_RejectsLossyDeposit_Default() public {
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = CapitalManager.AllocationCall({
            adapter: address(lossy),
            kind: CapitalManager.AllocationCallKind.Deposit,
            amount: 50e6
        });

        // After deposit: 50 free + lossy reports 50*0.8 = 40 → totalAssetsUsdc = 90.
        // Loss = 10/100 = 1000 bps. Default cap is 100 bps. Reverts.
        vm.expectRevert("per-call loss");
        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);
    }

    // ─── raising the cap allows the previously-rejected call ────────────────

    function test_PerCallCap_RaisedCap_AllowsLossyDeposit() public {
        vault.setMaxPerCallLossBps(2000); // 20%
        vault.setMaxSlippageBps(2000);    // 20% global, otherwise global cap kicks in

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = CapitalManager.AllocationCall({
            adapter: address(lossy),
            kind: CapitalManager.AllocationCallKind.Deposit,
            amount: 50e6
        });

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);

        assertEq(vault.totalAssetsUsdc(), 90e6);
    }

    // ─── global cap catches losses even when per-call cap is loose ──────────

    function test_GlobalCap_RejectsAggregateLoss() public {
        // Raise per-call cap so it doesn't fire first; global cap still 1%.
        vault.setMaxPerCallLossBps(5000); // 50%

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = CapitalManager.AllocationCall({
            adapter: address(lossy),
            kind: CapitalManager.AllocationCallKind.Deposit,
            amount: 50e6
        });

        // Per-call: -10% (within 50% cap, passes). Global: -10% (exceeds 1% cap, fails).
        vm.expectRevert("max slippage");
        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);
    }

    // ─── agent's minTotalAssetsAfter still enforced even if owner caps loose ─

    function test_AgentMinTotalAssetsAfter_StillEnforced() public {
        vault.setMaxPerCallLossBps(5000);
        vault.setMaxSlippageBps(5000);

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = CapitalManager.AllocationCall({
            adapter: address(lossy),
            kind: CapitalManager.AllocationCallKind.Deposit,
            amount: 50e6
        });

        // Agent insists on no loss → reverts despite loose owner caps.
        vm.expectRevert("slippage");
        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 100e6);
    }

    // ─── empty-vault edge case: pre-call cap is skipped when taBefore == 0 ───

    function test_EmptyVault_FirstAllocation_DoesNotRevert() public {
        // Drain the vault first so totalAssetsUsdc = 0 before allocation.
        vault.recordWithdraw(100e6, user);
        assertEq(vault.totalAssetsUsdc(), 0);

        // Re-seed tokens directly into the vault to give the deposit something to move.
        token.mint(address(vault), 50e6);

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = CapitalManager.AllocationCall({
            adapter: address(honest),
            kind: CapitalManager.AllocationCallKind.Deposit,
            amount: 50e6
        });

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);

        assertEq(vault.totalAssetsUsdc(), 50e6);
    }
}
