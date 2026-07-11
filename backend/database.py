import os
import glob
import shutil
import datetime
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./copytrader.db")

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def backup_sqlite_db(keep: int = 30) -> None:
    """Copies the sqlite DB file into backend/backups/ with a timestamped name, called on
    every startup. No-op for non-sqlite DATABASE_URLs. Prunes older backups beyond `keep` so
    the folder doesn't grow unbounded."""
    if not DATABASE_URL.startswith("sqlite:///"):
        return
    db_path = DATABASE_URL[len("sqlite:///"):]
    if not os.path.exists(db_path):
        return

    backup_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
    os.makedirs(backup_dir, exist_ok=True)

    db_name = os.path.basename(db_path)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(backup_dir, f"{db_name}.{stamp}.bak")
    shutil.copy2(db_path, dest)

    existing = sorted(glob.glob(os.path.join(backup_dir, f"{db_name}.*.bak")))
    for old in existing[:-keep]:
        os.remove(old)
