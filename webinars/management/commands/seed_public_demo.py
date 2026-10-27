"""Seed the public demo catalogue: six fictional, clearly labelled events.

``python manage.py seed_public_demo [--dry-run] [--allow-registration]``

* Four upcoming and two past events for "Harborlight", a made-up B2B analytics
  company. Every public title starts with "Demo:", every summary and thank-you
  message says the event is fictional, and every host bio starts "Fictional demo host."
* Everything is namespaced (``demo-`` slugs, ``[Demo]`` internal names, ICS UIDs on
  ``demo-seed.invalid``) and recorded as seed-owned with a ``demo_seed.owned`` audit
  entry. Before writing anything the command checks every slug, form version and UID:
  if one already belongs to data the seed did not create, it stops with no changes.
* Re-runs are idempotent. Seed-owned hosts, topics and events converge to the
  definitions below (a new event revision only when something differs). Form versions
  and sessions are never modified once created, so demo dates stay stable.
* No people, registrations, consents, destinations or outbox rows are created and no
  mail or webhooks are sent. Sessions have no join link and no waiting room, and new
  sessions are created with registration closed unless ``--allow-registration`` is given.
* Past sessions are completed, with a published recording link that points to a
  demonstration page on the demo site (``RECORDING_PAGE_BASE``), never a real recording.
* Images are served by the demo frontend: ``ART_BASE + <slug>.svg`` for every event
  and host slug (see ``art_slugs()``).
"""
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from webinars import services
from webinars.admin import HostForm
from webinars.audit import audit
from webinars.models import AuditLog, Destination, Event, FormVersion, Host, Occurrence, Topic
from webinars.timeutils import local_to_utc

OWNED = "demo_seed.owned"
ART_BASE = "https://visioncast-ten.vercel.app/demo-art/"
RECORDING_PAGE_BASE = "https://visioncast-ten.vercel.app/demo-recording/"
ICS_DOMAIN = "demo-seed.invalid"
INTERNAL_PREFIX = "[Demo] "
TITLE_PREFIX = "Demo: "
FICTIONAL_NOTE = (
    "Fictional demo event on the VisionCast showcase: Harborlight is not a real company "
    "and this session is not scheduled to take place."
)
THANK_YOU = (
    "Thanks for trying the demo. This is a fictional event, so no session is scheduled "
    "and no emails or calendar invites will be sent."
)
# Hours below are mid-day local time, so no DST gap or overlap applies.

TOPICS = [
    ("demo-product-launches", "Product launches"),
    ("demo-demand-generation", "Demand generation"),
    ("demo-marketing-analytics", "Marketing analytics"),
    ("demo-lifecycle-marketing", "Lifecycle marketing"),
    ("demo-customer-education", "Customer education"),
]

HOSTS = [
    {
        "slug": "demo-rowan-achterberg",
        "name": "Rowan Achterberg",
        "title": "Group Product Manager, Harborlight (fictional)",
        "bio": (
            "Fictional demo host. Rowan leads the forecasting product group at Harborlight, a made-up "
            "B2B analytics company used on the VisionCast demo site. Before Harborlight, Rowan spent six "
            "years in sales operations building the weekly pipeline reports that Pipeline Signals now replaces."
        ),
    },
    {
        "slug": "demo-selin-marchetti",
        "name": "Selin Marchetti",
        "title": "Head of Demand Generation, Harborlight (fictional)",
        "bio": (
            "Fictional demo host. Selin runs Harborlight's webinar programme: around fifty sessions a year "
            "with a team of three. Selin's focus is the unglamorous middle of the funnel: forms that ask only "
            "what sales will use, reminder timing, and follow-up that turns no-shows into recording views."
        ),
    },
    {
        "slug": "demo-tomas-lindqvist",
        "name": "Tomas Lindqvist",
        "title": "Marketing Analytics Lead, Harborlight (fictional)",
        "bio": (
            "Fictional demo host. Tomas owns attribution and reporting at Harborlight and is the person "
            "finance calls when two dashboards disagree. Tomas has rebuilt Harborlight's measurement around "
            "first-party registration data and explicit consent records."
        ),
    },
    {
        "slug": "demo-kei-nakamura-ross",
        "name": "Kei Nakamura-Ross",
        "title": "Director of Lifecycle Marketing, Harborlight (fictional)",
        "bio": (
            "Fictional demo host. Kei leads lifecycle marketing at Harborlight, from onboarding sequences to "
            "win-back campaigns. Kei is known for cutting send volume and raising response, and for teardown "
            "sessions that rewrite real-looking emails line by line."
        ),
    },
    {
        "slug": "demo-amara-osei",
        "name": "Amara Osei",
        "title": "Customer Education Manager, Harborlight (fictional)",
        "bio": (
            "Fictional demo host. Amara runs Harborlight's customer education team: onboarding office hours, "
            "the admin certification track and the help-centre courses. Amara has answered most first-month "
            "setup questions more than once and keeps the list of the ones worth answering live."
        ),
    },
]

