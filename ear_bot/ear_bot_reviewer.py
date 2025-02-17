import os
import re
import subprocess
import sys
from argparse import ArgumentParser
from datetime import datetime, timedelta

import pytz
from github import Github, UnknownObjectException

root_folder = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
csv_folder = os.path.join(root_folder, "rev")
sys.path.append(csv_folder)
import get_EAR_reviewer  # type: ignore

cet = pytz.timezone("CET")


def commit(repo, path, message, content):
    path = os.path.relpath(path, root_folder)
    try:
        contents = repo.get_contents(path)
        if not isinstance(contents, list):
            repo.update_file(contents.path, message, content, contents.sha)
            print(f"Updated {path} file.")
        else:
            print(f"{path} file could not be updated.")
    except UnknownObjectException:
        try:
            repo.create_file(path, message, content)
            print(f"Created {path} file.")
        except Exception as e:
            print(f"Error creating {path} file.\n\n{content}\n\n\n{e}")
    except Exception as e:
        print(f"Error updating {path} file.\n{e}")


class EAR_get_reviewer:
    def __init__(self, repo) -> None:
        self.repo = repo
        self.csv_file = os.path.join(csv_folder, "reviewers_list.csv")
        if not os.path.exists(self.csv_file):
            raise Exception("The CSV file does not exist.")
        with open(self.csv_file, "r") as file:
            csv_data = file.read()
        if not csv_data:
            raise Exception("The CSV file is empty.")
        self.data = get_EAR_reviewer.parse_csv(csv_data)

    def get_supervisor(self, user, calling_institution):
        try:
            selected_supervisor = get_EAR_reviewer.select_random_supervisor(
                self.data, user, calling_institution
            )
            return selected_supervisor.get("Github ID")
        except Exception as e:
            raise Exception(f"No eligible supervisors found.\n{e}")

    def get_reviewer(self, institution, project):
        try:
            _, top_candidate, _ = get_EAR_reviewer.select_best_reviewer(
                self.data, institution, project
            )
            get_EAR_reviewer_path = os.path.join(csv_folder, "get_EAR_reviewer.py")
            reviewer_print = subprocess.run(
                f"python {get_EAR_reviewer_path} -i '{institution}' -t '{project}'",
                shell=True,
                capture_output=True,
                text=True,
            ).stdout
            return top_candidate[0].get("Github ID"), reviewer_print
        except Exception as e:
            raise Exception(f"No eligible candidates found.\n{e}")

    def add_pr(self, name, institution, species, pr):
        ear_reviews_csv_file = os.path.join(csv_folder, "EAR_reviews.csv")
        if not os.path.exists(ear_reviews_csv_file):
            raise Exception("The EAR reviews CSV file does not exist.")
        with open(ear_reviews_csv_file, "r") as file:
            ear_reviews_csv_data = file.read()
        ear_reviews_csv_data += f"{name},{institution},{species},{pr}\n"
        commit(
            self.repo, ear_reviews_csv_file, "Add new EAR review", ear_reviews_csv_data
        )
        print(f"Added {name} to the EAR reviews CSV file.")

    def update_reviewers_list(
        self,
        reviewers,
        busy,
        institution="",
        submitted_at="",
        fined_reviewers=set(),
    ):
        if not reviewers:
            print("No reviewers to update.")
            return
        for reviewer_data in self.data:
            reviewer_data_id = reviewer_data.get("Github ID", "").lower()
            reviewer_data_score = int(reviewer_data.get("Calling Score", 1000))
            reviewer_data_total = int(reviewer_data.get("Total Reviews", 0))
            reviewer_data_institution = reviewer_data.get("Institution", "").lower()

            if reviewer_data_id in reviewers:
                reviewer_data["Busy"] = "Y" if busy else "N"
                if submitted_at:
                    reviewer_data_score -= 1
                    reviewer_data["Calling Score"] = str(reviewer_data_score)
                    reviewer_data["Total Reviews"] = str(reviewer_data_total + 1)
                    reviewer_data["Last Review"] = submitted_at
            elif reviewer_data_id in fined_reviewers:
                reviewer_data_score += 1
                reviewer_data["Calling Score"] = str(reviewer_data_score)
            if reviewer_data_institution == institution.lower():
                reviewer_data["Calling Score"] = str(reviewer_data_score + 1)

        csv_str = ",".join(self.data[0].keys()) + "\n"
        for row in self.data:
            csv_str += ",".join(row.values()) + "\n"
        commit(self.repo, self.csv_file, "Update reviewers list", csv_str)
        print(f"Updated the reviewers list for {', '.join(reviewers)}.\n{csv_str}")


