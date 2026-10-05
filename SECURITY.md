# Security

This plugin moves real money: it holds a seed (in the signer), funds gas and sweeps deposits. Please report a security problem privately, not in a public issue.

- Use **Security → Report a vulnerability** on this repository (GitHub private vulnerability reporting).
- Say what an attacker can do, what they need (network position, a stolen token, admin access, a malicious RPC provider), and how to reproduce it.
- Test only against your own installation, a test chain or anvil. Never against someone else's Bitcart host.

Ordinary bugs and improvements are welcome as normal issues and pull requests.

The threat model of the signer is in `signer/THREATS.md`.
