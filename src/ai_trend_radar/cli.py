from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-trend-radar", description="Find AI changes, affected developers, and practical workflow consequences.")
    parser.add_argument("--verbose", action="store_true", help="enable informational logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="collect, rank, and report current opportunities")
    scan.add_argument("--config", default="config.toml", help="TOML configuration path")
    scan.add_argument("--top", type=int, help="maximum number of developer updates")
    video = scan.add_mutually_exclusive_group()
    video.add_argument("--no-youtube", action="store_true", help="skip the optional YouTube appendix")
    video.add_argument("--youtube", action="store_true", help="enable the optional YouTube appendix")
    scan.add_argument("--slack", action="store_true", help="queue and send the changes brief to Slack")
    llm = scan.add_mutually_exclusive_group()
    llm.add_argument("--llm", dest="llm", action="store_true", default=None, help="assess developer impact from captured sources (uses model allowance)")
    llm.add_argument("--no-llm", dest="llm", action="store_false", help="disable model extraction for this scan")

    notify = subparsers.add_parser("notify", help="send the latest saved brief and retry pending Slack delivery")
    notify.add_argument("--config", default="config.toml")

    doctor = subparsers.add_parser("doctor", help="check configuration, storage, credentials, and connectivity")
    doctor.add_argument("--config", default="config.toml", help="TOML configuration path")
    brief = subparsers.add_parser("brief", help="show pending changes from the latest scan without fetching")
    brief.add_argument("--config", default="config.toml")
    brief.add_argument("--top", type=int, help="maximum new discoveries")
    decide = subparsers.add_parser("decide", help="review, defer, or reopen one development")
    decide.add_argument("event_id")
    decide.add_argument("--event", action="store_true", help="explicit legacy event-level operation; otherwise supply a topic ID")
    decide.add_argument("action", choices=["reviewed", "deferred", "reopen"])
    decide.add_argument("--until", help="future ISO timestamp including timezone, for deferred")
    decide.add_argument("--note")
    decide.add_argument("--config", default="config.toml")
    feedback = subparsers.add_parser("feedback", help="record usefulness and prior awareness for one development")
    feedback.add_argument("event_id")
    feedback.add_argument("--event", action="store_true", help="record legacy event feedback; otherwise supply a topic ID")
    feedback.add_argument("verdict", choices=["investigate", "brief", "skip"])
    feedback.add_argument("--known", choices=["yes", "no", "unknown"], default="unknown", help="did you already know this development before Radar surfaced it?")
    feedback.add_argument("--note")
    feedback.add_argument("--revision", type=int, help="reject feedback if this report revision is no longer current")
    feedback.add_argument("--config", default="config.toml")
    summary = subparsers.add_parser("feedback-summary", help="summarize explicit local feedback without fetching")
    summary.add_argument("--json", action="store_true", help="machine-readable counts and limitations")
    summary.add_argument("--config", default="config.toml")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path.cwd() / ".env")
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    # httpx logs full request URLs at INFO, including YouTube's API key query parameter.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if args.command == "scan":
        from ai_trend_radar.pipeline import run_scan

        options = dict(top=args.top, no_youtube=args.no_youtube, slack=args.slack, llm=args.llm)
        if args.youtube:
            options['youtube_enabled'] = True
        return run_scan(Path(args.config), **options)
    if args.command == "notify":
        import json
        import os
        import sqlite3
        import sys
        from ai_trend_radar.config import load_config
        from ai_trend_radar.slack import SlackDelivery, validate_webhook
        try:
            webhook = validate_webhook(os.getenv("SLACK_WEBHOOK_URL"))
            config = load_config(Path(args.config))
            delivery = SlackDelivery(config.database_path, webhook)
            brief_path = config.reports_path / "latest.brief.json"
            if brief_path.is_file():
                delivery.enqueue(json.loads(brief_path.read_text()))
            sent, pending = delivery.send_pending()
            print(f"Slack: {sent} sent; {pending} pending")
            return 2 if pending else 0
        except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, TypeError):
            print("Slack notification failed; check SLACK_WEBHOOK_URL, configuration, storage, and latest.brief.json", file=sys.stderr)
            return 1
    if args.command in {"brief", "decide", "feedback", "feedback-summary"}:
        from datetime import UTC, datetime
        import sqlite3
        import sys
        from ai_trend_radar.config import load_config, ConfigError
        from ai_trend_radar.db import Database
        from ai_trend_radar.state import RadarState, render_brief
        from ai_trend_radar.feedback import record_feedback, feedback_summary, render_feedback_summary
        try:
            config = load_config(Path(args.config))
            database = Database(config.database_path)
            database.initialize()
            from ai_trend_radar.topic_state import TopicState
            state = TopicState(database)
            if args.command in {'decide', 'feedback'} and not args.event:
                args.event_id = state.resolve_topic(args.event_id)
            now = datetime.now(UTC)
            if args.command == "brief":
                top = args.top if args.top is not None else config.top_results
                if top <= 0:
                    raise ValueError("--top must be positive")
                brief = state.brief(now, top)
                print(render_brief(brief), flush=True)
                state.acknowledge(brief)
            elif args.command == "feedback":
                key = record_feedback(database, args.event_id, args.verdict, args.known, now, args.note, args.revision)
                print(f"{key}: {args.verdict}; already known: {args.known}. Review/defer state unchanged.")
            elif args.command == "feedback-summary":
                import json
                summary = feedback_summary(database, 'topics')
                summary['legacy_event_feedback'] = feedback_summary(database, 'legacy')
                print(json.dumps(summary, indent=2) if args.json else render_feedback_summary(summary) + '\n\nLegacy event history (not copied into topic ratings):\n' + render_feedback_summary(summary['legacy_event_feedback']))
            else:
                until = datetime.fromisoformat(args.until.replace("Z", "+00:00")) if args.until else None
                if until is not None and until.tzinfo is None:
                    raise ValueError("--until must include a timezone")
                key = state.decide(args.event_id, args.action, now, until, args.note)
                print(f"{key}: {args.action}")
            return 0
        except (ConfigError, ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            print(f"{args.command} failed: {exc}", file=sys.stderr)
            return 1
    from ai_trend_radar.doctor import run_doctor

    return run_doctor(Path(args.config))