class EARBotReviewer:
    def __init__(self) -> None:
        g = Github(os.getenv("GITHUB_APP_TOKEN"))
        self.repo = g.get_repo(str(os.getenv("GITHUB_REPOSITORY")))
        self.EAR_reviewer = EAR_get_reviewer(self.repo)
        self.pr_number = os.getenv("PR_NUMBER")
        self.comment_text = os.getenv("COMMENT_TEXT")
        self.comment_author = os.getenv("COMMENT_AUTHOR")
        self.reviewer = os.getenv("REVIEWER")
        self.valid_projects = ["ERGA-BGE", "ERGA-Pilot", "ERGA-Community"]

    def find_supervisor(self):
        # Will run when a new PR is opened
        pr = self.repo.get_pull(int(self.pr_number))

        action_type = os.getenv("ACTION_TYPE")
        error_label_existed = "ERROR!" in (label.name for label in pr.get_labels())
        if action_type == "edited" and not error_label_existed:
            return

        researcher = pr.user.login
        files_changed = pr.get_files()

        if files_changed.totalCount != 1:
            pr.create_issue_comment(
                f"Attention @{researcher}, you have changed more than one file!\n"
                "Please make sure to update only the EAR PDF file."
            )
            pr.add_to_labels("ERROR!")
            raise Exception("More than one file changed.")
        elif files_changed[0].status == "modified":
            pr.create_issue_comment(
                f"Hi @{researcher} this looks like an update of an approved EAR. I will flag the PR to call the attention of a supervisor to handle this :)"
            )
            pr.add_to_labels("EAR-UPDATE")
            print("EAR PDF file was modified.")
            sys.exit(0)

        if not pr.body:
            pr.create_issue_comment(
                f"Attention @{researcher}, it seems you want to start the reviewing process of an EAR, but the PR has an empty body.\n"
                "Please check the [Wiki](https://github.com/ERGA-consortium/EARs/wiki/Reviewers-section) if you need to refresh something."
            )
            pr.add_to_labels("ERROR!")
            raise Exception("Empty PR description.")

        project = self._search_in_body(pr, "Project")
        species = self._search_in_body(pr, "Species")
        calling_institution = self._search_for_institution(pr)

        if project not in self.valid_projects:
            pr.create_issue_comment(
                f"Attention @{researcher}, you have entered an invalid `Project:` field!\n"
                f"Please use one of the following project names: {', '.join(self.valid_projects)}"
            )
            pr.add_to_labels("ERROR!")
            raise Exception(f"Invalid project name: {project}")

        if error_label_existed and action_type != "reopened":
            pr.remove_from_labels("ERROR!")

        if not any(
            "I added the corresponding tag" in comment.body
            for comment in pr.get_issue_comments().reversed
        ):
            pr.create_issue_comment(
                f"Hi @{researcher}, thanks for sending the EAR of _{species}_.\n"
                "I added the corresponding tag to the PR and will contact a supervisor and a reviewer ASAP."
            )

        if not any(label.name in self.valid_projects for label in pr.get_labels()):
            pr.add_to_labels(project)

        if not any(
            "do you agree to [supervise]" in comment.body
            for comment in pr.get_issue_comments().reversed
        ):
            supervisor = self.EAR_reviewer.get_supervisor(
                researcher, calling_institution
            )
            pr.create_issue_comment(
                f"Hi @{supervisor}, do you agree to [supervise](https://github.com/ERGA-consortium/EARs/wiki/Assignees-section) this assembly?\n"
                "Please reply to this message only with **OK** to give acknowledge."
            )

        if action_type == "synchronize" and pr.assignees:
            reviewer = next(
                (
                    reviewer.login.lower()
                    for reviewer in pr.requested_reviewers
                    if reviewer.login.lower() != pr.assignee.login.lower()
                ),
                pr.assignee.login.lower(),
            )
            pr.create_issue_comment(f"Attention @{reviewer}, the EAR PDF was updated.")

        if action_type == "reopened" and pr.assignees:
            supervisor = pr.assignee.login
            pr.create_issue_comment(
                f"Attention @{supervisor}!\n"
                "The PR has been re-opened. Please check that everything looks OK."
            )

    def find_reviewer(self, prs=[], reject=False):
        # Will run when supervisor approves to be a assignee, or when there is a rejection or a deadline passed for a reviewer
        if not prs:
            prs = list(self.repo.get_pulls(state="open"))

        current_date = datetime.now(tz=cet)

        for pr in prs:
            if pr.updated_at.astimezone(cet) + timedelta(days=7) < current_date:
                supervisor = pr.assignee.login if pr.assignee else pr.user.login
                pr.create_issue_comment(
                    f"Ping @{supervisor},\nOne week without any movements on this PR!"
                )
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
                self._deadline(last_comment_date) < current_date
                if last_comment_date
                else False
            )
            institution = self._search_for_institution(pr)
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
                        f" {self._deadline(current_date).strftime('%d-%b-%Y at %H:%M CET')}"
                    )
                    self.EAR_reviewer.update_reviewers_list(
                        reviewers=[new_reviewer.lower()], busy=True
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

        supervisors = [
            reviewer["Github ID"]
            for reviewer in self.EAR_reviewer.data
            if reviewer["Supervisor"] == "Y" and reviewer["Github ID"] != pr.user.login
        ]

        if (
            comment_author in supervisors
            and "@erga-ear-bot clear" in comment_text
            and pr.state == "closed"
        ):
            comment_reviewer = self._search_comment_user(pr, "do you agree to review")
            self.EAR_reviewer.update_reviewers_list(
                reviewers=set(comment_reviewer), busy=False
            )
            for label in pr.get_labels():
                pr.remove_from_labels(label)

        if not pr.assignees:
            if comment_author not in supervisors:
                print("The comment author is not one of the supervisors.")
                sys.exit()
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
                time_wasted_reviewers = set(
                    self._search_comment_user(pr, "Time is out!")
                )
                self.EAR_reviewer.update_reviewers_list(
                    reviewers=set(comment_reviewer) - set([comment_author]),
                    busy=False,
                    fined_reviewers=time_wasted_reviewers,
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
                    f" {self._deadline(current_date).strftime('%d-%b-%Y at %H:%M CET')}"
                )
                print("Invalid comment text.")
                sys.exit()

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
            f"I will add a new reviewed species for you to the table when @{supervisor} merges the PR ;)\n\n"
            f"Congrats on the assembly @{researcher}!\n"
            "Please make sure that the fasta file to [upload to ENA](https://github.com/ERGA-consortium/ERGA-submission) is generated based on the final reviewed version of the assembly.\n\n"
            f"After @{supervisor} confirmation, you can start with the assembly submission to save time.\n"
            "The PR will be merged only when the final version of the EAR pdf is available."
        )

    def closed_pr(self):
        # Will run when the PR is closed
        pr = self.repo.get_pull(int(self.pr_number))
        reviews = pr.get_reviews().reversed
        merged = os.getenv("MERGED_STATUS") == "true"
        if merged and reviews.totalCount > 0:
            comment_reviewers = self._search_comment_user(pr, "for the review")
            the_review = next(
                (
                    review
                    for review in reviews
                    if comment_reviewers
                    and review.user.login.lower() == comment_reviewers[0]
                ),
                reviews[0],
            )
            reviewer = the_review.user.login.lower()
            submitted_at = datetime.now(tz=cet).strftime("%Y-%m-%d")

            researcher_name = pr.user.name or pr.user.login
            supervisor_name = pr.assignee.name or pr.assignee.login
            reviewer_name = the_review.user.name or the_review.user.login
            reviewer_institution = ""
            for entry in self.EAR_reviewer.data:
                github_id = entry.get("Github ID", "").lower()
                full_name = entry.get("Full Name")
                if full_name:
                    if github_id == pr.user.login.lower():
                        researcher_name = full_name
                    if github_id == pr.assignee.login.lower():
                        supervisor_name = full_name
                    if github_id == reviewer:
                        reviewer_name = full_name
                if github_id == reviewer:
                    reviewer_institution = entry.get("Institution", "")

            species = self._search_in_body(pr, "Species")
            self.EAR_reviewer.add_pr(
                reviewer_name, reviewer_institution, species, pr.html_url
            )

            institution = self._search_for_institution(pr)
            self.EAR_reviewer.update_reviewers_list(
                reviewers=[reviewer],
                busy=False,
                institution=institution,
                submitted_at=submitted_at,
            )
            EAR_pdf = next(
                file
                for file in pr.get_files()
                if file.filename.lower().endswith(".pdf")
            )
            self._add_yaml_file(EAR_pdf.filename)
            EAR_pdf_url = re.sub(r"/blob/[\w\d]+/", "/blob/main/", EAR_pdf.blob_url)
            slack_post = (
                f":tada: *New Assembly Finished!* :tada:\n\n"
                f"Congratulations to {researcher_name} and the {institution} team for the high-quality assembly of _{species}_\n\n"
                f"The assembly was reviewed by {reviewer_name}, and the process supervised by {supervisor_name}. The EAR can be found in the following link:\n"
                f"{EAR_pdf_url}"
            )
            self._create_slack_post(slack_post)
        elif not merged:
            supervisor = pr.assignee.login
            pr.create_issue_comment(
                f"Attention @{supervisor}!\n"
                "The PR has been closed, but the reviewers will retain their busy status in case it is re-opened.\n"
                "If the PR is going to remain closed, please instruct me to clear the active tasks."
            )
            pr.add_to_labels("ERROR!")
        else:
            EAR_pdf_filename = next(
                file.filename
                for file in pr.get_files()
                if file.filename.lower().endswith(".pdf")
            )
            self._add_yaml_file(EAR_pdf_filename)
            print("No review has been found for this merged PR.")
            pr.create_issue_comment(
                "The YAML file has been updated based on the new EAR.pdf"
            )

    def _search_comment_user(self, pr, text_to_check):
        comment_user = []
        for comment in pr.get_issue_comments().reversed:
            if comment.user.type == "Bot" and text_to_check in comment.body:
                comment_user_re = re.findall(
                    r"\B@([a-z0-9](?:-(?=[a-z0-9])|[a-z0-9]){0,38}(?<=[a-z0-9]))",
                    comment.body,
                    re.IGNORECASE,
                )
                if comment_user_re:
                    comment_user.append(comment_user_re[0].lower())
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

    def _search_for_institution(self, pr):
        institution = self._search_in_body(pr, "Affiliation").lower()
        if "cnag" in institution:
            return "CNAG"
        elif any(
            name in institution
            for name in ["sanger", "welcome sanger institute", "wsi"]
        ):
            return "Sanger"
        elif "genoscope" in institution:
            return "Genoscope"
        elif "scilifelab" in institution:
            return "SciLifeLab"
        return institution

    def _deadline(self, start_date):
        current_date = start_date
        added_hours = timedelta(hours=0)
        total_hours = timedelta(hours=100)
        while added_hours != total_hours:
            remaining_hours = total_hours - added_hours
            time_to_add = min(remaining_hours, timedelta(days=1))
            if (current_date + time_to_add).weekday() < 5:
                added_hours += time_to_add
            else:
                time_to_add = timedelta(days=1)
            current_date += time_to_add
        return current_date

    def _create_slack_post(self, content):
        from slack_sdk import WebClient

        client = WebClient(token=os.getenv("SLACK_TOKEN"))
        channel_id = os.getenv("SLACK_CHANNEL_ID")
        response = client.chat_postMessage(channel=channel_id, text=content)
        if not response["ok"]:
            print("Error creating post in Slack")
            return False
        link = client.chat_getPermalink(channel=channel_id, message_ts=response["ts"])[
            "permalink"
        ]
        print(f"Slack post created: {link}")
        return True

    def _add_yaml_file(self, EAR_pdf_filename):
        EARpdf_to_yaml_path = os.path.join(root_folder, "EARpdf_to_yaml.py")
        EAR_pdf_file = os.path.join(root_folder, EAR_pdf_filename)
        output_pdf_to_yaml = subprocess.run(
            f"python {EARpdf_to_yaml_path} {EAR_pdf_file}",
            shell=True,
            capture_output=True,
            text=True,
        )
        yaml_file = EAR_pdf_file.replace(".pdf", ".yaml")
        with open(yaml_file, "r") as file:
            yaml_content = file.read()
        commit(self.repo, yaml_file, "Add YAML file", yaml_content)
        print(output_pdf_to_yaml.stdout, output_pdf_to_yaml.stderr)


if __name__ == "__main__":
    parser = ArgumentParser(description="EAR bot!")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--supervisor", action="store_true")
    group.add_argument("--comment", action="store_true")
    group.add_argument("--search", action="store_true")
    group.add_argument("--approve", action="store_true")
    group.add_argument("--merged", action="store_true")
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
    elif args.merged:
        EARBot.closed_pr()
    else:
        parser.print_help()
        sys.exit(1)