FORMS = [
    {
        "version": "demo-standard-v1",
        "meta": {
            "description": "Fictional demo form: standard B2B registration",
            "field_mapping": {"work_email": "email"},
            "is_active": True,
        },
        "rows": [
            {"key": "email", "label": "Work email", "help": "Demo only: nothing is sent to this address.",
             "required": True, "options": []},
            {"key": "firstName", "label": "First name", "help": "", "required": True, "options": []},
            {"key": "lastName", "label": "Last name", "help": "", "required": True, "options": []},
            {"key": "company", "label": "Company", "help": "", "required": False, "options": []},
            {"key": "jobTitle", "label": "Job title", "help": "", "required": False, "options": []},
            {"key": "companySize", "label": "Company size", "help": "", "required": False,
             "options": ["1-10", "11-50", "51-200", "201-1,000", "1,000+"]},
            {"key": "custom.biggestQuestion", "label": "What do you most want answered?",
             "help": "Optional. Hosts read these before the session.", "required": False, "type": "textarea",
             "options": []},
        ],
    },
    {
        "version": "demo-workshop-v1",
        "meta": {
            "description": "Fictional demo form: hands-on workshop registration",
            "field_mapping": {"work_email": "email"},
            "is_active": True,
        },
        "rows": [
            {"key": "email", "label": "Work email", "help": "Demo only: nothing is sent to this address.",
             "required": True, "options": []},
            {"key": "firstName", "label": "First name", "help": "", "required": True, "options": []},
            {"key": "lastName", "label": "Last name", "help": "", "required": True, "options": []},
            {"key": "company", "label": "Company", "help": "", "required": False, "options": []},
            {"key": "jobTitle", "label": "Job title", "help": "", "required": False, "options": []},
            {"key": "custom.programmeStage", "label": "Where is your webinar programme today?",
             "help": "Helps the hosts pitch the exercises at the right level.", "required": False,
             "type": "select", "options": ["Planning our first", "A few a year", "Monthly or more"]},
            {"key": "custom.teamSize", "label": "Joining with colleagues?", "help": "", "required": False,
             "type": "select", "options": ["Just me", "2-5 colleagues", "6 or more"]},
            {"key": "custom.funnelChallenge", "label": "Which part of your funnel leaks most?",
             "help": "Optional. We use answers to choose the breakout examples.", "required": False,
             "type": "textarea", "options": []},
        ],
    },
]

