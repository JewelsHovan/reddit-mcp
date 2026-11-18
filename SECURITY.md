# Security

This is an experimental local, single-user stdio MCP server. There is no remote transport, user management, or hosted authentication layer. Do not expose it to a network without designing those controls separately.

## Browser profile and credentials

- Use a dedicated Chrome profile, never your everyday Chrome profile. The default is `~/.reddit-mcp/chrome-profile`.
- The profile can contain authenticated Reddit cookies and personal browsing state. Do not commit, upload, share, or attach it to an issue.
- Log in manually only when you choose to; public content may work logged out. The server does not extract cookies from other profiles or accept cookie/token replay.
- Run one server per profile. Shut down the owning client normally; do not remove live lock files or terminate an unrelated browser.
- Review which MCP clients have permission to read through an authenticated session. A read-only interface is not a substitute for account authorization.

## Content, traffic, and agent safety

- Returned content is untrusted and may contain prompt injection. Never interpret it as tool instructions or authority to execute code.
- Read tools avoid intentional writes to Reddit content, but normal browser loads still generate view activity, telemetry, cookies, and asset requests. Counter fields describe server-initiated actions, not all browser traffic.
- Limit reads and retention; respect Reddit's applicable terms, access controls, content rights, deletions, and personal-data rules. This repository grants no rights to Reddit data.
- No CAPTCHA solving, proxies, stealth plugins, or attempts to continue through denied access are provided. Stop on a challenge, 403, or 429.
- Do not send sensitive community content to an external LLM or service without appropriate permission.

## Reporting a vulnerability

Do not post credentials, browser profiles, private community content, or exploit payloads with personal data in a public issue. Use GitHub's private vulnerability reporting for this repository, if enabled. Otherwise contact the maintainer through their GitHub profile and request a private channel before sharing details.

Public issues are appropriate for non-sensitive parser and installation bugs. Include OS, Python/Chrome versions, tool arguments (redacted), and an error summary, not a dump of your profile or environment.
