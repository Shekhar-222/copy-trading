"""
Covers main.update_account (PATCH /accounts/{id}) - editing an existing account's
label/credentials in place instead of requiring delete + re-add. Calls the endpoint function
directly against an isolated in-memory db, same pattern as test_order_mirroring.py; this does
NOT touch the real copytrader.db (main.py's module-level engine is only used by its own
get_db() dependency, never by the plain function call below).
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import crypto_utils
import main
import models


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def make_account(db, broker="zerodha", **overrides):
    defaults = dict(
        label="acc", role="child", broker=broker, client_id="acc",
        api_key_enc=crypto_utils.encrypt("api-key"),
        api_secret_enc=crypto_utils.encrypt("secret"),
        totp_secret_enc=crypto_utils.encrypt("totp"),
        access_token_enc=crypto_utils.encrypt("token"),
        token_generated_at=__import__("datetime").datetime.utcnow(),
    )
    defaults.update(overrides)
    acc = models.Account(**defaults)
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return acc


def test_label_and_client_id_can_be_edited_without_touching_credentials(db_session):
    acc = make_account(db_session, label="old label", client_id="old-id")

    main.update_account(acc.id, main.AccountUpdate(label="new label", client_id="new-id"), db_session)

    db_session.refresh(acc)
    assert acc.label == "new label"
    assert acc.client_id == "new-id"
    assert acc.access_token_enc is not None  # untouched - no credential field was sent


def test_editing_a_credential_invalidates_the_stored_token(db_session):
    acc = make_account(db_session)
    assert acc.access_token_enc is not None

    main.update_account(acc.id, main.AccountUpdate(api_key="new-api-key"), db_session)

    db_session.refresh(acc)
    assert crypto_utils.decrypt(acc.api_key_enc) == "new-api-key"
    assert acc.access_token_enc is None
    assert acc.token_generated_at is None


def test_groww_rejects_both_totp_secret_and_api_secret_together(db_session):
    acc = make_account(db_session, broker="groww")

    with pytest.raises(HTTPException) as exc_info:
        main.update_account(acc.id, main.AccountUpdate(totp_secret="ABCDEFGHIJKLMNOP", api_secret="a-secret"), db_session)
    assert exc_info.value.status_code == 400


def test_groww_setting_api_secret_clears_any_stored_totp_secret(db_session):
    acc = make_account(db_session, broker="groww")
    assert crypto_utils.decrypt(acc.totp_secret_enc) == "totp"

    main.update_account(acc.id, main.AccountUpdate(api_secret="a-secret"), db_session)

    db_session.refresh(acc)
    assert crypto_utils.decrypt(acc.api_secret_enc) == "a-secret"
    assert crypto_utils.decrypt(acc.totp_secret_enc) == ""


def test_groww_setting_totp_secret_clears_any_stored_api_secret(db_session):
    acc = make_account(db_session, broker="groww")
    assert crypto_utils.decrypt(acc.api_secret_enc) == "secret"

    main.update_account(acc.id, main.AccountUpdate(totp_secret="ABCDEFGHIJKLMNOP"), db_session)

    db_session.refresh(acc)
    assert crypto_utils.decrypt(acc.totp_secret_enc) == "ABCDEFGHIJKLMNOP"
    assert crypto_utils.decrypt(acc.api_secret_enc) == ""


def test_editing_unknown_account_raises_404(db_session):
    with pytest.raises(HTTPException) as exc_info:
        main.update_account(999, main.AccountUpdate(label="x"), db_session)
    assert exc_info.value.status_code == 404