EVENTS = [
    {
        "slug": "demo-pipeline-signals-launch",
        "internal_name": "Launch: Pipeline Signals",
        "public_title": "Pipeline Signals launch: spot stalled deals before the forecast call",
        "summary": (
            "A 45-minute live launch of Pipeline Signals with the product lead who built it: a walkthrough "
            "on a realistic sample pipeline, the rollout plan, and open Q&A."
        ),
        "attention": "Most stalled deals were visible two weeks before anyone said so in a forecast call.",
        "interest": (
            "Pipeline Signals watches stage age, champion engagement and meeting gaps across every open "
            "opportunity, then flags the handful that need attention this week. Rowan turns it on against a "
            "sample pipeline and walks through each alert as it fires."
        ),
        "decision": (
            "You'll see exactly what changes for reps (one prioritised list instead of a dashboard tour), for "
            "managers (forecast reviews that start from risk) and for admins (a 20-minute setup with no new fields)."
        ),
        "action": "Save your seat for the live launch. Can't make the time? Register anyway and get the walkthrough.",
        "learning_outcomes": [
            "Read the three signals that predict a stalled deal",
            "Route alerts to reps and managers without new CRM fields",
            "Run a forecast review that starts from risk, not totals",
        ],
        "hosts": ["demo-rowan-achterberg"],
        "topics": ["demo-product-launches", "demo-marketing-analytics"],
        "form": "demo-standard-v1",
        "register_label": "Save my seat",
        "confirm_label": "Confirm my seat",
        "sessions": [{"timezone": "America/New_York", "days": 9, "hour": 13, "duration": 45}],
    },
    {
        "slug": "demo-webinar-funnel-workshop",
        "internal_name": "Workshop: Webinar funnel",
        "public_title": "Hands-on workshop: build a webinar funnel that fills the room",
        "summary": (
            "A 90-minute working session: rebuild a webinar registration funnel step by step on a sample "
            "programme and leave with the worksheet and reminder template."
        ),
        "attention": "Half of webinar registrants never show up, and most programmes only find out on the day.",
        "interest": (
            "You'll rebuild a funnel end to end: the landing page hook, a form that asks only what sales will "
            "use, reminder timing that lifts attendance, and the follow-up that turns no-shows into recording "
            "views. Every exercise uses a sample programme with realistic numbers."
        ),
        "decision": (
            "You keep the funnel worksheet and the reminder schedule template. Each exercise ends with a "
            "before-and-after, so you can show your team why the change is worth making."
        ),
        "action": "Reserve a workshop seat. Places are capped at 40 so both hosts can review every funnel.",
        "learning_outcomes": [
            "Write a landing page hook that earns the registration",
            "Cut a registration form to the fields sales actually uses",
            "Schedule reminders that lift live attendance",
            "Turn no-shows into recording views with one follow-up",
        ],
        "hosts": ["demo-selin-marchetti", "demo-tomas-lindqvist"],
        "topics": ["demo-demand-generation", "demo-marketing-analytics"],
        "form": "demo-workshop-v1",
        "register_label": "Reserve my workshop seat",
        "confirm_label": "Confirm my workshop seat",
        "sessions": [{"timezone": "Europe/London", "days": 16, "hour": 14, "duration": 90}],
    },
    {
        "slug": "demo-consent-first-attribution",
        "internal_name": "Panel: Consent-first attribution",
        "public_title": "Panel: attribution that survives the consent banner",
        "summary": (
            "Two analytics practitioners compare how they measure webinar-sourced pipeline when tracking is "
            "optional. Sixty minutes, with live Q&A."
        ),
        "attention": "If a third of visitors decline tracking, your attribution report describes someone else's funnel.",
        "interest": (
            "Tomas and Kei walk through first-party registration data, explicit consent records and the "
            "monthly reconciliation that keeps marketing and finance reading the same number."
        ),
        "decision": (
            "Specifics, not a pitch: the fields they capture at registration, the reports they stopped "
            "trusting, and a checklist for auditing your own consent-aware attribution."
        ),
        "action": "Register to join the panel live and bring your hardest attribution question.",
        "learning_outcomes": [
            "Measure webinar pipeline from first-party registration data",
            "Keep consent evidence that stands up to an audit",
            "Reconcile marketing and finance numbers every month",
        ],
        "hosts": ["demo-tomas-lindqvist", "demo-kei-nakamura-ross"],
        "topics": ["demo-marketing-analytics", "demo-demand-generation"],
        "form": "demo-standard-v1",
        "register_label": "Join the panel",
        "confirm_label": "Confirm my place",
        "sessions": [{"timezone": "America/Los_Angeles", "days": 23, "hour": 10, "duration": 60}],
    },
    {
        "slug": "demo-onboarding-office-hours",
        "internal_name": "Office hours: First 30 days",
        "public_title": "Office hours: your first 30 days with Harborlight",
        "summary": (
            "A short, practical session for new admins: the setup steps that matter, the defaults worth "
            "changing, and open Q&A for the rest of the time."
        ),
        "attention": "The first month decides whether a new tool becomes a habit or a line item.",
        "interest": (
            "Amara covers the three setup steps that matter, the defaults worth changing on day one, and the "
            "questions customer education hears most. Half of the 30 minutes is open Q&A."
        ),
        "decision": (
            "You'll leave with a 30-day checklist and a clear list of settings that can wait. Bring your "
            "account questions and get them answered live."
        ),
        "action": "Book a spot and add your question when you register.",
        "learning_outcomes": [
            "Finish the three setup steps that matter in week one",
            "Know which defaults to change and which to leave",
            "Follow a 30-day adoption checklist",
        ],
        "hosts": ["demo-amara-osei"],
        "topics": ["demo-customer-education"],
        "form": "demo-standard-v1",
        "register_label": "Book my spot",
        "confirm_label": "Confirm my spot",
        "sessions": [{"timezone": "Australia/Sydney", "days": 37, "hour": 11, "duration": 30}],
    },
    {
        "slug": "demo-reactivation-email-teardown",
        "internal_name": "Teardown: Reactivation emails",
        "public_title": "Teardown: five reactivation emails, rewritten live",
        "summary": (
            "Kei rewrites five sample reactivation emails live, from subject line to send time, explaining "
            "every edit. The demonstration recording page is open to everyone."
        ),
        "attention": "Most reactivation emails ask a lapsed customer to care before giving them a reason to.",
        "interest": (
            "Kei took five sample reactivation emails and rewrote each one live: subject line, first sentence, "
            "offer and send timing, with the reasoning behind every change."
        ),
        "decision": (
            "The session covers the full before-and-after for each email, plus the scoring rubric used to "
            "decide what to fix first."
        ),
        "action": "Open the demonstration recording page and score one of your own emails with the rubric.",
        "learning_outcomes": [
            "Spot the opening line that loses a lapsed reader",
            "Lead with value before the ask",
            "Choose send timing from engagement history",
        ],
        "hosts": ["demo-kei-nakamura-ross"],
        "topics": ["demo-lifecycle-marketing"],
        "form": "demo-standard-v1",
        "register_label": "Get the recording",
        "confirm_label": "Confirm",
        "sessions": [{"timezone": "Europe/Berlin", "days": -12, "hour": 16, "duration": 60}],
    },
    {
        "slug": "demo-event-ops-interview",
        "internal_name": "Interview: Webinar operations",
        "public_title": "In conversation: running 50 webinars a year with a team of three",
        "summary": (
            "Amara interviews Selin about the run-of-show template, the hand-offs that used to break and what "
            "got automated first. The demonstration recording page is open to everyone."
        ),
        "attention": "A webinar programme rarely fails on content. It fails on the week-before checklist.",
        "interest": (
            "Selin runs a fifty-session programme with two colleagues. In conversation with Amara, Selin walks "
            "through the run-of-show template, the hand-offs that used to break, and what got automated first."
        ),
        "decision": (
            "Practical and unpolished: the actual checklist, the timings that worked, and the two rituals the "
            "team dropped without anyone noticing."
        ),
        "action": "Open the demonstration recording page and borrow the run-of-show template.",
        "learning_outcomes": [
            "Use a run-of-show template that survives speaker changes",
            "Fix the three hand-offs that break most often",
            "Decide what to automate first in a small team",
        ],
        "hosts": ["demo-selin-marchetti", "demo-amara-osei"],
        "topics": ["demo-demand-generation", "demo-customer-education"],
        "form": "demo-standard-v1",
        "register_label": "Get the recording",
        "confirm_label": "Confirm",
        "sessions": [{"timezone": "America/Chicago", "days": -40, "hour": 12, "duration": 45}],
    },
]


