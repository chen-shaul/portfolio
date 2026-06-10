import os
import smtplib
import requests
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from notion_client import Client as NotionClient
import anthropic

# ── Secrets (injected by GitHub Actions) ──────────────────────────────────────
TIMELESS_TOKEN      = os.environ["TIMELESS_TOKEN"]
NOTION_TOKEN        = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID  = os.environ["NOTION_DATABASE_ID"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
GMAIL_APP_PASSWORD  = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_TO            = os.environ["GMAIL_TO"]
GMAIL_FROM          = os.environ.get("GMAIL_FROM", GMAIL_TO)

TIMELESS_BASE = "https://api.timeless.day/v1"

# ── Step 1: Check Timeless for a meeting in the next 30 min ───────────────────
def get_upcoming_meeting():
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(minutes=30)

    headers = {"Authorization": f"Bearer {TIMELESS_TOKEN}"}
    resp = requests.get(
        f"{TIMELESS_BASE}/meetings",
        headers=headers,
        params={"status": "scheduled", "limit": 10},
    )
    resp.raise_for_status()
    meetings = resp.json().get("data", [])

    for meeting in meetings:
        start_str = meeting.get("start_time", "")
        if not start_str:
            continue
        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        if now <= start_dt <= window_end:
            return meeting

    return None


# ── Step 2: Fetch last completed transcript for this customer ─────────────────
def get_last_transcript(company_name: str) -> str:
    headers = {"Authorization": f"Bearer {TIMELESS_TOKEN}"}
    resp = requests.get(
        f"{TIMELESS_BASE}/meetings",
        headers=headers,
        params={"company": company_name, "status": "completed", "limit": 1},
    )
    resp.raise_for_status()
    meetings = resp.json().get("data", [])

    if not meetings:
        return "No previous meeting transcript found for this customer."

    last_meeting_id = meetings[0]["id"]
    last_meeting_title = meetings[0].get("title", "Previous meeting")
    last_meeting_date = meetings[0].get("start_time", "")[:10]

    transcript_resp = requests.get(
        f"{TIMELESS_BASE}/meetings/{last_meeting_id}/transcript",
        headers=headers,
    )
    if transcript_resp.status_code != 200:
        return f"Transcript not available for last meeting ({last_meeting_title}, {last_meeting_date})."

    segments = transcript_resp.json().get("segments", [])
    lines = []
    for seg in segments[:80]:  # cap at 80 segments to stay within token budget
        speaker = seg.get("speaker", "Unknown")
        text = seg.get("text", "").strip()
        if text:
            lines.append(f"{speaker}: {text}")

    transcript_text = "\n".join(lines) if lines else "Transcript is empty."
    return f"=== Last meeting: {last_meeting_title} ({last_meeting_date}) ===\n{transcript_text}"


# ── Step 3: Fetch customer record from Notion ─────────────────────────────────
def get_notion_record(company_name: str) -> dict:
    notion = NotionClient(auth=NOTION_TOKEN)

    results = notion.databases.query(
        database_id=NOTION_DATABASE_ID,
        filter={
            "property": "Company",
            "title": {"contains": company_name},
        },
    ).get("results", [])

    if not results:
        return {"found": False, "company": company_name}

    page = results[0]
    props = page.get("properties", {})

    def get_text(prop):
        val = props.get(prop, {})
        ptype = val.get("type", "")
        if ptype == "title":
            items = val.get("title", [])
        elif ptype == "rich_text":
            items = val.get("rich_text", [])
        else:
            return ""
        return "".join(i.get("plain_text", "") for i in items)

    def get_select(prop):
        val = props.get(prop, {}).get("select") or {}
        return val.get("name", "")

    def get_number(prop):
        return props.get(prop, {}).get("number")

    def get_date(prop):
        val = props.get(prop, {}).get("date") or {}
        return val.get("start", "")

    return {
        "found": True,
        "company":       get_text("Company"),
        "contact":       get_text("Main Contact"),
        "deal_stage":    get_select("Deal Stage"),
        "health_score":  get_number("Health Score"),
        "renewal_date":  get_date("Renewal Date"),
        "arr":           get_number("ARR"),
        "open_tickets":  get_number("Open Tickets"),
        "last_touchpoint": get_date("Last Touchpoint"),
        "notes":         get_text("Notes"),
    }


# ── Step 4: Generate brief with Claude ────────────────────────────────────────
def generate_brief(meeting: dict, notion: dict, transcript: str) -> str:
    meeting_title    = meeting.get("title", "Customer meeting")
    meeting_time     = meeting.get("start_time", "")[:16].replace("T", " ")
    participants     = ", ".join(
        p.get("name", "") for p in meeting.get("participants", [])
    ) or "Unknown"

    notion_block = ""
    if notion["found"]:
        notion_block = f"""
CRM record (Notion):
- Company: {notion['company']}
- Main contact: {notion['contact']}
- Deal stage: {notion['deal_stage']}
- Health score: {notion['health_score']}
- ARR: ${notion['arr']:,} if {notion['arr']} else 'N/A'
- Renewal date: {notion['renewal_date']}
- Open tickets: {notion['open_tickets']}
- Last touchpoint: {notion['last_touchpoint']}
- Notes: {notion['notes']}
""".strip()
    else:
        notion_block = f"No Notion record found for '{notion['company']}'."

    prompt = f"""You are a CSM assistant. Generate a pre-call brief for the following meeting.

MEETING
Title: {meeting_title}
Time: {meeting_time} UTC
Attendees: {participants}

{notion_block}

LAST CALL TRANSCRIPT
{transcript}

Generate a structured brief with these four sections. Be specific and concise.

## Customer snapshot
2-3 sentences covering company context, deal stage, health, and renewal urgency.

## Risk signals
List up to 3 risks based on open tickets, low health score, long gap since last touchpoint, or anything concerning from the transcript. If no risks, say "No major risks detected."

## Suggested agenda
5 numbered agenda items tailored to this customer's current situation. Reference specific open items or transcript topics where relevant.

## Discovery questions
3 sharp questions the CSM should ask, grounded in the transcript and CRM data. Not generic — specific to this customer right now.
"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ── Step 5: Send HTML email via Gmail ─────────────────────────────────────────
def send_email(meeting: dict, brief_text: str):
    meeting_title = meeting.get("title", "Upcoming meeting")
    meeting_time  = meeting.get("start_time", "")[:16].replace("T", " ")

    # Convert markdown-style brief to simple HTML
    html_body = brief_text.replace("\n", "<br>")
    for heading in ["## Customer snapshot", "## Risk signals", "## Suggested agenda", "## Discovery questions"]:
        label = heading.replace("## ", "")
        html_body = html_body.replace(
            heading,
            f'<h3 style="margin:20px 0 6px;font-family:sans-serif;font-size:14px;font-weight:600;color:#111">{label}</h3>',
        )

    html = f"""
<html><body style="font-family:sans-serif;font-size:14px;color:#333;max-width:640px;margin:auto;padding:24px">
  <div style="background:#f5f5f5;border-radius:8px;padding:16px 20px;margin-bottom:20px">
    <p style="margin:0;font-size:13px;color:#666">Meeting prep brief</p>
    <p style="margin:4px 0 0;font-size:18px;font-weight:600;color:#111">{meeting_title}</p>
    <p style="margin:4px 0 0;font-size:13px;color:#666">{meeting_time} UTC</p>
  </div>
  <div style="line-height:1.7">{html_body}</div>
  <p style="margin-top:32px;font-size:12px;color:#aaa">Generated by your CSM agent</p>
</body></html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Brief: {meeting_title} at {meeting_time}"
    msg["From"]    = GMAIL_FROM
    msg["To"]      = GMAIL_TO
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_FROM, GMAIL_TO, msg.as_string())

    print(f"Brief sent to {GMAIL_TO}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print("Checking for upcoming meetings...")
    meeting = get_upcoming_meeting()

    if not meeting:
        print("No meeting in the next 30 minutes. Done.")
        return

    meeting_title = meeting.get("title", "")
    participants  = meeting.get("participants", [])

    # Infer company name: try participant company field first, then meeting title
    company_name = ""
    for p in participants:
        if p.get("company"):
            company_name = p["company"]
            break
    if not company_name:
        company_name = meeting_title  # fallback: use title as search term

    print(f"Meeting found: {meeting_title} | Company: {company_name}")

    print("Fetching last transcript from Timeless...")
    transcript = get_last_transcript(company_name)

    print("Fetching Notion CRM record...")
    notion = get_notion_record(company_name)

    print("Generating brief with Claude...")
    brief = generate_brief(meeting, notion, transcript)

    print("Sending email...")
    send_email(meeting, brief)


if __name__ == "__main__":
    main()
