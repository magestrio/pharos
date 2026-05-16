// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {Vault8004} from "../src/Vault8004.sol";
import {DecisionLog} from "../src/DecisionLog.sol";
import {ReputationOracle} from "../src/ReputationOracle.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

contract MockERC20 is ERC20 {
    constructor() ERC20("Mock USDC", "USDC") {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

contract Vault8004Test is Test {
    Vault8004 vault;
    DecisionLog decisionLog;
    ReputationOracle oracle;
    MockERC20 usdc;

    address owner = address(0xBEEF);

    function setUp() public {
        usdc = new MockERC20();
        vault = new Vault8004(address(usdc), owner);
        decisionLog = new DecisionLog(owner);
        oracle = new ReputationOracle(address(vault), address(0), 1, 1e6);
    }

    function test_Deploy() public view {
        assert(address(vault) != address(0));
        assert(address(decisionLog) != address(0));
        assert(address(oracle) != address(0));
    }
}
