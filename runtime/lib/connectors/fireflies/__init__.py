"""Fireflies.ai support connector — read-only GraphQL transcript search and summary reads.

Force-code trigger:
  (5) GraphQL transport / search DSL. The connector owns the query documents so the read-tier path
      cannot drift into Fireflies mutations.

CLI:
    python -m lib.connectors.fireflies search --participant customer@example.com --limit 10
    python -m lib.connectors.fireflies search --organizer teammate@example.com --from-date 2026-07-01
    python -m lib.connectors.fireflies search --keyword onboarding --scope all
    python -m lib.connectors.fireflies show <transcript_id>
    python -m lib.connectors.fireflies show <transcript_id> --sentences-file /tmp/fireflies.txt
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests as _requests

from lib import api, oauth

API_BASE = "https://api.fireflies.ai/graphql"
MAX_SEARCH_LIMIT = 50

MANIFEST = api.register(
    api.Manifest(
        key="fireflies",
        base_url=API_BASE,
        auth=api.Auth(strategy="bearer"),
        pagination=api.Pagination(style="none"),
        rate_limit_remaining_header="",
    )
)

SEARCH_QUERY = """
query FirefliesTranscripts(
  $organizers: [String]
  $participants: [String]
  $fromDate: DateTime
  $toDate: DateTime
  $keyword: String
  $scope: TranscriptsQueryScope
  $limit: Int
  $skip: Int
) {
  transcripts(
    organizers: $organizers
    participants: $participants
    fromDate: $fromDate
    toDate: $toDate
    keyword: $keyword
    scope: $scope
    limit: $limit
    skip: $skip
  ) {
    id
    title
    date
    dateString
    duration
    organizer_email
    participants
    transcript_url
    meeting_link
    meeting_info {
      summary_status
      fred_joined
      silent_meeting
    }
    summary {
      short_summary
      gist
      action_items
    }
  }
}
"""

TRANSCRIPT_QUERY = """
query FirefliesTranscript($transcriptId: String!) {
  transcript(id: $transcriptId) {
    id
    title
    date
    dateString
    duration
    organizer_email
    participants
    transcript_url
    audio_url
    video_url
    meeting_link
    privacy
    meeting_info {
      summary_status
      fred_joined
      silent_meeting
    }
    meeting_attendees {
      displayName
      email
      phoneNumber
      name
      location
    }
    meeting_attendance {
      name
      join_time
      leave_time
    }
    speakers {
      id
      name
    }
    summary {
      keywords
      action_items
      outline
      shorthand_bullet
      overview
      bullet_gist
      gist
      short_summary
      short_overview
      meeting_type
      topics_discussed
      transcript_chapters
    }
    sentences {
      index
      speaker_name
      speaker_id
      text
      raw_text
      start_time
      end_time
    }
  }
}
"""


def _request(query: str, variables: dict[str, Any]) -> Any:
    token = oauth.token("fireflies")
    resp = _requests.post(
        API_BASE,
        json={"query": query, "variables": variables},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=(api.DEFAULT_CONNECT_TIMEOUT, api.DEFAULT_READ_TIMEOUT),
    )
    if not (200 <= resp.status_code < 300):
        raise api.ApiError(resp.status_code, resp.text, url=API_BASE)
    try:
        body = resp.json()
    except ValueError:
        raise api.ApiError(resp.status_code, f"non-JSON response: {resp.text[:200]}", url=API_BASE)
    if body.get("errors"):
        raise api.ApiError(resp.status_code, json.dumps(body["errors"], separators=(",", ":")), url=API_BASE)
    return body.get("data") or {}


def _date_arg(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return f"{raw}T00:00:00.000Z"
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date {value!r}; use YYYY-MM-DD or ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _emails(values: list[str] | None) -> list[str] | None:
    cleaned = [v.strip() for v in (values or []) if v.strip()]
    return cleaned or None


def search_transcripts(
    *,
    organizers: list[str] | None = None,
    participants: list[str] | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    keyword: str | None = None,
    scope: str | None = None,
    limit: int = 10,
    skip: int = 0,
) -> list[dict[str, Any]]:
    """Search transcript metadata by participant/organizer/date/keyword using Fireflies' read query."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if limit > MAX_SEARCH_LIMIT:
        raise ValueError("Fireflies transcript search limit must be <= 50")
    variables = {
        "organizers": _emails(organizers),
        "participants": _emails(participants),
        "fromDate": _date_arg(from_date),
        "toDate": _date_arg(to_date),
        "keyword": keyword or None,
        "scope": scope or None,
        "limit": limit,
        "skip": skip,
    }
    data = _request(SEARCH_QUERY, variables)
    return [compact_transcript(t, include_sentences=False) for t in (data.get("transcripts") or [])]


def get_transcript(transcript_id: str) -> dict[str, Any]:
    """Read one transcript summary, metadata, attendees, and sentences."""
    data = _request(TRANSCRIPT_QUERY, {"transcriptId": transcript_id})
    transcript = data.get("transcript")
    if not transcript:
        raise api.ApiError(404, f"transcript not found or unavailable: {transcript_id}", url=API_BASE)
    return compact_transcript(transcript, include_sentences=True)


