#!/usr/bin/env ccq
# This whole job file is a scratch test for comment handling.
#$ --name comment-test
#$ #this is a #$-style comment and must be ignored
#$ --at +3h
# another bare comment line
#$ --window offpeak
## a double-hash bare comment
#$ --notify ntfy

Body starts here and must not contain any comment text.
