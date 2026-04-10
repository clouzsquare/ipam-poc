from sqlalchemy import Column, BigInteger, String, DateTime, Text, Integer, ForeignKey, Date, Boolean
from sqlalchemy.sql import func
from app.core.database import Base

class IpReclaimCandidate(Base):
    __tablename__ = "ip_reclaim_candidate"

    candidate_id = Column(BigInteger, primary_key=True, autoincrement=True)
    extraction_batch_id = Column(String(50), nullable=False)
    extraction_date = Column(Date, nullable=False)
    nw_id = Column(String(50), nullable=False)
    ip_address = Column(String(45), nullable=False)
    owner_team = Column(String(100), nullable=False)
    owner_email = Column(String(255), nullable=False)
    status = Column(String(20), nullable=False, default="READY")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class IpReclaimJob(Base):
    __tablename__ = "ip_reclaim_job"

    ip_reclaim_job_id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(String(100), nullable=False)
    requester_id = Column(String(100), nullable=False)
    main_task_id = Column(String(100))
    sub_task_id = Column(String(100))
    job_status = Column(String(20), nullable=False) # Ready, In-Progress, Released, Failed
    daily_limit = Column(Integer, nullable=False, default=20)
    team_limit = Column(Integer, nullable=False, default=4)
    fail_reason = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class IpReclaimJobItem(Base):
    __tablename__ = "ip_reclaim_job_item"

    ip_reclaim_job_item_id = Column(BigInteger, primary_key=True, autoincrement=True)
    ip_reclaim_job_id = Column(BigInteger, ForeignKey("ip_reclaim_job.ip_reclaim_job_id"), nullable=False)
    candidate_id = Column(BigInteger, nullable=False)
    nw_id = Column(String(50), nullable=False)
    device_id = Column(String(50))
    ip_address = Column(String(45), nullable=False)
    owner_team = Column(String(100), nullable=False)
    owner_email = Column(String(255), nullable=False)
    item_status = Column(String(50), nullable=False)
    dhcp_result = Column(String(20))
    device_result = Column(String(20))
    ntoss_result_message = Column(Text)
    exception_requested = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class ChatSession(Base):
    __tablename__ = "chat_session"

    session_id = Column(String(100), primary_key=True)
    requester_id = Column(String(100), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class ChatMessage(Base):
    __tablename__ = "chat_message"

    message_id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(String(100), ForeignKey("chat_session.session_id"), nullable=False)
    role = Column(String(20), nullable=False) # user / assistant
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

class AppConfig(Base):
    __tablename__ = "app_config"

    config_key = Column(String(100), primary_key=True)
    config_value = Column(String(255), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class IpReclaimPreview(Base):
    """사용자에게 확정 전 보여줄 임시 추출 목록"""
    __tablename__ = "ip_reclaim_preview"

    preview_id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(String(100), nullable=False)
    candidate_id = Column(BigInteger, nullable=False)
    nw_id = Column(String(50), nullable=False)
    ip_address = Column(String(45), nullable=False)
    owner_team = Column(String(100), nullable=False)
    owner_email = Column(String(255), nullable=False)
    created_at = Column(DateTime, server_default=func.now())