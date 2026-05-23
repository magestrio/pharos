// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {IStrategyAdapter} from "./IStrategyAdapter.sol";

/// @title BybitAttestor
/// @notice Off-chain Bybit balance represented as an IStrategyAdapter.
///         To CapitalManager this contract looks like any other whitelisted
///         adapter (`deposit` / `withdraw` / `valueInUsdc`). Under the hood
///         real capital lives off-chain on a Bybit account controlled by the
///         attestor (a Gnosis Safe). The attestor pushes signed
///         attestations of the Bybit-side USDC equivalent balance into this
///         contract.
///
///         Trust model — same pattern as Ondo USDY / BlackRock BUIDL:
///         vUSDC holders trust the attestor (Safe 2-of-3) to (a) honestly
///         report the off-chain balance and (b) actually return funds
///         on-chain when a withdraw is requested. In production this is
///         strengthened with Chainlink Proof of Reserves, multiple
///         attestors and an insurance fund; for the MVP it is a single
///         Safe with on-chain sanity checks (see subtasks .4 and .7) as
///         circuit breakers.
///
/// @dev    Oracle rules (per IStrategyAdapter NatSpec): the oracle for
///         `valueInUsdc()` is the attestor-pushed `attestedBalance`. This
///         is a deliberate deviation from manipulation-resistant on-chain
///         oracles: there is no on-chain price source for Bybit-side
///         positions. Liveness is enforced via `lastAttestationTime` — if
///         the attestor goes silent past a heartbeat, `valueInUsdc()`
///         will revert (implemented in subtask .8). Sanity bounds on
///         single-update balance moves are enforced in `confirmDeposit`,
///         `confirmWithdraw` and `updateBalance` (subtasks .4, .6, .7).
contract BybitAttestor is IStrategyAdapter, Ownable {
    using SafeERC20 for IERC20;

    IERC20  public immutable usdc;
    address public immutable vault;
    address public immutable attestor;

    /// @notice Emitted when CapitalManager calls `deposit`. The off-chain
    ///         bot listens for this event, pulls escrowed USDC, transfers
    ///         it to Bybit and subscribes the funds into a chosen Earn
    ///         product. The bot then closes the loop via `confirmDeposit`.
    event DepositRequested(uint256 indexed txId, uint256 amount);

    /// @notice Emitted by the attestor after off-chain capital has been
    ///         deployed on Bybit. Carries the new authoritative Bybit-side
    ///         USDC equivalent balance.
    event DepositConfirmed(uint256 indexed txId, uint256 newAttestedBalance);

    /// @notice Emitted when CapitalManager calls `withdraw`. The bot must
    ///         close hedges, redeem Earn positions, swap back to USDC and
    ///         bridge USDC from Bybit to Mantle. The withdraw is async —
    ///         `withdraw()` itself returns 0 and the actual USDC arrives
    ///         later via `confirmWithdraw`.
    event WithdrawRequested(uint256 indexed txId, uint256 amount);

    /// @notice Emitted by the attestor after USDC has been bridged back
    ///         on-chain and returned to CapitalManager.
    event WithdrawConfirmed(uint256 indexed txId, uint256 amount);

    /// @notice Emitted by the attestor on a periodic balance push (5 min
    ///         for volatile positions, 30 min for stable-only).
    event BalanceUpdated(uint256 newAttestedBalance, uint256 timestamp);

    modifier onlyVault() {
        require(msg.sender == vault, "not vault");
        _;
    }

    modifier onlyAttestor() {
        require(msg.sender == attestor, "not attestor");
        _;
    }

    constructor(
        address _usdc,
        address _vault,
        address _attestor,
        address _owner
    ) Ownable(_owner) {
        usdc     = IERC20(_usdc);
        vault    = _vault;
        attestor = _attestor;
    }

    /// @inheritdoc IStrategyAdapter
    function asset() external view returns (address) {
        return address(usdc);
    }

    /// @inheritdoc IStrategyAdapter
    /// @dev Filled in by subtask .3. Escrows USDC, records pendingDeposit,
    ///      emits DepositRequested, returns txId.
    function deposit(uint256 /* amount */) external onlyVault {
        revert("not implemented");
    }

    /// @inheritdoc IStrategyAdapter
    /// @dev Filled in by subtask .5. Async — returns 0; real USDC arrives
    ///      via `confirmWithdraw` after the off-chain bot bridges funds
    ///      back from Bybit.
    function withdraw(uint256 /* amount */) external onlyVault returns (uint256) {
        revert("not implemented");
    }

    /// @inheritdoc IStrategyAdapter
    /// @dev Filled in by subtask .2 / .8. Returns the attestor-pushed
    ///      Bybit-side balance (native unit == USDC, 6 decimals).
    function balance() external view returns (uint256) {
        return 0;
    }

    /// @inheritdoc IStrategyAdapter
    /// @dev Filled in by subtask .8. Returns
    ///      `attestedBalance + totalPendingDeposits`, with a staleness
    ///      revert on `lastAttestationTime` exceeding the heartbeat.
    function valueInUsdc() external view returns (uint256) {
        return 0;
    }
}
