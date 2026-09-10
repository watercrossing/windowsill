# Notifications are opt-in, and no destination is ever defaulted or guessed

**Settled:** 2026-07-30

## Context

The repo owner ran two jobs overnight against the live queue, with nothing configured about notifications.

## Decision

**A shipped default recipient gets mailed.**
The `you@example.org` in the example config was the effective default, `[notify].default` was `email`, and both runs mailed it.
The postmaster refused it: *"an MX or SRV record indicated no SMTP service"*.

## Consequence

Notifications are opt-in.
`[notify].default` is `none`, and neither channel has a default destination.
A channel that is on with nowhere to send to sends nothing: no placeholder recipient, no topic, nothing derived from `$USER` or the hostname.
Instead it records a `notify_skipped` event, because the alternative is a queue that quietly mails a stranger.

The same applies to `[notify.ntfy].topic`: a shipped topic is a *public* one.

`--notify <channel>:<destination>` carries a per-job address or topic for when the global config names nobody.
`windowsill show` prints the destination a job would actually reach, not just the channel.
