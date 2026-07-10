import os
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

_key = os.getenv("ENCRYPTION_KEY")
if not _key:
    raise RuntimeError(
        "ENCRYPTION_KEY is not set. Generate one with:\n"
        "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
        "and put it in backend/.env"
    )

_fernet = Fernet(_key.encode())


def encrypt(value: str) -> str:
    if value is None:
        return None
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if value is None:
        return None
    return _fernet.decrypt(value.encode()).decode()
