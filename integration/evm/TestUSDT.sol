// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;
// Integration test only: USDT-like (transfer returns nothing), 6 decimals, open mint. Never deployed outside anvil.
contract TestUSDT {
    uint8 public constant decimals = 6;
    mapping(address => uint256) public balanceOf;
    event Transfer(address indexed from, address indexed to, uint256 value);
    function transfer(address to, uint256 value) external { balanceOf[msg.sender] -= value; balanceOf[to] += value; emit Transfer(msg.sender, to, value); }
    function mint(address to, uint256 value) external { balanceOf[to] += value; emit Transfer(address(0), to, value); }
    function symbol() external pure returns (string memory) { return "USDT"; }
    function name() external pure returns (string memory) { return "Test USDT"; }
}
