#!/usr/bin/env python3
import argparse
import glob
import json
import sys
from collections import defaultdict
from typing import Any, Iterable, TextIO

def parse_logs(stream: TextIO) -> Iterable[dict[str, Any]]:
    """Parse JSON log lines from a stream."""
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue

def summarize(logs: Iterable[dict[str, Any]], top_n: int | None = None) -> dict[str, Any]:
    """Summarize log entries into route statistics, status codes, and retry metrics."""
    # Group by (route, route_reason, route_tier, route_rule)
    # route is not strictly defined in requirements but usually present in such logs.
    # We'll use 'method' and 'path' or just 'route' if available.
    
    route_stats = defaultdict(lambda: {
        "total": 0,
        "2xx": 0,
        "4xx": 0,
        "5xx": 0,
    })
    
    # HTTP status metrics
    status_stats = defaultdict(int)
    
    # Retry-after metrics for 429 and 503
    retry_metrics = {
        429: {"count": 0, "delays": []},
        503: {"count": 0, "delays": []},
    }

    for entry in logs:
        # Only process quota_router.request logs for route-specific stats
        if entry.get("logger") != "quota_router.request":
            continue
            
        status = entry.get("status")
        if status is None:
            continue
            
        status_stats[status] += 1
        
        route = entry.get("route", "unknown")
        reason = entry.get("route_reason", "none")
        tier = entry.get("route_tier", "none")
        rule = entry.get("route_rule", "none")
        
        key = (route, reason, tier, rule)
        stats = route_stats[key]
        stats["total"] += 1
        
        if 200 <= status < 300:
            stats["2xx"] += 1
        elif 400 <= status < 500:
            stats["4xx"] += 1
        elif 500 <= status < 600:
            stats["5xx"] += 1

        if status in (429, 503):
            retry_after = entry.get("retry_after_s")
            if retry_after is not None:
                retry_metrics[status]["count"] += 1
                retry_metrics[status]["delays"].append(float(retry_after))

    # Format route table data
    table_rows = []
    for (route, reason, tier, rule), stats in route_stats.items():
        table_rows.append({
            "route": route,
            "reason": reason,
            "tier": tier,
            "rule": rule,
            "total": stats["total"],
            "2xx": stats["2xx"],
            "4xx": stats["4xx"],
            "5xx": stats["5xx"],
        })
    
    # Sort by total descending
    table_rows.sort(key=lambda x: x["total"], reverse=True)
    if top_n:
        table_rows = table_rows[:top_n]

    # Format retry stats
    retry_summary = {}
    for code in (429, 503):
        delays = retry_metrics[code]["delays"]
        if not delays:
            retry_summary[code] = {
                "total_errors": status_stats[code],
                "with_retry_after": 0,
                "min": 0.0,
                "max": 0.0,
                "avg": 0.0,
            }
        else:
            retry_summary[code] = {
                "total_errors": status_stats[code],
                "with_retry_after": len(delays),
                "min": min(delays),
                "max": max(delays),
                "avg": sum(delays) / len(delays),
            }

    return {
        "routes": table_rows,
        "statuses": dict(sorted(status_stats.items())),
        "retry_after": retry_summary,
    }

def print_table(rows: list[dict[str, Any]]) -> None:
    """Print a formatted table of route statistics."""
    if not rows:
        print("No route data found.")
        return

    headers = ["Route", "Reason", "Tier", "Rule", "Total", "2xx", "4xx", "5xx"]
    col_widths = [len(h) for h in headers]
    
    # Data rows
    display_rows = []
    for r in rows:
        row = [
            str(r["route"]),
            str(r["reason"]),
            str(r["tier"]),
            str(r["rule"]),
            str(r["total"]),
            str(r["2xx"]),
            str(r["4xx"]),
            str(r["5xx"]),
        ]
        display_rows.append(row)
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(val))

    fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
    print(fmt.format(*headers))
    print("-" * (sum(col_widths) + 2 * (len(headers) - 1)))
    for row in display_rows:
        print(fmt.format(*row))

def print_text_summary(summary: dict[str, Any]) -> None:
    """Print a text summary of the log analysis."""
    print("=== Route Summary ===")
    print_table(summary["routes"])
    print("\n=== Status Codes ===")
    for code, count in summary["statuses"].items():
        print(f"{code}: {count}")
    
    print("\n=== Retry-After Metrics ===")
    for code in [429, 503]:
        s = summary["retry_after"][code]
        print(f"Status {code}:")
        print(f"  Total errors: {s['total_errors']}")
        print(f"  With retry_after: {s['with_retry_after']}")
        if s['with_retry_after'] > 0:
            print(f"  Delay (min/max/avg): {s['min']:.2f} / {s['max']:.2f} / {s['avg']:.2f} seconds")

def main() -> None:
    """Main entry point for the log summarizer script."""
    parser = argparse.ArgumentParser(description="Summarize log metadata from JSONL files.")
    parser.add_argument("files", nargs="*", help="JSONL log files or globs. Use '-' or omit for stdin.")
    parser.add_argument("-j", "--json", action="store_true", help="Output in JSON format.")
    parser.add_argument("-n", "--top", type=int, help="Limit number of rows in the route table.")
    args = parser.parse_args()

    def get_input_streams():
        """Yield input streams from arguments or stdin."""
        if not args.files or args.files == ["-"]:
            yield sys.stdin
            return
        
        for pattern in args.files:
            if pattern == "-":
                yield sys.stdin
                continue
            filenames = glob.glob(pattern)
            if not filenames:
                print(f"Warning: no files matched '{pattern}'", file=sys.stderr)
                continue
            for fname in sorted(filenames):
                with open(fname, "r", encoding="utf-8") as f:
                    yield f

    def all_logs():
        """Yield all parsed log entries from input streams."""
        for stream in get_input_streams():
            yield from parse_logs(stream)

    summary = summarize(all_logs(), top_n=args.top)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print_text_summary(summary)

if __name__ == "__main__":
    main()
