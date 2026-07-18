#$ --name prose-test
#$ --priority 5
This line is prose, not a directive or a blank line, so the header ends here.
#$ --retry 9
This directive-looking line must be treated as literal body text, verbatim.
