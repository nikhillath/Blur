import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.session import SessionLocal
from app.models import Document, PiiEntity, Scan, User
from app.security.passwords import hash_password


def _user(db, email="a@example.com", role="USER"):
    u = User(name="A", email=email, password_hash=hash_password("x" * 12), role=role)
    db.add(u)
    db.flush()
    return u


def test_email_unique():
    with SessionLocal() as db:
        _user(db)
        db.commit()
        with pytest.raises(IntegrityError):
            _user(db)
            db.commit()


def test_role_check_constraint():
    with SessionLocal() as db, pytest.raises(IntegrityError):
        _user(db, role="ROOT")


def _entity(scan_id, **kw):
    base = dict(
        scan_id=scan_id, entity_type="EMAIL", original_entity_type="EMAIL", detected_value="x@y.z",
        masked_value="x***", confidence_score=0.9, start_position=0, end_position=5,
        detection_method="rule", model_version="v0",
    )
    return PiiEntity(**{**base, **kw})


@pytest.mark.parametrize(
    "bad", [{"confidence_score": 1.5}, {"start_position": 5, "end_position": 5}, {"review_status": "MAYBE"}]
)
def test_entity_check_constraints(bad):
    with SessionLocal() as db:
        u = _user(db)
        d = Document(uploaded_by=u.id, original_filename="a.txt", storage_key=uuid.uuid4().hex,
                     file_type="txt", mime_type="text/plain", file_size=1)
        db.add(d)
        db.flush()
        s = Scan(document_id=d.id, model_version="v0")
        db.add(s)
        db.flush()
        db.add(_entity(s.id, **bad))
        with pytest.raises(IntegrityError):
            db.flush()


def test_cascade_delete_document_removes_scans_and_entities():
    with SessionLocal() as db:
        u = _user(db)
        d = Document(uploaded_by=u.id, original_filename="a.txt", storage_key=uuid.uuid4().hex,
                     file_type="txt", mime_type="text/plain", file_size=1)
        s = Scan(model_version="v0")
        d.scans.append(s)
        s.entities.append(_entity(None))
        db.add(d)
        db.commit()
        db.delete(d)
        db.commit()
        assert db.query(Scan).count() == 0 and db.query(PiiEntity).count() == 0


def test_user_with_documents_cannot_be_deleted():
    with SessionLocal() as db:
        u = _user(db)
        db.add(Document(uploaded_by=u.id, original_filename="a.txt", storage_key=uuid.uuid4().hex,
                        file_type="txt", mime_type="text/plain", file_size=1))
        db.commit()
        db.delete(u)
        with pytest.raises(IntegrityError):
            db.commit()
