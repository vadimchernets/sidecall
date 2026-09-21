# Security

Sidecall starts other AI command-line tools on your computer, so safety problems matter to us.

## Reporting a vulnerability

Please do not open a public issue. Open a private security advisory instead:
GitHub repository page, then "Security", then "Report a vulnerability". Describe what you found, how to reproduce
it, and what it lets someone do. You will get a reply as soon as possible.

## In scope

- A way for a tool's answer to make Sidecall or Claude run commands, write files outside the run folder, or send
  data somewhere.
- A credential (key, token, password) reaching a tool or a run folder.
- A tool or its child processes surviving after the time limit or after Sidecall is stopped.
- A quota-spending call that happens without the notice line shown first.

Sidecall is not affiliated with Anthropic, OpenAI, Google, xAI or Moonshot AI; problems inside those tools should
be reported to their makers.
