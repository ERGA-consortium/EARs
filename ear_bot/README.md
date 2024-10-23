![](https://github.com/ERGA-consortium/EARs/blob/main/misc/EAR_bot_logo.png)

# EAR bot


## Overview

This project is a Python-based tool designed to automate the review process for ERGA Assembly Reports. It aims to streamline the review process and improve efficiency by automating repetitive tasks.
[More information...](https://github.com/ERGA-consortium/EARs/wiki)

## Workflows

This project includes several workflows to handle different stages of the review process. These workflows are designed to automate specific tasks and ensure a smooth and efficient review experience.

### Workflow 1: Find supervisor and add label on new PRs

This workflow is triggered upon the submission of a new EAR, showing a welcome message and applying the project name as label to the PR. Subsequently, it finds a supervisor based on `get_EAR_reviewer.py` and requests a confirmation.
During this process, it checks that only one file has been changed and ensures that the change is a new EAR, not a modification of an existing one. Additionally, it verifies that the PR body follows the correct format as per the default PR template. If any issues are detected, an `ERROR!` label is applied to the PR.

This workflow also handles updates to the EAR PR, including new commits, edits to the PR body, or the PR being reopened.
When a new commit is added (updated PDF), it notifies the reviewer or supervisor to review the changes, and also checks whether the ERROR flag can be removed if issues with the file count or PDF modification have been resolved.
If the PR body is edited while an ERROR flag is present, the workflow rechecks the body to ensure there are no issues, then removes the flag.
If the PR is reopened, it reinstates the "busy" status for the associated reviewer(s).

### Workflow 2+4: Check for supervisor or reviewer answer and Assign them (run on new comments)

Once there is a supervisor repling 'OK' as comment, it will automatically request a review from one of the reviewers determined by the `get_EAR_reviewer.py` script. Additionally, the `reviewers_list.csv` file will be updated to reflect the reviewer's status as "busy" by setting the corresponding field to `Y`.
If the reviewer responds with a `Yes`, the bot will request a review from that reviewer and reverse to "not busy" the status of any previously called reviewers that didn't accept. If the response is `No`, the bot will move on to the next reviewer.

### Workflow 3: Find Reviewer on schedule

If the selected reviewer does not respond within 100 hours, the system will proceed to the next reviewer on the list. This process will continue until all reviewers have been asked. If no reviewer responds, the system will notify the supervisor to manually assign a reviewer.

This workflow also checks if a PR has not been updated within 7 days and pings the supervisor to check it.

### Workflow 5: Approved PR

This Workflow will just thank the reviewer and notify the supervisor.

### Workflow 6: Merged or Closed PR

Once the PR has been approved and merged by the supervisor, the process is complete. The bot will then update the relevant tables as follows:

1. update `reviewers_list.csv`:

- change reviewer busy status back to 'N'
- substracts 1 point to the reviewer for reviewing the genome
- adds 1 to the total reviews of the reviewer
- adds date of merging (YYYY-MM-DD) to the last review of the reviewer
- adds 1 point to all the reviewer IDs with the same institution that the person who submitted the EAR

2. Add a new line at the end of the table `EAR_reviews.csv`

If the PR is closed (not merged), the bot will reverse the status of any busy reviewer and call the supervisor to double-check for any issues

It will also create a Slack post in the [#wp9-assembly-delivery channel](https://biogeneu.slack.com/archives/C070UHJ80Q3) to inform the team about the new Assembly.

Additionally, it will generate a YAML file next to the PDF file using `EARpdf_to_yaml.py`.