def art_slugs():
    """Slugs the demo frontend must serve as ``ART_BASE + <slug>.svg``."""
    return [e["slug"] for e in EVENTS] + [h["slug"] for h in HOSTS]


def art_url(slug):
    return f"{ART_BASE}{slug}.svg"


def recording_page_url(slug):
    return f"{RECORDING_PAGE_BASE}{slug}"


def session_uid(slug, index):
    return f"{slug}-{index}@{ICS_DOMAIN}"


def is_owned(obj):
    return AuditLog.objects.filter(action=OWNED, object_type=type(obj).__name__, object_id=str(obj.pk)).exists()


def _claim(obj, label):
    audit(OWNED, obj=obj, slug=label)


def find_conflicts():
    """Existing rows in the demo namespace that the seed did not create."""
    conflicts = []
    checks = (
        (Topic, "slug", [slug for slug, _ in TOPICS]),
        (Host, "slug", [h["slug"] for h in HOSTS]),
        (FormVersion, "version", [f["version"] for f in FORMS]),
        (Event, "slug", [e["slug"] for e in EVENTS]),
        (
            Occurrence,
            "ics_uid",
            [session_uid(e["slug"], i) for e in EVENTS for i in range(1, len(e["sessions"]) + 1)],
        ),
    )
    for model, field, values in checks:
        for obj in model.objects.filter(**{f"{field}__in": values}):
            if not is_owned(obj):
                conflicts.append(f"{model.__name__} {getattr(obj, field)!r}")
    return conflicts


