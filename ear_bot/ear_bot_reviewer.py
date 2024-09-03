import os
import re
import subprocess
import sys
from argparse import ArgumentParser
from datetime import datetime, timedelta

import pytz
from github import Github

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "rev")))
import get_EAR_reviewer  # type: ignore

cet = pytz.timezone("CET")


class EAR_get_reviewer:
    def __init__(self) -> None:
        self.csv_folder = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "rev")
        )
        self.csv_file = os.path.join(self.csv_folder, "reviewers_list.csv")
        if not os.path.exists(self.csv_file):
            raise Exception("The CSV file does not exist.")
        with open(self.csv_file, "r") as file:
            csv_data = file.read()
        if not csv_data:
            raise Exception("The CSV file is empty.")
        self.data = get_EAR_reviewer.parse_csv(csv_data)

    def get_supervisor(self, user):
        try:
            selected_supervisor = get_EAR_reviewer.select_random_supervisor(
                self.data, user
            )
            return selected_supervisor.get("Github ID")
        except Exception as e:
            raise Exception(f"No eligible supervisors found.\n{e}")

    def get_reviewer(self, institution, project):
        try:
            _, top_candidate, _ = get_EAR_reviewer.select_best_reviewer(
                self.data, institution, project
            )
            get_EAR_reviewer_path = os.path.join(self.csv_folder, "get_EAR_reviewer.py")
            reviewer_print = subprocess.run(
                f"python {get_EAR_reviewer_path} -i {institution} -t {project}",
                shell=True,
                capture_output=True,
                text=True,
            ).stdout
            return top_candidate[0].get("Github ID"), reviewer_print
        except Exception as e:
            raise Exception(f"No eligible candidates found.\n{e}")

    def add_pr(self, name, institution, species, pr):
        ear_reviews_csv_file = os.path.join(self.csv_folder, "EAR_reviews.csv")
        if not os.path.exists(ear_reviews_csv_file):
            raise Exception("The EAR reviews CSV file does not exist.")
        with open(ear_reviews_csv_file, "a") as file:
            file.write(f"{name},{institution},{species},{pr}\n")
        print(f"Added {name} to the EAR reviews CSV file.")

    def update_reviewers_list(
        self,
        reviewer,
        busy,
        institution=None,
        submitted_at=None,
        old_reviewers=set(),
    ):
        for reviewer_data in self.data:
            if reviewer_data.get("Github ID", "").lower() == reviewer:
                reviewer_data["Busy"] = "Y" if busy else "N"
                if submitted_at:
                    reviewer_data["Calling Score"] = str(
                        int(reviewer_data.get("Calling Score", 1000)) - 1
                    )
                    reviewer_data["Total Reviews"] = str(
                        int(reviewer_data.get("Total Reviews", 1000)) + 1
                    )
                    reviewer_data["Last Review"] = submitted_at
                else:
                    break
            elif reviewer_data.get("Github ID", "").lower() in old_reviewers:
                reviewer_data["Calling Score"] = str(
                    int(reviewer_data.get("Calling Score", 1000)) + 1
                )
            if (
                institution
                and reviewer_data.get("Institution", "").lower() == institution.lower()
            ):
                reviewer_data["Calling Score"] = str(
                    int(reviewer_data.get("Calling Score", 1000)) + 1
                )

        csv_str = ",".join(self.data[0].keys()) + "\n"
        for row in self.data:
            csv_str += ",".join(row.values()) + "\n"
        with open(self.csv_file, "w") as file:
            file.write(csv_str)
        print(f"Updated the reviewers list for {reviewer}.\n")


