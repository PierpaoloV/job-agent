"""Job agent entry point."""
import sys, pathlib, traceback
sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from fetch_gmail import fetch_job_emails
from parse_jobs import parse_emails
from dedupe import filter_new, mark_seen, is_applied
from rank_llm import rank_jobs
from notify_telegram import send_digest, send_error


def main(days_back: int = 2):
    try:
        # 1. Fetch emails
        try:
            emails = fetch_job_emails(days_back=days_back)
        except Exception as e:
            send_error(f"Gmail fetch failed: {e}")
            return

        if not emails:
            print("No job alert emails found.")
            send_digest([])
            return

        # 2. Parse into job listings
        jobs = parse_emails(emails)
        if not jobs:
            print("No job listings parsed from emails.")
            send_digest([])
            return

        # 3. Deduplicate
        new_jobs = filter_new(jobs)
        new_jobs = [j for j in new_jobs if not is_applied(j.get("url", ""))]

        if not new_jobs:
            print("No new jobs after deduplication.")
            send_digest([])
            return

        # 4. LLM ranking
        top_jobs = rank_jobs(new_jobs, top_n=10)

        # 5. Mark all new jobs as seen
        mark_seen(new_jobs)

        # 6. Send digest
        send_digest(top_jobs)

    except Exception as e:
        tb = traceback.format_exc()
        print(f"Pipeline crashed:\n{tb}")
        try:
            send_error(f"Pipeline crashed: {e}\n\n<pre>{tb[-1000:]}</pre>")
        except Exception:
            pass


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2, help="How many days back to fetch emails")
    args = parser.parse_args()
    main(days_back=args.days)
