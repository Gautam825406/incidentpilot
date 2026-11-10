"""Seed demo users + knowledge base (runbooks/postmortems). Idempotent.

Phase 7: the demo users/incidents/documents belong to the Demo org
(slug "demo"). Seeding creates the org first, assigns every demo user
(primary org + membership) and every seeded document to it, and backfills
any pre–Phase 7 rows that still have org_id NULL to the demo org.
Demo login credentials are unchanged.
"""
from sqlalchemy.orm import Session

from .llm import hash_embedding
from .models import Document, Incident, EvalRun, IncidentEvent, Organization, OrganizationMembership, User
from .security import hash_password
from .synthetic import POSTMORTEMS, RUNBOOKS, SCENARIOS

# Demo incidents seeded into the Demo org on startup. Render's free tier
# runs SQLite on an ephemeral disk, so every redeploy wipes the database
# and the demo would otherwise start with zero incidents to open — a
# stale list in an already-open browser then 404s on "Open". Seeding here
# (only when the demo org has no incidents at all) keeps the demo usable
# after every restart. Seeded incidents have no hypotheses yet: opening
# one shows "Re-run investigation", which runs the real pipeline.
DEMO_INCIDENTS = ["checkout-oom", "payments-pool"]

DEMO_USERS = [
    ("Admin User", "admin@demo.local", "admin123", "admin"),
    ("Responder", "responder@demo.local", "responder123", "responder"),
    ("Viewer", "viewer@demo.local", "viewer123", "viewer"),
]

DEMO_ORG_SLUG = "demo"
DEMO_ORG_NAME = "Demo"


def get_or_create_demo_org(db: Session) -> Organization:
    """Return the Demo org, creating it (with a fresh webhook token) if needed."""
    org = db.query(Organization).filter_by(slug=DEMO_ORG_SLUG).first()
    if org is None:
        org = Organization(name=DEMO_ORG_NAME, slug=DEMO_ORG_SLUG, plan="free")
        db.add(org)
        db.commit()
        db.refresh(org)
    return org


def seed(db: Session):
    demo_org = get_or_create_demo_org(db)
    for name, email, password, role in DEMO_USERS:
        user = db.query(User).filter_by(email=email).first()
        if not user:
            user = User(name=name, email=email, password_hash=hash_password(password),
                        role=role, org_id=demo_org.id)
            db.add(user)
            db.commit()
            db.refresh(user)
        # Backfill: pre–Phase 7 demo users had no org.
        if not user.org_id:
            user.org_id = demo_org.id
        if not db.query(OrganizationMembership).filter_by(org_id=demo_org.id, user_id=user.id).first():
            db.add(OrganizationMembership(org_id=demo_org.id, user_id=user.id, role=role))
    for doc in RUNBOOKS + POSTMORTEMS:
        existing = db.query(Document).filter_by(title=doc["title"]).first()
        if not existing:
            db.add(Document(title=doc["title"], kind=doc["kind"], service=doc["service"],
                            content=doc["content"], embedding=hash_embedding(doc["content"]),
                            org_id=demo_org.id))
        elif not existing.org_id:
            existing.org_id = demo_org.id
    # Backfill any pre–Phase 7 rows that still have org_id NULL: they were
    # created before multi-tenancy and belong to the demo org.
    db.query(Incident).filter(Incident.org_id.is_(None)).update({Incident.org_id: demo_org.id})
    db.query(Document).filter(Document.org_id.is_(None)).update({Document.org_id: demo_org.id})
    db.query(EvalRun).filter(EvalRun.org_id.is_(None)).update({EvalRun.org_id: demo_org.id})
    # Demo incidents: only when the demo org has none (fresh DB after a
    # redeploy, or first boot). Never re-seed over real user incidents.
    if db.query(Incident).filter_by(org_id=demo_org.id).count() == 0:
        for key in DEMO_INCIDENTS:
            scenario = SCENARIOS[key]
            incident = Incident(
                title=scenario["title"], service=scenario["service"],
                severity=scenario["severity"], status="open",
                source="simulator", external_id=f"seed:{key}",
                scenario=key, org_id=demo_org.id,
            )
            db.add(incident)
            db.commit()
            db.refresh(incident)
            db.add(IncidentEvent(incident_id=incident.id, type="alert",
                                  actor="simulator",
                                  payload={"message": scenario["alert"]}))
        db.commit()
