import os
import time
from datetime import datetime, timezone
from typing import List, Optional, Literal
import asyncio
import logging
from contextlib import asynccontextmanager
import socket
import httpx

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl, validator, ConfigDict
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Constants from env
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
DELAY = int(os.getenv("DELAY", "300"))  # 5 minutes
SSH_TIMEOUT = int(os.getenv("SSH_TIMEOUT", "10"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))

# Database setup (location: /app/data/monitoring.db)
if not os.path.exists("/app/data"):
    os.makedirs("/app/data")
SQLALCHEMY_DATABASE_URL = "sqlite:///./data/monitoring.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Database models
class Service(Base):
    __tablename__ = "services"

    id = Column(Integer, primary_key=True, index=True)
    protocol = Column(String, index=True)
    host = Column(String)
    port = Column(Integer)
    alias = Column(String)
    username = Column(String, nullable=True)
    ignore_http_errors = Column(Boolean, default=False)
    path = Column(String, nullable=True)

class ServiceStatus(Base):
    __tablename__ = "service_status"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, index=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    status = Column(Boolean)
    response_time = Column(Float, nullable=True)
    error_message = Column(String, nullable=True)
    http_status = Column(Integer, nullable=True)

# Pydantic models
class BaseModelWithConfig(BaseModel):
    model_config = ConfigDict(
        json_encoders={
            # Ensure datetime is serialized as ISO format with timezone info
            datetime: lambda dt: dt.replace(tzinfo=timezone.utc).isoformat()
        }
    )

class ServiceBase(BaseModelWithConfig):
    protocol: Literal["SSH", "HTTP"]
    host: str
    port: int
    alias: str
    username: Optional[str] = None
    ignore_http_errors: Optional[bool] = False
    path: Optional[str] = None

    @validator('port')
    def validate_port(cls, v, values):
        if 'protocol' in values:
            if values['protocol'] == 'SSH' and v == 0:
                return 22
        return v

    @validator('path')
    def validate_path(cls, v, values):
        print(f"Validating path: {v}, protocol: {values.get('protocol')}")
        if 'protocol' in values and values['protocol'] == 'HTTP':
            # For HTTP, ensure path always has a value
            if v is None:
                print(f"Path is None, returning /")
                return '/'
            # If path exists, normalize it
            if v:
                print(f"Path exists: {v}")
                # Remove any leading/trailing whitespace
                v = v.strip()
                # Ensure path starts with a slash
                if not v.startswith('/'):
                    v = f'/{v}'
                print(f"Normalized path: {v}")
                return v
            print(f"Path is empty string, returning /")
            return '/'  # Empty string case
        print(f"Returning unchanged path: {v}")
        return v

class ServiceCreate(ServiceBase):
    pass

class ServiceRead(ServiceBase):
    id: int

    model_config = ConfigDict(from_attributes=True)  # replaces orm_mode=True in Pydantic v2

class StatusBase(BaseModelWithConfig):
    service_id: int
    timestamp: datetime
    status: bool
    response_time: Optional[float] = None
    error_message: Optional[str] = None
    http_status: Optional[int] = None

class StatusCreate(StatusBase):
    pass

class StatusRead(StatusBase):
    id: int

    model_config = ConfigDict(from_attributes=True)  # replaces orm_mode=True in Pydantic v2

# Background monitoring task
async def monitor_services():
    while True:
        try:
            db = SessionLocal()
            services = db.query(Service).all()
            for service in services:
                try:
                    if service.protocol == "SSH":
                        await check_ssh(service, db)
                    elif service.protocol == "HTTP":
                        await check_http(service, db)
                except Exception as e:
                    logging.error(f"Error monitoring service {service.id}: {str(e)}")
                await asyncio.sleep(1)  # Small delay between checks
            db.close()
        except Exception as e:
            logging.error(f"Error in monitoring loop: {str(e)}")
        await asyncio.sleep(DELAY)

