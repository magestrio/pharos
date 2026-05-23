// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {BybitAttestor} from "../src/adapters/BybitAttestor.sol";

contract MockERC20 is ERC20 {
    constructor(string memory name, string memory symbol) ERC20(name, symbol) {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

contract BybitAttestorTest is Test {
    MockERC20      usdc;
    BybitAttestor  attestorContract;

    address vault    = address(this);
    address attestor = address(0xA77E);
    address owner    = address(0xBEEF);

    // Mirrors the event declarations in BybitAttestor.sol — required for
    // vm.expectEmit matching.
    event DepositRequested(uint256 indexed txId, uint256 amount);
    event DepositConfirmed(uint256 indexed txId, uint256 newAttestedBalance);
    event WithdrawRequested(uint256 indexed txId, uint256 amount);
    event WithdrawConfirmed(uint256 indexed txId, uint256 amount);
    event BalanceUpdated(uint256 newAttestedBalance, uint256 timestamp);

    function setUp() public {
        usdc = new MockERC20("USD Coin", "USDC");
        attestorContract = new BybitAttestor(
            address(usdc),
            vault,
            attestor,
            owner
        );
    }

    // -----------------------------------------------------------------
    // Deploy / immutables
    // -----------------------------------------------------------------

    function test_Deploy() public view {
        assertEq(address(attestorContract.usdc()), address(usdc));
        assertEq(attestorContract.vault(), vault);
        assertEq(attestorContract.attestor(), attestor);
        assertEq(attestorContract.owner(), owner);
        assertEq(attestorContract.asset(), address(usdc));
        assertEq(attestorContract.HEARTBEAT(), 24 hours);
        assertEq(attestorContract.attestedBalance(), 0);
        assertEq(attestorContract.totalPendingDeposits(), 0);
        assertEq(attestorContract.nextTxId(), 0);
        assertEq(attestorContract.lastAttestationTime(), 0);
    }

    // -----------------------------------------------------------------
    // Access control
    // -----------------------------------------------------------------

    function test_OnlyVault_Deposit() public {
        vm.prank(address(0xDEAD));
        vm.expectRevert("not vault");
        attestorContract.deposit(1e6);
    }

    function test_OnlyVault_Withdraw() public {
        vm.prank(address(0xDEAD));
        vm.expectRevert("not vault");
        attestorContract.withdraw(1e6);
    }

    function test_OnlyAttestor_ConfirmDeposit() public {
        vm.prank(address(0xDEAD));
        vm.expectRevert("not attestor");
        attestorContract.confirmDeposit(0, 1e6);
    }

    function test_OnlyAttestor_ConfirmWithdraw() public {
        vm.prank(address(0xDEAD));
        vm.expectRevert("not attestor");
        attestorContract.confirmWithdraw(0, 1e6);
    }

    function test_OnlyAttestor_UpdateBalance() public {
        vm.prank(address(0xDEAD));
        vm.expectRevert("not attestor");
        attestorContract.updateBalance(1e6);
    }

    // -----------------------------------------------------------------
    // deposit()
    // -----------------------------------------------------------------

    function test_Deposit_PullsUsdc_RecordsPending_EmitsEvent() public {
        uint256 amount = 1_000e6;
        usdc.mint(vault, amount);
        usdc.approve(address(attestorContract), amount);

        vm.expectEmit(true, false, false, true, address(attestorContract));
        emit DepositRequested(0, amount);

        attestorContract.deposit(amount);

        assertEq(usdc.balanceOf(vault), 0);
        assertEq(usdc.balanceOf(address(attestorContract)), amount, "escrowed");
        assertEq(attestorContract.pendingDeposits(0), amount);
        assertEq(attestorContract.totalPendingDeposits(), amount);
        assertEq(attestorContract.nextTxId(), 1, "txId advanced");
    }

    function test_Deposit_MultipleAssignDistinctTxIds() public {
        usdc.mint(vault, 3_000e6);
        usdc.approve(address(attestorContract), 3_000e6);

        attestorContract.deposit(1_000e6);
        attestorContract.deposit(2_000e6);

        assertEq(attestorContract.pendingDeposits(0), 1_000e6);
        assertEq(attestorContract.pendingDeposits(1), 2_000e6);
        assertEq(attestorContract.totalPendingDeposits(), 3_000e6);
        assertEq(attestorContract.nextTxId(), 2);
    }

    // -----------------------------------------------------------------
    // confirmDeposit()
    // -----------------------------------------------------------------

    function _seedDeposit(uint256 amount) internal returns (uint256 txId) {
        usdc.mint(vault, amount);
        usdc.approve(address(attestorContract), amount);
        txId = attestorContract.nextTxId();
        attestorContract.deposit(amount);
    }

    function test_ConfirmDeposit_HappyPath() public {
        uint256 amount = 1_000e6;
        uint256 txId = _seedDeposit(amount);

        // First-ever confirm: prev attestedBalance is 0, so sanity floor is
        // 0 + amount/2 = 500e6. Push exactly the deposited value.
        uint256 newBalance = amount;

        vm.warp(1_000_000);

        vm.expectEmit(true, false, false, true, address(attestorContract));
        emit DepositConfirmed(txId, newBalance);

        vm.prank(attestor);
        attestorContract.confirmDeposit(txId, newBalance);

        assertEq(attestorContract.pendingDeposits(txId), 0);
        assertEq(attestorContract.totalPendingDeposits(), 0);
        assertEq(attestorContract.attestedBalance(), newBalance);
        assertEq(attestorContract.lastAttestationTime(), 1_000_000);
        assertEq(usdc.balanceOf(attestor), amount, "USDC forwarded to attestor");
        assertEq(usdc.balanceOf(address(attestorContract)), 0);
    }

    function test_ConfirmDeposit_RevertOnNoPending() public {
        vm.prank(attestor);
        vm.expectRevert("no pending deposit");
        attestorContract.confirmDeposit(42, 1e6);
    }

    function test_ConfirmDeposit_RevertOnDoubleConfirm() public {
        uint256 amount = 1_000e6;
        uint256 txId = _seedDeposit(amount);

        vm.prank(attestor);
        attestorContract.confirmDeposit(txId, amount);

        vm.prank(attestor);
        vm.expectRevert("no pending deposit");
        attestorContract.confirmDeposit(txId, amount);
    }

    function test_ConfirmDeposit_RevertOnSanityFloor() public {
        uint256 amount = 1_000e6;
        uint256 txId = _seedDeposit(amount);

        // Sanity floor = attestedBalance(0) + amount/2 = 500e6. Pushing
        // 499e6 must revert.
        vm.prank(attestor);
        vm.expectRevert("attested balance below sanity floor");
        attestorContract.confirmDeposit(txId, 499e6);
    }

    function test_ConfirmDeposit_AcceptsExactlyAtFloor() public {
        uint256 amount = 1_000e6;
        uint256 txId = _seedDeposit(amount);

        vm.prank(attestor);
        attestorContract.confirmDeposit(txId, 500e6);
        assertEq(attestorContract.attestedBalance(), 500e6);
    }

    // -----------------------------------------------------------------
    // withdraw() — async
    // -----------------------------------------------------------------

    function test_Withdraw_RecordsPending_ReturnsZero_EmitsEvent() public {
        uint256 amount = 1_000e6;

        vm.expectEmit(true, false, false, true, address(attestorContract));
        emit WithdrawRequested(0, amount);

        uint256 received = attestorContract.withdraw(amount);

        assertEq(received, 0, "async withdraw returns 0");
        assertEq(attestorContract.pendingWithdraws(0), amount);
        assertEq(attestorContract.nextTxId(), 1);
        assertEq(usdc.balanceOf(vault), 0, "no USDC moved yet");
    }

    function test_Withdraw_RevertOnZero() public {
        vm.expectRevert("amount = 0");
        attestorContract.withdraw(0);
    }

    // -----------------------------------------------------------------
    // confirmWithdraw()
    // -----------------------------------------------------------------

    function _seedWithdraw(uint256 amount) internal returns (uint256 txId) {
        txId = attestorContract.nextTxId();
        attestorContract.withdraw(amount);
    }

    function test_ConfirmWithdraw_HappyPath_TwoHop() public {
        uint256 expected = 1_000e6;
        uint256 delivered = 1_000e6;
        uint256 txId = _seedWithdraw(expected);

        // Attestor must hold USDC and approve the contract.
        usdc.mint(attestor, delivered);
        vm.prank(attestor);
        usdc.approve(address(attestorContract), delivered);

        vm.warp(2_000_000);

        vm.expectEmit(true, false, false, true, address(attestorContract));
        emit WithdrawConfirmed(txId, delivered);

        vm.prank(attestor);
        attestorContract.confirmWithdraw(txId, delivered);

        assertEq(attestorContract.pendingWithdraws(txId), 0);
        assertEq(usdc.balanceOf(attestor), 0, "attestor paid");
        assertEq(usdc.balanceOf(address(attestorContract)), 0, "no escrow leak");
        assertEq(usdc.balanceOf(vault), delivered, "vault received");
        assertEq(attestorContract.lastAttestationTime(), 2_000_000);
    }

    function test_ConfirmWithdraw_RevertOnNoPending() public {
        vm.prank(attestor);
        vm.expectRevert("no pending withdraw");
        attestorContract.confirmWithdraw(42, 1e6);
    }

    function test_ConfirmWithdraw_RevertOnZero() public {
        uint256 expected = 1_000e6;
        uint256 txId = _seedWithdraw(expected);

        vm.prank(attestor);
        vm.expectRevert("amount = 0");
        attestorContract.confirmWithdraw(txId, 0);
    }

    function test_ConfirmWithdraw_RevertOnBelowFloor() public {
        uint256 expected = 1_000e6;
        uint256 txId = _seedWithdraw(expected);

        // Floor = expected / 2 = 500e6. Pushing 499e6 must revert.
        vm.prank(attestor);
        vm.expectRevert("delivered below sanity floor");
        attestorContract.confirmWithdraw(txId, 499e6);
    }

    // -----------------------------------------------------------------
    // updateBalance()
    // -----------------------------------------------------------------

    function _seedAttestedBalance(uint256 amount) internal {
        uint256 txId = _seedDeposit(amount);
        vm.prank(attestor);
        attestorContract.confirmDeposit(txId, amount);
    }

    function test_UpdateBalance_RevertOnNoPriorBalance() public {
        vm.prank(attestor);
        vm.expectRevert("no prior balance");
        attestorContract.updateBalance(1_000e6);
    }

    function test_UpdateBalance_HappyPath_InBand() public {
        _seedAttestedBalance(1_000e6);

        // +5% — inside [-5%, +10%] band.
        vm.warp(3_000_000);
        vm.expectEmit(false, false, false, true, address(attestorContract));
        emit BalanceUpdated(1_050e6, 3_000_000);

        vm.prank(attestor);
        attestorContract.updateBalance(1_050e6);

        assertEq(attestorContract.attestedBalance(), 1_050e6);
        assertEq(attestorContract.lastAttestationTime(), 3_000_000);
    }

    function test_UpdateBalance_AcceptsExactUpperBound() public {
        _seedAttestedBalance(1_000e6);
        vm.prank(attestor);
        attestorContract.updateBalance(1_100e6); // +10% exactly
        assertEq(attestorContract.attestedBalance(), 1_100e6);
    }

    function test_UpdateBalance_AcceptsExactLowerBound() public {
        _seedAttestedBalance(1_000e6);
        vm.prank(attestor);
        attestorContract.updateBalance(950e6); // -5% exactly
        assertEq(attestorContract.attestedBalance(), 950e6);
    }

    function test_UpdateBalance_RevertOnUpperBoundExceeded() public {
        _seedAttestedBalance(1_000e6);
        vm.prank(attestor);
        vm.expectRevert("balance > +10%");
        attestorContract.updateBalance(1_100e6 + 1);
    }

    function test_UpdateBalance_RevertOnLowerBoundExceeded() public {
        _seedAttestedBalance(1_000e6);
        vm.prank(attestor);
        vm.expectRevert("balance < -5%");
        attestorContract.updateBalance(950e6 - 1);
    }

    function test_UpdateBalance_RevertOnZeroPush() public {
        _seedAttestedBalance(1_000e6);
        // 0 is below the -5% floor (950e6) → rejected as lower-bound trip.
        vm.prank(attestor);
        vm.expectRevert("balance < -5%");
        attestorContract.updateBalance(0);
    }

    // -----------------------------------------------------------------
    // balance() / valueInUsdc()
    // -----------------------------------------------------------------

    function test_Balance_ReturnsAttested() public {
        _seedAttestedBalance(1_000e6);
        assertEq(attestorContract.balance(), 1_000e6);
    }

    function test_ValueInUsdc_ZeroOnEmpty() public view {
        assertEq(attestorContract.valueInUsdc(), 0);
    }

    function test_ValueInUsdc_OnlyPendingEscrow_NoStalenessYet() public {
        // Deposit but never confirmed → lastAttestationTime stays 0.
        // valueInUsdc must include escrow without reverting.
        uint256 amount = 500e6;
        _seedDeposit(amount);

        // Warp far past HEARTBEAT to prove staleness check is skipped when
        // lastAttestationTime == 0.
        vm.warp(block.timestamp + 30 days);

        assertEq(attestorContract.valueInUsdc(), amount);
    }

    function test_ValueInUsdc_AttestedPlusPending() public {
        _seedAttestedBalance(1_000e6); // attestedBalance = 1000e6, no pending

        // Add a fresh pending deposit on top.
        usdc.mint(vault, 200e6);
        usdc.approve(address(attestorContract), 200e6);
        attestorContract.deposit(200e6);

        assertEq(attestorContract.valueInUsdc(), 1_200e6);
    }

    function test_ValueInUsdc_RevertOnStale() public {
        _seedAttestedBalance(1_000e6);
        // _seedAttestedBalance set lastAttestationTime to current. Warp
        // past HEARTBEAT.
        vm.warp(block.timestamp + 24 hours + 1);

        vm.expectRevert("attestation stale");
        attestorContract.valueInUsdc();
    }

    function test_ValueInUsdc_AcceptsExactlyAtHeartbeat() public {
        _seedAttestedBalance(1_000e6);
        vm.warp(block.timestamp + 24 hours); // exactly at HEARTBEAT
        assertEq(attestorContract.valueInUsdc(), 1_000e6);
    }
}
