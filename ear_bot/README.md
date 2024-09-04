# EAR bot


## Overview

This project is a Python-based tool designed to automate the review process for ERGA Assembly Reports. It aims to streamline the review process and improve efficiency by automating repetitive tasks.
[More information...](https://hackmd.io/f073OVnGQQ-pYy3aJHjalA)

## Workflows

This project includes several workflows to handle different stages of the review process. These workflows are designed to automate specific tasks and ensure a smooth and efficient review experience.

### Workflow 1: Find supervisor and add label on new PRs

This workflow is triggered upon the submission of a new EAR, showing a welcome message and applying the project name as label to the PR. Subsequently, it finds a supervisor based on `get_EAR_reviewer.py` and requests a confirmation.

### Workflow 2+4: Check for supervisor or reviewer answer and Assign them (run on new comments)

Once there is a supervisor repling 'OK' as comment, it will automatically request a review from one of the reviewers determined by the `get_EAR_reviewer.py` script. Additionally, the `reviewers_list.csv` file will be updated to reflect the reviewer's status as "busy" by setting the corresponding field to `Y`.
If the reviewer responds with a `Yes`, the bot will request a review from that reviewer and reverse to "not busy" the status of any previously called reviewers that didn't accept. If the response is `No`, the bot will move on to the next reviewer.

### Workflow 3: Find Reviewer on schedule

If the selected reviewer does not respond within 7 days, the system will proceed to the next reviewer on the list. This process will continue until all reviewers have been asked. If no reviewer responds, the system will notify the supervisor to manually assign a reviewer.

### Workflow 5: Approved PR

This Workflow will just thank the reviewer and notify the supervisor.

### Workflow 6: Merged PR

Once the PR has been approved and merged by the supervisor, the process is complete. The bot will then update the relevant tables as follows:

1. update `reviewers_list.csv`:

- change reviewer busy status back to 'N'
- substracts 1 point to the reviewer for reviewing the genome
- adds 1 to the total reviews of the reviewer
- adds date of merging (YYYY-MM-DD) to the last review of the reviewer
- adds 1 point to all the reviewer IDs with the same institution that the person who submitted the EAR

2. Add a new line at the end of the table `EAR_reviews.csv`

### Workflow 7: Closed PR

If the PR is closed (not merged), the bot will reverse the status of any busy reviewer and call the supervisor to double-check for any issues
