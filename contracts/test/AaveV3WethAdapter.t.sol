// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {AaveV3WethAdapter} from "../src/adapters/AaveV3WethAdapter.sol";

contract MockERC20 is ERC20 {
    constructor(string memory name, string memory symbol) ERC20(name, symbol) {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

contract MockAavePoolV2 {
    mapping(address => address) public aTokenFor;

    function setAToken(address underlying, address aToken) external {
        aTokenFor[underlying] = aToken;
    }

    function supply(address asset, uint256 amount, address onBehalfOf, uint16) external {
        IERC20(asset).transferFrom(msg.sender, address(this), amount);
        MockERC20(aTokenFor[asset]).mint(onBehalfOf, amount);
    }

    function withdraw(address asset, uint256 amount, address to) external returns (uint256) {
        MockERC20(asset).mint(to, amount);
        return amount;
    }
}

/// @notice Mock Chainlink-style aggregator returning a configurable answer.
contract MockChainlinkAggregator {
    int256 private _answer;

    constructor(int256 initial) { _answer = initial; }

    function setAnswer(int256 a) external { _answer = a; }
    function latestAnswer() external view returns (int256) { return _answer; }
}

/// @notice Mock Aave Oracle exposing per-asset Chainlink-style sources.
contract MockAaveOracle {
    mapping(address => address) public sources;

    function setSource(address asset, address src) external { sources[asset] = src; }
    function getSourceOfAsset(address asset) external view returns (address) { return sources[asset]; }
    function getAssetPrice(address) external pure returns (uint256) { return 0; }
}

contract AaveV3WethAdapterTest is Test {
    MockERC20         weth;
    MockERC20         aWeth;
    MockERC20         usdc;
    MockAavePoolV2    pool;
    MockAaveOracle    oracle;
    MockChainlinkAggregator wethFeed;
    MockChainlinkAggregator usdcFeed;
    AaveV3WethAdapter adapter;

    address vault = address(this);
    address owner = address(0xBEEF);

    function setUp() public {
        weth  = new MockERC20("Wrapped Ether", "WETH");
        aWeth = new MockERC20("Aave WETH",     "aWETH");
        usdc  = new MockERC20("USD Coin",      "USDC");
        pool  = new MockAavePoolV2();
        pool.setAToken(address(weth), address(aWeth));

        // Default prices: WETH = $2500.00, USDC = $1.00 (both 1e8).
        wethFeed = new MockChainlinkAggregator(2500_00000000);
        usdcFeed = new MockChainlinkAggregator(1_00000000);

        oracle = new MockAaveOracle();
        oracle.setSource(address(weth), address(wethFeed));
        oracle.setSource(address(usdc), address(usdcFeed));

        adapter = new AaveV3WethAdapter(
            address(pool),
            address(oracle),
            address(weth),
            address(aWeth),
            address(usdc),
            vault,
            owner
        );
    }

    function test_Deploy() public view {
        assertEq(address(adapter.aavePool()),   address(pool));
        assertEq(address(adapter.aaveOracle()), address(oracle));
        assertEq(address(adapter.weth()),       address(weth));
        assertEq(address(adapter.aWeth()),      address(aWeth));
        assertEq(adapter.usdc(),                address(usdc));
        assertEq(adapter.vault(),               vault);
        assertEq(adapter.owner(),               owner);
        assertEq(adapter.asset(),               address(weth));
    }

    function test_OnlyVault_Deposit() public {
        vm.prank(address(0xDEAD));
        vm.expectRevert("not vault");
        adapter.deposit(1e18);
    }

    function test_OnlyVault_Withdraw() public {
        vm.prank(address(0xDEAD));
        vm.expectRevert("not vault");
        adapter.withdraw(1e18);
    }

    function test_Deposit_SuppliesToAave() public {
        uint256 amount = 1e18;
        weth.mint(vault, amount);
        weth.approve(address(adapter), amount);

        adapter.deposit(amount);

        assertEq(weth.balanceOf(vault), 0);
        assertEq(weth.balanceOf(address(pool)), amount);
        assertEq(aWeth.balanceOf(address(adapter)), amount);
        assertEq(adapter.balance(), amount);
    }

    function test_Withdraw_ReturnsToVault() public {
        uint256 amount = 1e18;
        weth.mint(vault, amount);
        weth.approve(address(adapter), amount);
        adapter.deposit(amount);

        adapter.withdraw(amount);

        assertEq(weth.balanceOf(vault), amount);
    }

    function test_ValueInUsdc_ZeroOnEmpty() public view {
        assertEq(adapter.valueInUsdc(), 0);
    }

    function test_ValueInUsdc_DecimalFormula() public {
        // 1 WETH @ $2500, USDC @ $1.00 → expect 2500 USDC = 2500e6 units.
        uint256 amount = 1e18;
        weth.mint(vault, amount);
        weth.approve(address(adapter), amount);
        adapter.deposit(amount);

        // formula: (aWeth * wethPrice * 1e6) / (usdcPrice * 1e18)
        //        = (1e18 * 2500e8 * 1e6) / (1e8 * 1e18) = 2500e6
        assertEq(adapter.valueInUsdc(), 2500e6, "1 WETH @ $2500 should value 2500 USDC");
    }

    function test_ValueInUsdc_FractionalWeth() public {
        // 0.5 WETH @ $2500 → 1250 USDC
        uint256 amount = 0.5e18;
        weth.mint(vault, amount);
        weth.approve(address(adapter), amount);
        adapter.deposit(amount);

        assertEq(adapter.valueInUsdc(), 1250e6);
    }

    function test_ValueInUsdc_USDCPriceDrift() public {
        // 1 WETH @ $2500, USDC depegged to $0.99
        // expect (1e18 * 2500e8 * 1e6) / (0.99e8 * 1e18) ≈ 2525.252525e6
        uint256 amount = 1e18;
        weth.mint(vault, amount);
        weth.approve(address(adapter), amount);
        adapter.deposit(amount);

        usdcFeed.setAnswer(99_000_000); // $0.99 (1e8 scale)

        // Allow rounding slack: integer math truncates last decimals.
        assertApproxEqAbs(adapter.valueInUsdc(), 2525252525, 1);
    }

    function test_ValueInUsdc_RevertsOnZeroPrice() public {
        uint256 amount = 1e18;
        weth.mint(vault, amount);
        weth.approve(address(adapter), amount);
        adapter.deposit(amount);

        wethFeed.setAnswer(0);
        vm.expectRevert("invalid price");
        adapter.valueInUsdc();
    }

    function test_ValueInUsdc_RevertsOnNegativePrice() public {
        uint256 amount = 1e18;
        weth.mint(vault, amount);
        weth.approve(address(adapter), amount);
        adapter.deposit(amount);

        usdcFeed.setAnswer(-1);
        vm.expectRevert("invalid price");
        adapter.valueInUsdc();
    }

    function test_ValueInUsdc_RevertsOnMissingSource() public {
        uint256 amount = 1e18;
        weth.mint(vault, amount);
        weth.approve(address(adapter), amount);
        adapter.deposit(amount);

        oracle.setSource(address(weth), address(0));
        vm.expectRevert("no oracle source");
        adapter.valueInUsdc();
    }
}