def compact_transcript(raw: dict[str, Any], *, include_sentences: bool) -> dict[str, Any]:
    summary = raw.get("summary") or {}
    meeting_info = raw.get("meeting_info") or {}
    compact: dict[str, Any] = {
        "id": raw.get("id"),
        "title": raw.get("title"),
        "date": raw.get("dateString") or raw.get("date"),
        "duration": raw.get("duration"),
        "organizer_email": raw.get("organizer_email"),
        "participants": raw.get("participants") or [],
        "transcript_url": raw.get("transcript_url"),
        "meeting_link": raw.get("meeting_link"),
        "summary_status": meeting_info.get("summary_status"),
        "short_summary": summary.get("short_summary") or summary.get("short_overview") or summary.get("gist"),
        "action_items": summary.get("action_items"),
        "topics_discussed": summary.get("topics_discussed"),
        "keywords": summary.get("keywords"),
    }
    if include_sentences:
        compact.update(
            {
                "privacy": raw.get("privacy"),
                "audio_url": raw.get("audio_url"),
                "video_url": raw.get("video_url"),
                "meeting_attendees": raw.get("meeting_attendees") or [],
                "meeting_attendance": raw.get("meeting_attendance") or [],
                "speakers": raw.get("speakers") or [],
                "sentences": raw.get("sentences") or [],
            }
        )
    return compact


def sentences_text(transcript: dict[str, Any]) -> str:
    lines: list[str] = []
    for sentence in transcript.get("sentences") or []:
        speaker = sentence.get("speaker_name") or sentence.get("speaker_id") or "Speaker"
        start = sentence.get("start_time")
        stamp = f"[{start}] " if start not in (None, "") else ""
        text = sentence.get("text") or sentence.get("raw_text") or ""
        if text:
            lines.append(f"{stamp}{speaker}: {text}")
    return "\n".join(lines) + ("\n" if lines else "")


def _list_value(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v)
    return str(value)


def _summary_lines(t: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if t.get("date"):
        lines.append(f"- Date: {t['date']}")
    if t.get("duration") not in (None, ""):
        lines.append(f"- Duration: {t['duration']}")
    if t.get("organizer_email"):
        lines.append(f"- Organizer: {t['organizer_email']}")
    participants = _list_value(t.get("participants"))
    if participants:
        lines.append(f"- Participants: {participants}")
    if t.get("summary_status"):
        lines.append(f"- Summary status: {t['summary_status']}")
    if t.get("transcript_url"):
        lines.append(f"- Transcript URL: {t['transcript_url']}")
    if t.get("meeting_link"):
        lines.append(f"- Meeting link: {t['meeting_link']}")
    if t.get("short_summary"):
        lines.extend(["", str(t["short_summary"])])
    action_items = _list_value(t.get("action_items"))
    if action_items:
        lines.extend(["", f"Action items: {action_items}"])
    return lines


def search_to_markdown(results: list[dict[str, Any]]) -> str:
    lines = [f"# Fireflies transcripts ({len(results)})", ""]
    for t in results:
        title = t.get("title") or t.get("id") or "Untitled transcript"
        lines.append(f"## {title}")
        if t.get("id"):
            lines.append(f"- ID: `{t['id']}`")
        lines.extend(_summary_lines(t))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def transcript_to_markdown(transcript: dict[str, Any], *, sentences_file: str | None = None) -> str:
    title = transcript.get("title") or transcript.get("id") or "Fireflies transcript"
    lines = [f"# {title}", ""]
    if transcript.get("id"):
        lines.append(f"- ID: `{transcript['id']}`")
    lines.extend(_summary_lines(transcript))
    if transcript.get("meeting_attendees"):
        lines.extend(["", "## Attendees"])
        for attendee in transcript["meeting_attendees"]:
            name = attendee.get("displayName") or attendee.get("name") or attendee.get("email") or "Attendee"
            email = attendee.get("email")
            lines.append(f"- {name}" + (f" <{email}>" if email and email != name else ""))
    if sentences_file:
        path = Path(sentences_file)
        path.write_text(sentences_text(transcript), encoding="utf-8")
        lines.extend(["", f"Full transcript sentences written to `{path}`."])
    elif transcript.get("sentences"):
        lines.extend(["", f"Sentence count: {len(transcript['sentences'])}. Use --sentences-file /tmp/fireflies.txt for full text."])
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.fireflies")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="search transcript metadata by attendee/organizer/date/keyword")
    s.add_argument("--participant", action="append", dest="participants", help="participant email; repeatable")
    s.add_argument("--organizer", action="append", dest="organizers", help="organizer email; repeatable")
    s.add_argument("--from-date", help="YYYY-MM-DD or ISO-8601 lower bound")
    s.add_argument("--to-date", help="YYYY-MM-DD or ISO-8601 upper bound")
    s.add_argument("--keyword", help="keyword search in title/sentences")
    s.add_argument("--scope", choices=["title", "sentences", "all"], help="keyword scope")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--skip", type=int, default=0)

    sh = sub.add_parser("show", help="show one transcript summary/metadata")
    sh.add_argument("transcript_id")
    sh.add_argument("--sentences-file", help="write full transcript sentences here instead of stdout")

    args = parser.parse_args(argv)

    if args.cmd == "search":
        results = search_transcripts(
            organizers=args.organizers,
            participants=args.participants,
            from_date=args.from_date,
            to_date=args.to_date,
            keyword=args.keyword,
            scope=args.scope,
            limit=args.limit,
            skip=args.skip,
        )
        print(search_to_markdown(results), end="")
    elif args.cmd == "show":
        transcript = get_transcript(args.transcript_id)
        print(transcript_to_markdown(transcript, sentences_file=args.sentences_file), end="")
    else:
        parser.error("unknown command")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
