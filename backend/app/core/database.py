# app/core/database.py
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

load_dotenv()

# .env에서 DATABASE_URL을 가져오거나 직접 구성
# 예: mysql+pymysql://root:password@localhost:3306/ipam_db
# For PoC, fallback to SQLite if MySQL connection fails or is not preferred for local testing
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL")
if not SQLALCHEMY_DATABASE_URL or "mysql" not in SQLALCHEMY_DATABASE_URL:
    # Use SQLite for PoC if MySQL is not available or if it's the default in .env but failing
    SQLALCHEMY_DATABASE_URL = "sqlite:///./ipam_poc.db"

# 엔진 생성
if "sqlite" in SQLALCHEMY_DATABASE_URL:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}, echo=True
    )
else:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=3600
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# DB 세션 의존성 주입용 함수
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()