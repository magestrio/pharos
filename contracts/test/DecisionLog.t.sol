// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {DecisionLog} from "../src/DecisionLog.sol";

contract DecisionLogTest is Test {
    DecisionLog dlog;

    address owner = address(0xBEEF);
    address agent = address(0xCAFE);
    address user  = address(0xDEAD);

    function setUp() public {
        dlog = new DecisionLog(owner);
        vm.prank(owner);
        dlog.setAgent(agent);
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    function _record(bytes32 id) internal returns (uint256) {
        vm.prank(agent);
        return dlog.recordDecision(0, id, "QmTest", keccak256("act"));
    }

    // ── tests ─────────────────────────────────────────────────────────────────

    function test_Deploy() public view {
        assertEq(dlog.nextDecisionNonce(), 0);
        assertEq(dlog.owner(), owner);
    }

    function test_SetAgent_OnlyOwner() public {
        vm.expectRevert();
        vm.prank(user);
        dlog.setAgent(user);

        vm.prank(owner);
        dlog.setAgent(user);
        assertEq(dlog.agent(), user);
    }

    function test_RecordDecision_OnlyAgent() public {
        vm.expectRevert("not agent");
        vm.prank(user);
        dlog.recordDecision(0, bytes32(uint256(1)), "QmTest", keccak256("act"));
    }

    function test_RecordDecision_EmitsEvent() public {
        bytes32 id = bytes32(uint256(1));
        string memory cid = "QmTest";
        bytes32 actionHash = keccak256("act");

        vm.expectEmit(true, true, false, true);
        emit DecisionLog.DecisionRecorded(0, id, cid, actionHash, block.timestamp);

        vm.prank(agent);
        dlog.recordDecision(0, id, cid, actionHash);
    }

    function test_RecordDecision_IncrementsNonce() public {
        uint256 n0 = _record(bytes32(uint256(1)));
        uint256 n1 = _record(bytes32(uint256(2)));

        assertEq(n0, 0);
        assertEq(n1, 1);
        assertEq(dlog.nextDecisionNonce(), 2);
    }

    function test_RecordDecision_RejectsDuplicate() public {
        bytes32 id = bytes32(uint256(42));
        _record(id);

        vm.expectRevert("duplicate decision");
        vm.prank(agent);
        dlog.recordDecision(0, id, "QmTest", keccak256("act"));
    }

    function test_RecordDecision_StoresExists() public {
        bytes32 id = bytes32(uint256(7));
        assertFalse(dlog.exists(id));

        _record(id);
        assertTrue(dlog.exists(id));
    }
}
