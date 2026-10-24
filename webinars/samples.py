"""Starter content for new events and the preview seed.

Fictional company ("Fieldnote", a B2B analytics product) and fictional hosts. Copy
follows AIDA: Attention (the hook), Interest (why it matters now), Decision (proof
and what changes for the attendee), Action (the specific ask). Everything here is a
starting point that marketers edit in the studio; nothing implies real customers.
"""

STARTERS = {
    "feature-release": {
        "label": "Feature release",
        "description": "Launch something new: show it live, answer questions, drive adoption.",
        "internal_name": "Q4 launch: Live Dashboards",
        "slug": "live-dashboards-launch",
        "public_title": "Live Dashboards: see your pipeline the moment it moves",
        "summary": (
            "A 45-minute live walkthrough of Live Dashboards, the biggest release of the year, "
            "with the product lead who built it and time for your questions."
        ),
        "attention": "Your weekly pipeline report is already out of date by the time anyone opens it.",
        "interest": (
            "Live Dashboards stream changes from your CRM in seconds, so forecast calls start from "
            "what is true now, not what was true on Monday. We'll build one from scratch on a "
            "realistic dataset and show where teams in beta saved the most time."
        ),
        "decision": (
            "You'll leave knowing exactly which reports to retire, how alerts replace status "
            "meetings, and what the rollout looks like for an admin with an afternoon to spare."
        ),
        "action": "Save your seat. Can't make it live? Register anyway and we'll send the recording.",
        "learning_outcomes": [
            "Build a live pipeline dashboard in under ten minutes",
            "Replace three recurring status reports with alerts",
            "Plan a rollout your sales team will actually adopt",
        ],
        "host": ("Maya Okonkwo", "maya-okonkwo", "Group Product Manager, Fieldnote"),
        "topic": ("Product updates", "product-updates"),
        "thank_you_message": "You're in. We'll email your calendar invite and a reminder an hour before we go live.",
        "duration_minutes": 45,
        "timezone": "America/New_York",
        "hour": 13,
        "days": 6,
    },
    "workshop": {
        "label": "Hands-on workshop",
        "description": "Teach a skill with exercises; attendees leave with something built.",
        "internal_name": "Workshop: Attribution models",
        "slug": "attribution-models-workshop",
        "public_title": "Hands-on workshop: build an attribution model your CFO will trust",
        "summary": (
            "Bring a spreadsheet and 90 minutes. We'll build a defensible multi-touch attribution "
            "model together, step by step, and pressure-test it against real objections."
        ),
        "attention": "Last-click attribution is quietly defunding the channels that create your demand.",
        "interest": (
            "In this working session you'll compare first-touch, last-touch and position-based "
            "models on the same sample dataset, see where each one misleads, and learn the three "
            "checks finance teams use to decide whether to believe a model at all."
        ),
        "decision": (
            "Every exercise uses a template you keep. Attendees in earlier cohorts used it to "
            "rebalance budget within one quarter, with the reasoning written down."
        ),
        "action": "Reserve your place. Seats are capped at 60 so everyone gets help in the exercises.",
        "learning_outcomes": [
            "Compare three attribution models on one dataset",
            "Spot the double-counting that inflates paid channels",
            "Present the model with the caveats finance expects",
        ],
        "host": ("Daniel Reyes", "daniel-reyes", "Head of Marketing Analytics, Fieldnote"),
        "topic": ("Marketing analytics", "marketing-analytics"),
        "thank_you_message": "Your seat is reserved. The workbook link arrives with your reminder the day before.",
        "duration_minutes": 90,
        "timezone": "Europe/London",
        "hour": 15,
        "days": 13,
    },
    "interview": {
        "label": "Expert interview",
        "description": "A conversation with a practitioner; build trust and community.",
        "internal_name": "Interview: Lifecycle at scale",
        "slug": "lifecycle-at-scale-interview",
        "public_title": "In conversation: how a 12-person team runs lifecycle for 2 million users",
        "summary": (
            "A candid interview with a lifecycle lead about what they automated, what they stopped "
            "doing, and the experiment that changed their roadmap. Live Q&A at the end."
        ),
        "attention": "More emails stopped working for them in 2023. Fewer, better ones doubled activation.",
        "interest": (
            "Priya leads lifecycle at a fast-growing fintech. She'll walk through the audit that cut "
            "their sends by 40%, how they decide what deserves a message, and the guardrails that keep "
            "a small team from shipping mistakes to millions of inboxes."
        ),
        "decision": (
            "No slides, no pitch: a practitioner's honest account, with the scoring sheet they use "
            "to approve new campaigns shared with everyone who attends."
        ),
        "action": "Register to join live and put your question to Priya.",
        "learning_outcomes": [
            "The send-audit framework behind a 40% volume cut",
            "How to score a campaign idea before building it",
            "Guardrails for a small team shipping at scale",
        ],
        "host": ("Priya Natarajan", "priya-natarajan", "Director of Lifecycle Marketing"),
        "topic": ("Lifecycle marketing", "lifecycle-marketing"),
        "thank_you_message": "Thanks for registering. Send us your question for Priya by replying to the confirmation email.",
        "duration_minutes": 60,
        "timezone": "Australia/Sydney",
        "hour": 10,
        "days": 20,
    },
}

STARTER_ORDER = ("feature-release", "workshop", "interview")

# Default registration form for new installs: what most B2B webinar forms ask for.
DEFAULT_FORM_VERSION = "standard-v1"
DEFAULT_FORM_DESCRIPTION = "Standard B2B registration form"
DEFAULT_FORM_ROWS = [
    {"key": "email", "label": "Work email", "help": "We'll send your calendar invite here.", "required": True,
     "options": []},
    {"key": "firstName", "label": "First name", "help": "", "required": True, "options": []},
    {"key": "lastName", "label": "Last name", "help": "", "required": True, "options": []},
    {"key": "company", "label": "Company", "help": "", "required": False, "options": []},
    {"key": "jobTitle", "label": "Job title", "help": "", "required": False, "options": []},
    {"key": "companySize", "label": "Company size", "help": "", "required": False,
     "options": ["1-10", "11-50", "51-200", "201-1,000", "1,000+"]},
    {"key": "custom.biggestQuestion", "label": "What do you most want answered?",
     "help": "Optional. Hosts read these before the session.", "required": False, "type": "textarea", "options": []},
]
