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

# DESIGN §28.2: the branch in app.py that counts qr_holds_total, read back from a log line.
_REFUSED_REASONS = frozenset({"quota", "unavailable", "context_length_exceeded",
                              "model_not_found", "no_enabled_model"})


def hold_outcome(reason: Any, status: Any) -> str:
    """The qr_holds_total label for a held request's line, from its reason and status."""
    if reason == "served" or reason == "passthrough":
        return reason
    if reason in _REFUSED_REASONS:
        return "refused"
    if reason == "internal_error":
        return "error"
    if reason == "client_disconnected" and status == 499:
        return "cancelled"
    if reason in ("stream_error", "client_disconnected") and status == 200:
        return "served"  # a held stream that broke after it started (DESIGN §25.6)
    return "unknown"


def _number(value: Any) -> float | None:
    """An int or float that isn't a boolean, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _name(value: Any) -> str | None:
    """A non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def _nearest_rank(ordered: list[float], pct: float) -> float:
    """Nearest-rank percentile of a sorted, non-empty list."""
    rank = max(1, -(-len(ordered) * pct // 100))  # ceil without floats drifting
    return ordered[int(rank) - 1]


def _spread(values: list[float], *pcts: tuple[str, float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    out = {"min": ordered[0]}
    out.update({label: _nearest_rank(ordered, pct) for label, pct in pcts})
    out["max"] = ordered[-1]
    return out


_NOT_DONE_FIELDS = ("end", "error", "chunks", "finish_reason", "usage_seen", "idle_ms")


class _Sections:
    """The DESIGN §28 sections, filled one log line at a time."""

    def __init__(self) -> None:
        self.hold_count = 0
        self.hold_ms: list[float] = []
        self.hold_limits: dict[str, int] = defaultdict(int)
        self.hold_outcomes: dict[str, int] = defaultdict(int)
        self.hold_skipped = 0
        self.first_byte: dict[str, list[float]] = defaultdict(list)
        self.first_byte_skipped = 0
        self.end_counts: dict[tuple[str, str], int] = defaultdict(int)
        self.not_done: list[dict[str, Any]] = []
        self.end_skipped = 0

    def add(self, entry: dict[str, Any]) -> None:
        self._hold(entry)
        self._first_byte(entry)
        self._stream_end(entry)

    def _hold(self, entry: dict[str, Any]) -> None:
        hold = entry.get("hold")
        if hold is None:
            return
        if not isinstance(hold, dict):
            self.hold_skipped += 1
            return
        self.hold_count += 1
        self.hold_outcomes[hold_outcome(entry.get("reason"), entry.get("status"))] += 1
        ms = _number(hold.get("ms"))
        if ms is not None:
            self.hold_ms.append(ms)
        limits = hold.get("limits")
        if isinstance(limits, list):
            for limit in limits:
                if _name(limit) is not None:
                    self.hold_limits[limit] += 1

    def _first_byte(self, entry: dict[str, Any]) -> None:
        raw = entry.get("first_byte_ms")
        if raw is None:
            return
        ms, model = _number(raw), _name(entry.get("served_model"))
        if ms is None or model is None:
            self.first_byte_skipped += 1
            return
        self.first_byte[model].append(ms)

    def _stream_end(self, entry: dict[str, Any]) -> None:
        end = entry.get("stream_end")
        if end is None:
            return
        if not isinstance(end, dict) or _name(end.get("end")) is None:
            self.end_skipped += 1
            return
        finish = end.get("finish_reason")
        if finish is not None and not isinstance(finish, str):
            self.end_skipped += 1
            return
        self.end_counts[(end["end"], finish if finish is not None else "none")] += 1
        if end["end"] != "done":
            row = {"ts": entry.get("ts"), "request_id": entry.get("request_id"),
                   "served_model": entry.get("served_model")}
            row.update({k: end.get(k) for k in _NOT_DONE_FIELDS})
            self.not_done.append(row)

    def result(self) -> dict[str, Any]:
        return {
            "holds": {
                "count": self.hold_count,
                "ms": _spread(self.hold_ms, ("median", 50)),
                "limits": dict(sorted(self.hold_limits.items())),
                "outcomes": dict(sorted(self.hold_outcomes.items())),
                "skipped": self.hold_skipped,
            },
            "first_byte": {
                "models": {model: {"count": len(v), **_spread(v, ("median", 50), ("p90", 90))}
                           for model, v in sorted(self.first_byte.items())},
                "skipped": self.first_byte_skipped,
            },
            "stream_end": {
                "counts": [{"end": e, "finish_reason": f, "count": n}
                           for (e, f), n in sorted(self.end_counts.items())],
                "not_done": self.not_done,
                "skipped": self.end_skipped,
            },
        }


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

    sections = _Sections()

    for entry in logs:
        # Only process quota_router.request logs for route-specific stats
        if not isinstance(entry, dict) or entry.get("logger") != "quota_router.request":
            continue
        sections.add(entry)

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
        **sections.result(),
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

    print_sections(summary)

def _fmt_spread(spread: dict[str, float] | None, unit: str) -> str:
    if spread is None:
        return "none"
    return " / ".join(f"{k} {v:g}" for k, v in spread.items()) + f" {unit}"


def print_sections(summary: dict[str, Any]) -> None:
    """Print the DESIGN §28 sections."""
    holds = summary["holds"]
    print("\n=== Holds ===")
    if holds["count"] == 0:
        print("none")
    else:
        print(f"Held: {holds['count']}")
        print(f"  Waited: {_fmt_spread(holds['ms'], 'ms')}")
        print("  Limits: " + (", ".join(f"{k} {v}" for k, v in holds["limits"].items()) or "none"))
        print("  Outcomes: " + ", ".join(f"{k} {v}" for k, v in holds["outcomes"].items()))
    if holds["skipped"]:
        print(f"  Skipped (malformed): {holds['skipped']}")

    first = summary["first_byte"]
    print("\n=== First byte by model ===")
    if not first["models"]:
        print("none")
    for model, s in first["models"].items():
        rest = {k: v for k, v in s.items() if k != "count"}
        print(f"{model}: {s['count']} × {_fmt_spread(rest, 'ms')}")
    if first["skipped"]:
        print(f"  Skipped (malformed): {first['skipped']}")

    ends = summary["stream_end"]
    print("\n=== Stream endings ===")
    if not ends["counts"]:
        print("none")
    for row in ends["counts"]:
        print(f"{row['end']} / {row['finish_reason']}: {row['count']}")
    if ends["not_done"]:
        print("Not done:")
        for row in ends["not_done"]:
            print("  " + " ".join(f"{k}={row[k]}" for k in row if row[k] is not None))
    if ends["skipped"]:
        print(f"  Skipped (malformed): {ends['skipped']}")


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
