import os
import re
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
        selected_supervisor = get_EAR_reviewer.select_random_supervisor(self.data, user)
        if not selected_supervisor:
            raise Exception("No eligible supervisors found.")
        return selected_supervisor.get("Github ID")

    def get_reviewer(self, institution):
        all_eligible_candidates, _, _ = get_EAR_reviewer.select_best_reviewer(
            self.data, institution, "ERGA-BGE"
        )
        if not all_eligible_candidates:
            raise Exception("No eligible candidates found.")
        top_candidates = [
            candidate.get("Github ID", "").lower()
            for candidate in all_eligible_candidates
        ]
        return top_candidates

    def add_pr(self, name, institution, species, pr):
        ear_reviews_csv_file = os.path.join(self.csv_folder, "EAR_reviews.csv")
        if not os.path.exists(ear_reviews_csv_file):
            raise Exception("The EAR reviews CSV file does not exist.")
        with open(ear_reviews_csv_file, "a") as file:
            file.write(f"{name},{institution},{species},{pr}\n")

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


class EARBotReviewer:
    def __init__(self) -> None:
        g = Github(os.getenv("GITHUB_TOKEN"))
        self.repo = g.get_repo(str(os.getenv("GITHUB_REPOSITORY")))
        self.EAR_reviewer = EAR_get_reviewer()
        self.pr_number = os.getenv("PR_NUMBER")
        self.comment_text = os.getenv("COMMENT_TEXT")
        self.comment_author = os.getenv("COMMENT_AUTHOR")
        self.reviewer = os.getenv("REVIEWER")

    def find_reviewer(self, prs=[], reject=False):
        if not prs:
            prs = list(self.repo.get_pulls(state="open"))

        current_date = datetime.now(tz=cet)

        for pr in prs:
            if (
                len(pr.requested_reviewers) > 1
                or "ERGA-BGE" not in [label.name for label in pr.get_labels()]
                or pr.get_reviews().totalCount > 0
            ):
                continue
            old_reviewers = set()
            deadline_passed = False
            for comment in pr.get_issue_comments().reversed:
                text_to_check = "Please reply to this message"
                if comment.user.type == "Bot" and text_to_check in comment.body:
                    if not old_reviewers:
                        last_comment_date = comment.created_at.astimezone(cet)
                        deadline_passed = (
                            last_comment_date + timedelta(days=7) < current_date
                        )
                    comment_reviewer_re = re.search(r"@(\w+)", comment.body)
                    if comment_reviewer_re:
                        old_reviewers.add(comment_reviewer_re.group(1).lower())

            institution_re = re.search(r"Affiliation:\s*(\S+)", pr.body)
            species_re = re.search(r"Species:\s*(.+)", pr.body)
            if not institution_re or not species_re:
                pr.create_issue_comment(
                    "Missing affiliation or species in the PR description."
                )
                continue

            institution = institution_re.group(1)
            list_of_reviewers = self.EAR_reviewer.get_reviewer(institution)
            list_of_reviewers = [
                reviewer
                for reviewer in list_of_reviewers
                if reviewer != pr.user.login.lower()
                and reviewer != pr.assignee.login.lower()
            ]

            assign_new_reviewer = False
            if deadline_passed:
                assign_new_reviewer = True
                pr.create_issue_comment(
                    "Time is out! I will look for the next reviewer on the list :)"
                )
            if reject:
                assign_new_reviewer = True
                pr.create_issue_comment(
                    "Ok thank you, I will look for the next reviewer on the list :)"
                )
            if not old_reviewers:
                assign_new_reviewer = True

            if assign_new_reviewer:
                try:
                    new_reviewer = next(
                        reviewer
                        for reviewer in list_of_reviewers
                        if reviewer not in old_reviewers
                    )
                    pr.create_issue_comment(
                        f"👋 Hi @{new_reviewer}, do you agree to review this assembly?\n"
                        "Please reply to this message only with **Yes** or **No** by"
                        f" {(current_date + timedelta(days=7)).strftime('%d-%b-%Y at %H:%M CET')}"
                    )
                    self.EAR_reviewer.update_reviewers_list(
                        reviewer=new_reviewer, busy=True
                    )
                except:
                    supervisor = pr.assignee.login
                    pr.create_issue_comment(
                        f"No more reviewers available at the moment. @{supervisor} will assign a reviewer."
                    )

    def assign_reviewer(self):
        try:
            comment_text = self.comment_text.lower()
            comment_author = self.comment_author.lower()
            pr = self.repo.get_pull(int(self.pr_number))
        except Exception as e:
            print(f"Missing required environment variables.\n{e}")
            sys.exit(1)
        if len(pr.requested_reviewers) > 1:
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

        comment_reviewer = None
        for comment in pr.get_issue_comments().reversed:
            text_to_check = "Please reply to this message"
            if comment.user.type == "Bot" and text_to_check in comment.body:
                comment_reviewer = re.search(r"@(\w+)", comment.body).group(1).lower()
                break
        if not comment_reviewer:
            print("Missing reviewer from the comment.")
            sys.exit(1)
        if comment_author != comment_reviewer:
            print("The reviewer is not the one who was asked to review the PR.")
            sys.exit(1)
        if "yes" in comment_text:
            supervisor = pr.assignee.login
            pr.create_review_request([comment_author])
            pr.create_issue_comment(
                f"Thank you @{comment_author} for agreeing 👍\n"
                "I appointed you as the EAR reviewer.\n"
                "Please check the [Wiki](https://github.com/ERGA-consortium/EARs/wiki/Reviewers-section)"
                " if you need to refresh something. (and remember that you must download the EAR PDF to"
                " be able to click on the link to the contact map file!)\n"
                f"Contact the PR assignee (@{supervisor}) for any issues."
            )
            pr.add_to_labels("testing")

        elif "no" in comment_text:
            self.EAR_reviewer.update_reviewers_list(reviewer=comment_author, busy=False)
            self.find_reviewer([pr], reject=True)
        else:
            print("Invalid comment text.")
            sys.exit(1)

    def approve_reviewer(self):
        pr = self.repo.get_pull(int(self.pr_number))
        try:
            reviewer = self.reviewer.lower()
        except Exception as e:
            print(f"Missing required environment variables.\n{e}")
            sys.exit(1)
        supervisor = pr.assignee.login
        if reviewer == supervisor:
            print(
                "The reviewer is the same as the supervisor, don't need to do anything."
            )
            sys.exit()
        researcher = pr.user.login
        comment_reviewer = None
        for comment in pr.get_issue_comments().reversed:
            text_to_check = "for agreeing"
            if comment.user.type == "Bot" and text_to_check in comment.body:
                comment_reviewer = re.search(r"@(\w+)", comment.body).group(1).lower()
                break
        if comment_reviewer and comment_reviewer != reviewer:
            print("The reviewer is not the one who agreed to review the PR.")
            sys.exit()
        pr.create_issue_comment(
            f"Thanks @{reviewer} for the review.\nI will add a new reviewed species for you to the table when"
            f" @{supervisor} approves and merges the PR ;)\n\nCongrats on the assembly @{researcher}!\n"
            "After merging, you can [upload the assembly to ENA](https://github.com/ERGA-consortium/ERGA-submission)."
        )

    def closed_pr(self, merged=False):
        pr = self.repo.get_pull(int(self.pr_number))
        if "testing" in [label.name for label in pr.get_labels()]:
            pr.remove_from_labels("testing")

        reviews = pr.get_reviews().reversed
        comments = pr.get_issue_comments().reversed
        if reviews.totalCount > 0:
            for comment in comments:
                text_to_check = "for the review"
                if comment.user.type == "Bot" and text_to_check in comment.body:
                    comment_reviewer = (
                        re.search(r"@(\w+)", comment.body).group(1).lower()
                    )
                    the_review = next(
                        review
                        for review in reviews
                        if review.user.login.lower() == comment_reviewer
                    )
                    break
            else:
                the_review = reviews[0]
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

        old_reviewers = set()
        submitted_at = None
        institution = None
        if merged == True and reviews.totalCount > 0:
            for comment in comments:
                text_to_check = "Please reply to this message"
                if comment.user.type == "Bot" and text_to_check in comment.body:
                    comment_reviewer = (
                        re.search(r"@(\w+)", comment.body).group(1).lower()
                    )
                    old_reviewers.add(comment_reviewer)
            submitted_at = the_review.submitted_at.astimezone(cet).strftime("%Y-%m-%d")
            institution = re.search(r"Affiliation:\s*(\S+)", pr.body).group(1)

            name = next(
                (
                    entry["Full Name"]
                    for entry in self.EAR_reviewer.data
                    if entry.get("Github ID", "").lower() == reviewer
                ),
                the_review.user.name or the_review.user.login,
            )
            species = re.search(r"Species:\s*(.+)", pr.body).group(1).strip()
            self.EAR_reviewer.add_pr(name, institution, species, pr.html_url)

        self.EAR_reviewer.update_reviewers_list(
            reviewer=reviewer,
            busy=False,
            institution=institution,
            submitted_at=submitted_at,
            old_reviewers=old_reviewers,
        )

    def find_supervisor(self):
        pr = self.repo.get_pull(int(self.pr_number))
        try:
            if (
                "ERGA-BGE" not in [label.name for label in pr.get_labels()]
                or not pr.assignees
            ):
                pr.add_to_labels("ERGA-BGE")
                researcher = pr.user.login
                supervisor = self.EAR_reviewer.get_supervisor(researcher)
                pr.add_to_assignees(supervisor)
                pr.create_review_request([supervisor])
                message = (
                    f"👋 Hi @{researcher}, thanks for sending the EAR.\n"
                    "I added the corresponding tag to the PR and appointed"
                    f" @{supervisor} as the [assignee](https://github.com/ERGA-consortium/EARs/wiki/Assignees-section) to supervise."
                )
            else:
                if len(pr.requested_reviewers) < 2 and pr.get_reviews().totalCount > 0:
                    review_user = next(
                        review.user.login.lower() for review in pr.get_reviews()
                    )
                    pr.create_review_request([review_user])
                reviewer = next(
                    (
                        reviewer.login.lower()
                        for reviewer in pr.requested_reviewers
                        if reviewer.login.lower() != pr.assignee.login.lower()
                    ),
                    pr.assignee.login.lower(),
                )
                message = f"The researcher has updated the EAR PDF. Please review the assembly @{reviewer}."
            pr.create_issue_comment(message)
        except Exception as e:
            print(f"An error occurred: {e}")
            sys.exit(1)


if __name__ == "__main__":
    parser = ArgumentParser(description="EAR bot!")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--search", action="store_true", help="Search for a reviewer if needed."
    )
    group.add_argument(
        "--comment",
        action="store_true",
        help="Assign the reviewer to the PR when the reviewer agrees.",
    )
    group.add_argument("--approve", action="store_true", help="Thanks the reviewer.")
    group.add_argument(
        "--supervisor",
        action="store_true",
        help="Find the supervisor and assign the ERGA-BGE label.",
    )
    group.add_argument(
        "--merged",
        help="Remove the testing label and update the reviewer status.",
    )
    args = parser.parse_args()
    EARBot = EARBotReviewer()
    if args.search:
        EARBot.find_reviewer()
    elif args.comment:
        EARBot.assign_reviewer()
    elif args.approve:
        EARBot.approve_reviewer()
    elif args.supervisor:
        EARBot.find_supervisor()
    elif args.merged is not None:
        EARBot.closed_pr(merged=True if args.merged == "true" else False)
    else:
        parser.print_help()
        sys.exit(1)
