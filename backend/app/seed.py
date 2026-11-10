"""Seed demo users + knowledge base (runbooks/postmortems). Idempotent."""
from sqlalchemy.orm import Session

from .llm import hash_embedding
from .models import Document, User
from .security import hash_password
from .synthetic import POSTMORTEMS, RUNBOOKS

DEMO_USERS = [
    ("Admin User", "admin@demo.local", "admin123", "admin"),
    ("Responder", "responder@demo.local", "responder123", "responder"),
    ("Viewer", "viewer@demo.local", "viewer123", "viewer"),
]


def seed(db: Session):
    for name, email, password, role in DEMO_USERS:
        if not db.query(User).filter_by(email=email).first():
            db.add(User(name=name, email=email, password_hash=hash_password(password), role=role))
    for doc in RUNBOOKS + POSTMORTEMS:
        if not db.query(Document).filter_by(title=doc["title"]).first():
            db.add(Document(title=doc["title"], kind=doc["kind"], service=doc["service"],
                            content=doc["content"], embedding=hash_embedding(doc["content"])))
    db.commit()