async def check_ssh(service, db):
    start_time = time.time()
    status = False
    error_msg = None
    
    try:
        # Use a simple socket connection to check if the SSH port is open and responding        
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(SSH_TIMEOUT)
        
        # Connect to the host on the SSH port
        sock.connect((service.host, service.port))
        
        # Receive the initial banner to confirm it's an SSH server
        banner = sock.recv(1024).decode('utf-8', errors='ignore')
        
        # Check if the banner contains SSH identification, which indicates it's an SSH server
        # Different servers might have slightly different banner formats
        if 'SSH' in banner:
            status = True
        else:
            error_msg = "Port is open but doesn't appear to be an SSH server"
        
        sock.close()
    except Exception as e:
        error_msg = str(e)
    
    response_time = time.time() - start_time
    
    service_status = ServiceStatus(
        service_id=service.id,
        status=status,
        response_time=response_time,
        error_message=error_msg
    )
    db.add(service_status)
    db.commit()

async def check_http(service, db):
    start_time = time.time()
    status = False
    error_msg = None
    http_status = None
    
    # Build the URL with proper protocol based on port
    url = f"http{'s' if service.port == 443 else ''}://{service.host}"
    if service.port not in (80, 443):
        url += f":{service.port}"
    
    # Add the path if specified
    if service.path:
        # Ensure path doesn't have double slashes
        path = service.path
        if not path.startswith('/'):
            path = f'/{path}'
        url += path
    
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(url)
            http_status = response.status_code
            if 200 <= response.status_code < 300 or (service.ignore_http_errors and 300 <= response.status_code < 600):
                status = True
            else:
                error_msg = f"HTTP status code: {response.status_code}"
    except Exception as e:
        error_msg = str(e)
    
    response_time = time.time() - start_time
    
    service_status = ServiceStatus(
        service_id=service.id,
        status=status,
        response_time=response_time,
        error_message=error_msg,
        http_status=http_status
    )
    db.add(service_status)
    db.commit()

# Dependency to get DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Create tables if they don't exist
Base.metadata.create_all(bind=engine)

# FastAPI app with background task
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the background monitoring task
    task = asyncio.create_task(monitor_services())
    yield
    # Cancel the task when shutting down
    task.cancel()

app = FastAPI(lifespan=lifespan)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Routes
@app.post("/api/services/", response_model=ServiceRead)
async def create_service(service: ServiceCreate, db: Session = Depends(get_db)):
    # Log incoming data for debugging
    print(f"Received service data: {service}")
    
    service_data = service.dict()
    print(f"Service data after dict(): {service_data}")
    
    # For HTTP services, ensure path is set
    if service.protocol == "HTTP" and not service_data.get("path"):
        service_data["path"] = "/"
        print(f"Path was not set, defaulting to /")
    
    print(f"Final service data: {service_data}")
    db_service = Service(**service_data)
    db.add(db_service)
    db.commit()
    db.refresh(db_service)
    return db_service

@app.get("/api/services/", response_model=List[ServiceRead])
async def read_services(db: Session = Depends(get_db)):
    services = db.query(Service).all()
    return services

@app.get("/api/services/{service_id}", response_model=ServiceRead)
async def read_service(service_id: int, db: Session = Depends(get_db)):
    service = db.query(Service).filter(Service.id == service_id).first()
    if service is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return service

@app.delete("/api/services/{service_id}")
async def delete_service(service_id: int, db: Session = Depends(get_db)):
    service = db.query(Service).filter(Service.id == service_id).first()
    if service is None:
        raise HTTPException(status_code=404, detail="Service not found")
    db.delete(service)
    db.commit()
    return {"detail": "Service deleted"}

@app.get("/api/services/{service_id}/status", response_model=List[StatusRead])
async def read_service_status(service_id: int, limit: int = 100, db: Session = Depends(get_db)):
    statuses = db.query(ServiceStatus).filter(
        ServiceStatus.service_id == service_id
    ).order_by(ServiceStatus.timestamp.desc()).limit(limit).all()
    return statuses

# Serve static files for the frontend
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=True) 