class _DryRun(Exception):
    pass


class Command(BaseCommand):
    help = "Seed six fictional, clearly labelled public demo events (idempotent; never touches non-demo data)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would change, then roll back.")
        parser.add_argument(
            "--allow-registration",
            action="store_true",
            help="Open registration on newly created upcoming sessions. Visitors' submissions are then real "
            "personal data and are delivered to any enabled destination. Existing sessions are not changed.",
        )

    def handle(self, *args, **options):
        conflicts = find_conflicts()
        if conflicts:
            raise CommandError(
                "Refusing to seed: these demo identifiers already belong to data this command did not create: "
                + ", ".join(conflicts)
                + ". Nothing was changed."
            )
        self.log = []
        try:
            with transaction.atomic():
                self._seed(timezone.now(), allow_registration=options["allow_registration"])
                if options["dry_run"]:
                    raise _DryRun
        except _DryRun:
            pass
        except ValidationError as exc:
            raise CommandError("Demo seed failed validation; nothing was changed: " + "; ".join(exc.messages))

        prefix = "[dry run] would have " if options["dry_run"] else ""
        for action, kind, label in self.log:
            self.stdout.write(f"{prefix}{action} {kind} {label}")
        self.stdout.write("Demo art (frontend must serve each): " + ", ".join(art_url(s) for s in art_slugs()))
        enabled = Destination.objects.filter(enabled=True).count()
        if enabled:
            self.stderr.write(
                f"Warning: {enabled} enabled destination(s) exist. The seed sends nothing, but registrations on "
                "open demo sessions would be delivered to them."
            )
        created = sum(1 for action, _, _ in self.log if action == "created")
        updated = sum(1 for action, _, _ in self.log if action == "updated")
        done = "Dry run finished" if options["dry_run"] else "Public demo seeded"
        self.stdout.write(self.style.SUCCESS(f"{done}: {len(EVENTS)} events, {created} created, {updated} updated."))

    # ------------------------------------------------------------------

    def _seed(self, now, *, allow_registration):
        topics = {slug: self._topic(slug, name) for slug, name in TOPICS}
        hosts = {spec["slug"]: self._host(spec) for spec in HOSTS}
        forms = {spec["version"]: self._form(spec) for spec in FORMS}
        for spec in EVENTS:
            event = self._event(spec, hosts, topics, forms)
            self._sessions(event, spec, now, allow_registration)

    def _topic(self, slug, name):
        topic = Topic.objects.filter(slug=slug).first()
        if topic is None:
            topic = Topic.objects.create(slug=slug, name=name)
            _claim(topic, slug)
            audit("topic.saved", obj=topic, created=True)
            self.log.append(("created", "topic", slug))
        elif topic.name != name:
            topic.name = name
            topic.save(update_fields=["name", "updated_at"])
            audit("topic.saved", obj=topic, created=False)
            self.log.append(("updated", "topic", slug))
        else:
            self.log.append(("kept", "topic", slug))
        return topic

    def _host(self, spec):
        data = {**spec, "image_url": art_url(spec["slug"])}
        host = Host.objects.filter(slug=spec["slug"]).first()
        if host is not None and all(getattr(host, k) == v for k, v in data.items()):
            self.log.append(("kept", "host", spec["slug"]))
            return host
        form = HostForm(data=data, instance=host)
        if not form.is_valid():
            raise ValidationError(f"Host {spec['slug']}: {form.errors.as_text()}")
        created = host is None
        host = form.save()
        if created:
            _claim(host, spec["slug"])
        audit("host.saved", obj=host, created=created)
        self.log.append(("created" if created else "updated", "host", spec["slug"]))
        return host

    def _form(self, spec):
        # Form versions are a registration contract: created once, never edited by the seed.
        fv = FormVersion.objects.filter(version=spec["version"]).first()
        if fv is not None:
            self.log.append(("kept", "form version", spec["version"]))
            return fv
        fv, _ = services.save_form_version(spec["rows"], spec["meta"], new_version=spec["version"])
        _claim(fv, spec["version"])
        self.log.append(("created", "form version", spec["version"]))
        return fv

    def _event(self, spec, hosts, topics, forms):
        data = {
            "internal_name": INTERNAL_PREFIX + spec["internal_name"],
            "slug": spec["slug"],
            "public_title": TITLE_PREFIX + spec["public_title"],
            "summary": f"{FICTIONAL_NOTE} {spec['summary']}",
            "hero_image_url": art_url(spec["slug"]),
            "attention": spec["attention"],
            "interest": spec["interest"],
            "decision": spec["decision"],
            "action": spec["action"],
            "learning_outcomes": list(spec["learning_outcomes"]),
            "hosts": [hosts[slug].pk for slug in spec["hosts"]],
            "topics": [topics[slug].pk for slug in spec["topics"]],
            "visibility": Event.Visibility.PUBLIC,
            "form_version": forms[spec["form"]].pk,
            "thank_you_mode": Event.ThankYouMode.MESSAGE,
            "thank_you_url": "",
            "thank_you_message": THANK_YOU,
            "register_label": spec["register_label"],
            "confirm_label": spec["confirm_label"],
        }
        event = Event.objects.filter(slug=spec["slug"]).first()
        try:
            if event is None:
                event, _ = services.save_event(data)
                _claim(event, spec["slug"])
                self.log.append(("created", "event", spec["slug"]))
                return event
            if event.cover_image_id:
                data["hero_image_url"] = ""  # an uploaded cover replaces the URL; keep it
            event, changed = services.save_event(data, instance=event)
        except services.FormErrors as exc:
            raise ValidationError(f"Event {spec['slug']}: {exc.form.errors.as_text()}")
        self.log.append(("updated" if changed else "kept", "event", spec["slug"]))
        return event

    def _sessions(self, event, spec, now, allow_registration):
        for index, session in enumerate(spec["sessions"], 1):
            uid = session_uid(spec["slug"], index)
            if Occurrence.objects.filter(ics_uid=uid).exists():
                # Never moved once created: demo dates stay stable across re-runs.
                self.log.append(("kept", "session", uid))
                continue
            tz = session["timezone"]
            day = (now.astimezone(ZoneInfo(tz)) + timedelta(days=session["days"])).date()
            local_naive = datetime(day.year, day.month, day.day, session["hour"], 0)
            starts_at = local_to_utc(local_naive, tz, None)
            ends_at = starts_at + timedelta(minutes=session["duration"])
            past = ends_at <= now
            occ = Occurrence.objects.create(
                event=event,
                timezone=tz,
                # Wall-clock value stored UTC-tagged, matching admin.persist_occurrence.
                local_start=local_naive.replace(tzinfo=dt_timezone.utc),
                duration_minutes=session["duration"],
                starts_at=starts_at,
                ends_at=ends_at,
                status=Occurrence.Status.COMPLETED if past else Occurrence.Status.SCHEDULED,
                registration_open=allow_registration and not past,
                # Demonstration page on the demo site, not a real recording. Set directly
                # (not scheduling.publish_recording) so nothing is ever enqueued.
                recording_url=recording_page_url(spec["slug"]) if past else "",
                recording_published_at=min(ends_at + timedelta(hours=20), now) if past else None,
                ics_uid=uid,
            )
            _claim(occ, uid)
            audit("occurrence.created", obj=occ, event_id=str(event.pk))
            self.log.append(("created", "session", uid))
