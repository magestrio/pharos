// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {Vault8004} from "../src/Vault8004.sol";
import {IStrategyAdapter} from "../src/adapters/IStrategyAdapter.sol";

contract SlipMockERC20 is ERC20 {
    constructor() ERC20("Mock", "MOCK") {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

/// @notice Adapter that holds the underlying asset 1:1 but reports a haircut
/// in `valueInBaseAsset()` — simulates an oracle-mismatch or slippage scenario.
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
    function valueInBaseAsset() external view override returns (uint256) {
        uint256 bal = underlying.balanceOf(address(this));
        return bal * (10000 - lossBps) / 10000;
    }
}

/// @notice Honest adapter — `valueInBaseAsset()` matches `balance()`.
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
    function valueInBaseAsset() external view override returns (uint256) {
        return underlying.balanceOf(address(this));
    }
}

contract Vault8004SlippageTest is Test {
    Vault8004 vault;
    SlipMockERC20 token;
    HonestAdapter honest;
    LossyAdapter lossy;

    address owner = address(this);
    address agent = address(0xCAFE);
    address user  = address(0xBEEF);

    function setUp() public {
        token  = new SlipMockERC20();
        vault  = new Vault8004(IERC20(address(token)), owner, "V", "v", address(0));
        honest = new HonestAdapter(address(token));
        lossy  = new LossyAdapter(address(token), 2000); // 20% reported loss

        vault.whitelistStrategy(address(honest), true);
        vault.whitelistStrategy(address(lossy),  true);
        vault.setAgent(agent);

        // Seed the vault with 100 tokens (~user deposit).
        token.mint(user, 100 ether);
        vm.startPrank(user);
        token.approve(address(vault), 100 ether);
        vault.deposit(100 ether, user);
        vm.stopPrank();
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
        Vault8004.AllocationCall[] memory calls = new Vault8004.AllocationCall[](1);
        calls[0] = Vault8004.AllocationCall({
            adapter: address(honest),
            kind: Vault8004.AllocationCallKind.Deposit,
            amount: 50 ether
        });

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);

        // No loss — totalAssets preserved (50 free + 50 in honest adapter).
        assertEq(vault.totalAssets(), 100 ether);
    }

    // ─── per-call cap: lossy adapter is rejected by default 1% cap ──────────

    function test_PerCallCap_RejectsLossyDeposit_Default() public {
        Vault8004.AllocationCall[] memory calls = new Vault8004.AllocationCall[](1);
        calls[0] = Vault8004.AllocationCall({
            adapter: address(lossy),
            kind: Vault8004.AllocationCallKind.Deposit,
            amount: 50 ether
        });

        // After deposit: 50 free + lossy reports 50*0.8 = 40 → totalAssets = 90.
        // Loss = 10/100 = 1000 bps. Default cap is 100 bps. Reverts.
        vm.expectRevert("per-call loss");
        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);
    }

    // ─── raising the cap allows the previously-rejected call ────────────────

    function test_PerCallCap_RaisedCap_AllowsLossyDeposit() public {
        vault.setMaxPerCallLossBps(2000); // 20%
        vault.setMaxSlippageBps(2000);    // 20% global, otherwise global cap kicks in

        Vault8004.AllocationCall[] memory calls = new Vault8004.AllocationCall[](1);
        calls[0] = Vault8004.AllocationCall({
            adapter: address(lossy),
            kind: Vault8004.AllocationCallKind.Deposit,
            amount: 50 ether
        });

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);

        assertEq(vault.totalAssets(), 90 ether);
    }

    // ─── global cap catches losses even when per-call cap is loose ──────────

    function test_GlobalCap_RejectsAggregateLoss() public {
        // Raise per-call cap so it doesn't fire first; global cap still 1%.
        vault.setMaxPerCallLossBps(5000); // 50%

        Vault8004.AllocationCall[] memory calls = new Vault8004.AllocationCall[](1);
        calls[0] = Vault8004.AllocationCall({
            adapter: address(lossy),
            kind: Vault8004.AllocationCallKind.Deposit,
            amount: 50 ether
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

        Vault8004.AllocationCall[] memory calls = new Vault8004.AllocationCall[](1);
        calls[0] = Vault8004.AllocationCall({
            adapter: address(lossy),
            kind: Vault8004.AllocationCallKind.Deposit,
            amount: 50 ether
        });

        // Agent insists on no loss → reverts despite loose owner caps.
        vm.expectRevert("slippage");
        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 100 ether);
    }

    // ─── empty-vault edge case: pre-call cap is skipped when taBefore == 0 ───

    function test_EmptyVault_FirstAllocation_DoesNotRevert() public {
        // Drain the vault first so totalAssets = 0 before allocation.
        vm.prank(user);
        vault.withdraw(100 ether, user, user);
        assertEq(vault.totalAssets(), 0);

        // Re-seed tokens directly into the vault to give the deposit something to move.
        token.mint(address(vault), 50 ether);

        Vault8004.AllocationCall[] memory calls = new Vault8004.AllocationCall[](1);
        calls[0] = Vault8004.AllocationCall({
            adapter: address(honest),
            kind: Vault8004.AllocationCallKind.Deposit,
            amount: 50 ether
        });

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);

        assertEq(vault.totalAssets(), 50 ether);
    }
}
