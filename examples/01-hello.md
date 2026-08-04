#!/usr/bin/env ccsub
#
# The "does my install work" job. Two directives, a timeout, and a prompt
# that cannot fail for an interesting reason.
#
# What it proves, in order: ccsub parses the header, the queue accepts the
# job, a tick dispatches it, and `claude` authenticates with no TTY and
# nothing to prompt at. If this comes back `done`, every later example is
# only a question of which directives you add.
#
# Why each line is here:
#
#   --name     is the label `ccstat` prints, and the handle `windowsill
#              show`, `ccdel` and `ccresume` take in place of the numeric
#              id. Leave it out and the name is the filename stem --
#              `01-hello`, which is a worse thing to type. Note that a name
#              only resolves among jobs that have not finished; once this
#              one is `done` you address it by id.
#
#   --model    is *not* a scheduler directive, and that is the point.
#              windowsill recognises a fixed set of `#$` directives and
#              hands everything else to `claude` as argv, in order, without
#              inspecting it. This line becomes `claude --model
#              claude-haiku-4-5`. Any claude flag works here the day it
#              ships, because windowsill never learns what it means.
#              Haiku because a smoke test should not cost Opus tokens.
#
#   --timeout  because the failure mode you are testing for is a hang. The
#              default is 4h; a broken install that sits waiting for input
#              would hold the queue for four hours before you found out.
#              Five minutes is generous for one sentence.
#
# There is no --at, so the job is due immediately: the next tick promotes
# and dispatches it. On the README's */15 crontab that is under fifteen
# minutes; run `windowsill tick` yourself if you would rather not wait.
#
# One thing to know before submitting. Any dispatch opens a 5-hour usage
# window if none is open, so a hello job at 09:00 on a quiet Sunday spends
# a window boundary on one sentence. Submit it while you are already
# working -- a window that is already open is free to share.
#
# Submit:  ccsub examples/01-hello.md
# Watch:   ccstat            (then: windowsill show <id>)
#
#$ --name hello
#$ --model claude-haiku-4-5
#$ --timeout 5m

Report the absolute path of your working directory and the name of the git
branch it is checked out on, then stop. Do not read, write or change any
files beyond what those two answers need.