class EARBotReviewer:
    def __init__(self) -> None:
        g = Github(os.getenv("GITHUB_TOKEN"))
        self.repo = g.get_repo(str(os.getenv("GITHUB_REPOSITORY")))
        self.EAR_reviewer = EAR_get_reviewer()
        self.pr_number = os.getenv("PR_NUMBER")
        self.comment_text = os.getenv("COMMENT_TEXT")
        self.comment_author = os.getenv("COMMENT_AUTHOR")
        self.reviewer = os.getenv("REVIEWER")
        self.valid_projects = ["ERGA-BGE", "ERGA-Pilot", "ERGA-Community"]

    def find_supervisor(self):
        # Will run when a new PR is opened
        pr = self.repo.get_pull(int(self.pr_number))
        if (
            not any(label.name in self.valid_projects for label in pr.get_labels())
            or not pr.assignees
        ):
            researcher = pr.user.login
            project = self._search_in_body(pr, "Project")
            if project not in self.valid_projects:
                pr.create_issue_comment(
                    f"Attention @{researcher}, you have entered an invalid `Project:` field!\n"
                    f"Please use one of the following project names: {', '.join(self.valid_projects)}"
                )
                pr.add_to_labels("ERROR!")
                raise Exception(f"Invalid project name: {project}")
            pr.add_to_labels(project)
            species = self._search_in_body(pr, "Species")
            self._search_in_body(pr, "Affiliation")
            pr.create_issue_comment(
                f"Hi @{researcher}, thanks for sending the EAR of _{species}_.\n"
                "I added the corresponding tag to the PR and will contact a supervisor and a reviewer ASAP."
            )
            supervisor = self.EAR_reviewer.get_supervisor(researcher)
            pr.create_issue_comment(
                f"Hi @{supervisor}, do you agree to [supervise](https://github.com/ERGA-consortium/EARs/wiki/Assignees-section) this assembly?\n"
                "Please reply to this message only with **OK** to give acknowledge."
            )
        else:
            if (
                pr.get_review_requests()[0].totalCount == 0
                and pr.get_reviews().totalCount > 0
            ):
                review_user = next(
                    review.user.login.lower() for review in pr.get_reviews()
                )
                pr.create_review_request([review_user])
                pr = self.repo.get_pull(int(self.pr_number))
            reviewer = next(
                (
                    reviewer.login.lower()
                    for reviewer in pr.requested_reviewers
                    if reviewer.login.lower() != pr.assignee.login.lower()
                ),
                pr.assignee.login.lower(),
            )
            pr.create_issue_comment(
                f"The researcher has updated the EAR PDF. Please review the assembly @{reviewer}."
            )

    def find_reviewer(self, prs=[], reject=False):
        # Will run when supervisor approves to be a assignee, or when there is a rejection or a deadline passed for a reviewer
        if not prs:
            prs = list(self.repo.get_pulls(state="open"))

        current_date = datetime.now(tz=cet)

        for pr in prs:
            if (
                pr.get_review_requests()[0].totalCount > 0
                or not any(
                    label.name in self.valid_projects for label in pr.get_labels()
                )
                or pr.get_reviews().totalCount > 0
                or not pr.assignees
            ):
                continue
            old_reviewers_list = self._search_comment_user(pr, "do you agree to review")
            last_comment_date = self._search_last_comment_time(
                pr, "do you agree to review"
            )
            deadline_passed = (
                last_comment_date + timedelta(days=7) < current_date
                if last_comment_date
                else False
            )
            institution = self._search_in_body(pr, "Affiliation")
            project = self._search_in_body(pr, "Project")
            try:
                if deadline_passed or reject:
                    message = (
                        f"@{old_reviewers_list[0]} Time is out! I will look for the next reviewer on the list :)"
                        if deadline_passed
                        else f"@{old_reviewers_list[0]} Ok thank you, I will look for the next reviewer on the list :)"
                    )
                    pr.create_issue_comment(message)

                if deadline_passed or reject or not old_reviewers_list:
                    new_reviewer, get_EAR_reviewer_print = (
                        self.EAR_reviewer.get_reviewer(institution, project)
                    )
                    pr.create_issue_comment(f"```\n{get_EAR_reviewer_print}```")
                    pr.create_issue_comment(
                        f"Hi @{new_reviewer}, do you agree to review this assembly?\n"
                        "Please reply to this message only with **Yes** or **No** by"
                        f" {(current_date + timedelta(days=7)).strftime('%d-%b-%Y at %H:%M CET')}"
                    )
                    self.EAR_reviewer.update_reviewers_list(
                        reviewer=new_reviewer, busy=True
                    )
            except Exception as e:
                supervisor = pr.assignee.login
                pr.create_issue_comment(
                    f"Hi @{supervisor}, it looks like there is a problem with this PR that requires your involvement to sort it out."
                )
                pr.add_to_labels("ERROR!")
                print(e)

    def comment(self):
        # Will run when there is a new comment
        try:
            comment_text = self.comment_text.lower()
            comment_author = self.comment_author.lower()
            pr = self.repo.get_pull(int(self.pr_number))
        except Exception as e:
            print(f"Missing required environment variables.\n{e}")
            sys.exit(1)

        if not pr.assignees:
            supervisors = [
                reviewer["Github ID"]
                for reviewer in self.EAR_reviewer.data
                if reviewer["Supervisor"] == "Y"
                and reviewer["Github ID"] != pr.user.login
            ]
            if comment_author not in supervisors:
                print("The comment author is not one of the supervisors.")
                sys.exit(1)
            if "ok" in comment_text:
                pr.add_to_assignees(comment_author)
                self.find_reviewer([self.repo.get_pull(int(self.pr_number))])
            else:
                pr.create_issue_comment(f"Invalid confirmation!")
                pr.add_to_labels("ERROR!")
                sys.exit(1)
        else:
            print(
                "The PR has already been assigned to a supervisor.\nChecking for the reviewer..."
            )
            if pr.get_review_requests()[0].totalCount > 0:
                print("The PR is already assigned to a reviewer.")
                sys.exit()
            if pr.get_reviews().totalCount > 0:
                print("The PR already has a review.")
                sys.exit()
            if comment_author in map(
                str.lower, [rr.login for rr in pr.requested_reviewers]
            ):
                print("The reviewer has already been assigned.")
                sys.exit()

            comment_reviewer = self._search_comment_user(pr, "do you agree to review")
            if not comment_reviewer or (
                comment_reviewer and comment_author != comment_reviewer[0]
            ):
                print("The reviewer is not the one who was asked to review the PR.")
                sys.exit()
            if "yes" in comment_text:
                for old_reviewer in set(comment_reviewer):
                    if old_reviewer != comment_author:
                        self.EAR_reviewer.update_reviewers_list(
                            reviewer=old_reviewer, busy=False
                        )
                pr.create_review_request([comment_author])
                pr.create_issue_comment(
                    "Thanks for agreeing!\n"
                    "I appointed you as the EAR reviewer.\n"
                    "I will keep your status as _Busy_ until you finish this review.\n"
                    "Please check the [Wiki](https://github.com/ERGA-consortium/EARs/wiki/Reviewers-section)"
                    " if you need to refresh something. (and remember that you must download the EAR PDF to"
                    " be able to click on the link to the contact map file!)\n"
                    "Contact the PR assignee for any issues."
                )
            elif "no" in comment_text:
                self.find_reviewer([pr], reject=True)
            else:
                current_date = datetime.now(tz=cet)
                pr.create_issue_comment(
                    f"Invalid confirmation!\nHi @{comment_author}, do you agree to review this assembly?\n"
                    "Please reply to this message only with **Yes** or **No** by"
                    f" {(current_date + timedelta(days=7)).strftime('%d-%b-%Y at %H:%M CET')}"
                )
                print("Invalid comment text.")
                sys.exit(1)

    def approve_reviewer(self):
        # Will run when there is a new review
        try:
            pr = self.repo.get_pull(int(self.pr_number))
            reviewer = self.reviewer.lower()
        except Exception as e:
            print(f"Missing required environment variables.\n{e}")
            sys.exit(1)
        supervisor = pr.assignee.login
        researcher = pr.user.login
        comment_reviewer = pr.get_reviews()
        if comment_reviewer.totalCount == 0 or (
            comment_reviewer.totalCount > 0
            and comment_reviewer[0].user.login.lower() != reviewer
        ):
            print("The reviewer is not the one who agreed to review the PR.")
            sys.exit()
        pr.create_issue_comment(
            f"Thanks @{reviewer} for the review.\n"
            f"I will add a new reviewed species for you to the table when @{supervisor} approves and merges the PR ;)\n\n"
            f"Congrats on the assembly @{researcher}!\n"
            "After merging, you can [upload the assembly to ENA](https://github.com/ERGA-consortium/ERGA-submission)."
        )

    def closed_pr(self, merged=False):
        # Will run when the PR is closed
        pr = self.repo.get_pull(int(self.pr_number))

        reviews = pr.get_reviews().reversed
        if reviews.totalCount > 0:
            comment_reviewers = self._search_comment_user(pr, "for the review")
            if not comment_reviewers:
                the_review = reviews[0]
            else:
                the_review = next(
                    review
                    for review in reviews
                    if review.user.login.lower() == comment_reviewers[0]
                )
            reviewer = the_review.user.login.lower()
        elif pr.requested_reviewers:
            reviewer = next(
                req_reviewer.login.lower()
                for req_reviewer in pr.requested_reviewers
                if req_reviewer.login.lower() != pr.assignee.login.lower()
            )
        else:
            print("No reviewer found.")
            sys.exit()

        submitted_at = None
        institution = None
        time_wasted_reviewers = set()
        if merged == True and reviews.totalCount > 0:
            time_wasted_reviewers = set(self._search_comment_user(pr, "Time is out!"))
            submitted_at = datetime.now(tz=cet).strftime("%Y-%m-%d")
            institution = self._search_in_body(pr, "Affiliation")
            species = self._search_in_body(pr, "Species")
            name = next(
                (
                    entry["Full Name"]
                    for entry in self.EAR_reviewer.data
                    if entry.get("Github ID", "").lower() == reviewer
                ),
                the_review.user.name or the_review.user.login,
            )

            self.EAR_reviewer.add_pr(name, institution, species, pr.html_url)
        else:
            comment_reviewer = self._search_comment_user(pr, "do you agree to review")
            for old_reviewer in set(comment_reviewer):
                if old_reviewer != reviewer:
                    self.EAR_reviewer.update_reviewers_list(
                        reviewer=old_reviewer, busy=False
                    )

        self.EAR_reviewer.update_reviewers_list(
            reviewer=reviewer,
            busy=False,
            institution=institution,
            submitted_at=submitted_at,
            old_reviewers=time_wasted_reviewers,
        )

    def _search_comment_user(self, pr, text_to_check):
        comment_user = []
        for comment in pr.get_issue_comments().reversed:
            if comment.user.type == "Bot" and text_to_check in comment.body:
                comment_user_re = re.search(r"@(\w+)", comment.body)
                if comment_user_re:
                    comment_user.append(comment_user_re.group(1).lower())
        return comment_user

    def _search_last_comment_time(self, pr, text_to_check):
        comment_time = None
        for comment in pr.get_issue_comments().reversed:
            if comment.user.type == "Bot" and text_to_check in comment.body:
                comment_time = comment.created_at.astimezone(cet)
                break
        return comment_time

    def _search_in_body(self, pr, text_to_check):
        lines = pr.body.strip().split("\n")
        for line in lines:
            if line.strip().startswith(f"- {text_to_check}:"):
                parts = line.split(":", 1)
                if len(parts) > 1:
                    item_value = parts[1].strip()
                    if item_value:
                        return item_value
        pr.create_issue_comment(
            f"Attention @{pr.user.login}, the field `{text_to_check}:` is missing or empty!\n"
            f"Please fix the issue by editing your first message (click on the three dots '…' in the top right corner of it)"
        )
        pr.add_to_labels("ERROR!")
        raise Exception(f"Missing {text_to_check} in the PR description.")


if __name__ == "__main__":
    parser = ArgumentParser(description="EAR bot!")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--supervisor", action="store_true")
    group.add_argument("--comment", action="store_true")
    group.add_argument("--search", action="store_true")
    group.add_argument("--approve", action="store_true")
    group.add_argument("--merged")
    args = parser.parse_args()
    EARBot = EARBotReviewer()
    if args.supervisor:
        EARBot.find_supervisor()
    elif args.comment:
        EARBot.comment()
    elif args.search:
        EARBot.find_reviewer()
    elif args.approve:
        EARBot.approve_reviewer()
    elif args.merged is not None:
        EARBot.closed_pr(merged=True if args.merged == "true" else False)
    else:
        parser.print_help()
        sys.exit(1)
