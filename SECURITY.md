# Security policy

Security fixes are provided on the current `vmware` branch. Do not open a public issue for a vulnerability that exposes SIM identities, credentials, message content, host access, or remote code execution.

Until a dedicated security mailbox is published, use GitHub private vulnerability reporting on the repository. Include the affected version, deployment mode, reproduction steps, impact and a redacted diagnostic bundle. Never include a real PIN, IMSI, ICCID, EID, phone number, subscription URL or notification token.

Deploy behind a trusted LAN or VPN, use a trusted TLS certificate, keep the host patched, and do not expose Docker, ModemManager, pcscd, SIP, AMI or the management port directly to the public Internet.
