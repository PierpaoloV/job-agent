"""CLI: log a job application to the database."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

from dedupe import mark_applied, get_applied


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/mark_applied.py <url> [notes]")
        print("       python scripts/mark_applied.py --status")
        sys.exit(1)

    if sys.argv[1] == "--status":
        rows = get_applied()
        if not rows:
            print("No applications logged yet.")
            return
        print(f"\n{'Date':<22} {'Status':<12} {'Title':<40} {'Company':<25} URL")
        print("-" * 120)
        for r in rows:
            print(f"{r['applied_at'][:19]:<22} {r['status']:<12} {(r['title'] or '')[:38]:<40} {(r['company'] or '')[:23]:<25} {r['url']}")
        return

    url = sys.argv[1]
    notes = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
    mark_applied(url=url, notes=notes)
    print(f"Logged: {url}")
    if notes:
        print(f"Notes: {notes}")


if __name__ == "__main__":
    main()
