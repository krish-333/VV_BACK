"""
Vivah Verse — Backend API (single-file version)
FastAPI + PostgreSQL backend powering the company dashboard, customer site, and vendor portal.

Run locally:
    uvicorn main:app --reload

Deploy on Railway:
    Add a Postgres plugin (auto-injects DATABASE_URL), set SECRET_KEY / RAZORPAY keys /
    ALLOWED_ORIGINS as environment variables, and Railway runs this via the Procfile.
"""

import enum
import uuid
import hmac
import hashlib
from datetime import datetime, date, timedelta, timezone
from typing import Optional, List

import bcrypt
import razorpay
from jose import JWTError, jwt

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

from pydantic import BaseModel, EmailStr, Field
from pydantic_settings import BaseSettings

from sqlalchemy import (
    create_engine, Column, String, Integer, Float, Boolean, DateTime, Date,
    ForeignKey, Enum, Text, UniqueConstraint
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Session


# =========================================================================
# CONFIG
# =========================================================================

class Settings(BaseSettings):
    database_url: str = "postgresql://postgres:PMJjBZSQMxtNLQRuOKWKadwaWeMmErQV@trolley.proxy.rlwy.net:41197/railway"
    secret_key: str = "vvback-production.up.railway.app"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 days
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    allowed_origins: str = "http://localhost:3000,http://127.0.0.1:5500"
    environment: str = "development"

    class Config:
        env_file = ".env"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",")]


settings = Settings()


# =========================================================================
# DATABASE
# =========================================================================

db_url = settings.database_url
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(db_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def gen_uuid():
    return str(uuid.uuid4())


# =========================================================================
# ENUMS
# =========================================================================

class UserRole(str, enum.Enum):
    ADMIN = "admin"        # Vivah Verse company/ops team
    CUSTOMER = "customer"  # couples booking services
    VENDOR = "vendor"      # service providers


class BookingStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class AvailabilityStatus(str, enum.Enum):
    AVAILABLE = "available"
    BLOCKED = "blocked"
    BOOKED = "booked"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


# =========================================================================
# MODELS (SQLAlchemy)
# =========================================================================

class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=gen_uuid)
    full_name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    phone = Column(String, unique=True, nullable=True)
    hashed_password = Column(String, nullable=False)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.CUSTOMER)
    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    vendor_profile = relationship("VendorProfile", back_populates="user", uselist=False)
    bookings = relationship("Booking", back_populates="customer", foreign_keys="Booking.customer_id")


class ServiceCategory(Base):
    __tablename__ = "service_categories"

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, unique=True, nullable=False)
    icon = Column(String, nullable=True)
    description = Column(Text, nullable=True)

    services = relationship("Service", back_populates="category")


class VendorProfile(Base):
    __tablename__ = "vendor_profiles"

    id = Column(String, primary_key=True, default=gen_uuid)
    user_id = Column(String, ForeignKey("users.id"), unique=True, nullable=False)
    business_name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    city = Column(String, nullable=True)
    state = Column(String, nullable=True)
    address = Column(Text, nullable=True)
    cover_image_url = Column(String, nullable=True)
    logo_url = Column(String, nullable=True)
    is_verified = Column(Boolean, default=False)
    is_instant_book = Column(Boolean, default=False)
    rating_avg = Column(Float, default=0.0)
    rating_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="vendor_profile")
    services = relationship("Service", back_populates="vendor")
    availability = relationship("Availability", back_populates="vendor")
    bookings = relationship("Booking", back_populates="vendor")
    reviews = relationship("Review", back_populates="vendor")


class Service(Base):
    __tablename__ = "services"

    id = Column(String, primary_key=True, default=gen_uuid)
    vendor_id = Column(String, ForeignKey("vendor_profiles.id"), nullable=False)
    category_id = Column(String, ForeignKey("service_categories.id"), nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    price = Column(Float, nullable=False)
    price_unit = Column(String, default="per event")
    image_url = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    vendor = relationship("VendorProfile", back_populates="services")
    category = relationship("ServiceCategory", back_populates="services")


class Availability(Base):
    __tablename__ = "availability"
    __table_args__ = (UniqueConstraint("vendor_id", "date", name="uq_vendor_date"),)

    id = Column(String, primary_key=True, default=gen_uuid)
    vendor_id = Column(String, ForeignKey("vendor_profiles.id"), nullable=False)
    date = Column(Date, nullable=False)
    status = Column(Enum(AvailabilityStatus), default=AvailabilityStatus.AVAILABLE)
    note = Column(String, nullable=True)

    vendor = relationship("VendorProfile", back_populates="availability")


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(String, primary_key=True, default=gen_uuid)
    customer_id = Column(String, ForeignKey("users.id"), nullable=False)
    vendor_id = Column(String, ForeignKey("vendor_profiles.id"), nullable=False)
    service_id = Column(String, ForeignKey("services.id"), nullable=False)
    event_date = Column(Date, nullable=False)
    status = Column(Enum(BookingStatus), default=BookingStatus.PENDING)
    guest_count = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)
    total_amount = Column(Float, nullable=False)
    advance_amount = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("User", back_populates="bookings", foreign_keys=[customer_id])
    vendor = relationship("VendorProfile", back_populates="bookings")
    service = relationship("Service")
    payments = relationship("Payment", back_populates="booking")
    review = relationship("Review", back_populates="booking", uselist=False)


class Payment(Base):
    __tablename__ = "payments"

    id = Column(String, primary_key=True, default=gen_uuid)
    booking_id = Column(String, ForeignKey("bookings.id"), nullable=False)
    amount = Column(Float, nullable=False)
    provider = Column(String, default="razorpay")
    provider_order_id = Column(String, nullable=True)
    provider_payment_id = Column(String, nullable=True)
    status = Column(Enum(PaymentStatus), default=PaymentStatus.PENDING)
    created_at = Column(DateTime, default=datetime.utcnow)

    booking = relationship("Booking", back_populates="payments")


class Review(Base):
    __tablename__ = "reviews"

    id = Column(String, primary_key=True, default=gen_uuid)
    booking_id = Column(String, ForeignKey("bookings.id"), unique=True, nullable=False)
    customer_id = Column(String, ForeignKey("users.id"), nullable=False)
    vendor_id = Column(String, ForeignKey("vendor_profiles.id"), nullable=False)
    rating = Column(Integer, nullable=False)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    booking = relationship("Booking", back_populates="review")
    vendor = relationship("VendorProfile", back_populates="reviews")


# =========================================================================
# SCHEMAS (Pydantic)
# =========================================================================

# ---- Auth ----
class UserCreate(BaseModel):
    full_name: str
    email: EmailStr
    phone: Optional[str] = None
    password: str = Field(min_length=6)
    role: UserRole = UserRole.CUSTOMER


class UserOut(BaseModel):
    id: str
    full_name: str
    email: str
    phone: Optional[str]
    role: UserRole
    is_verified: bool

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# ---- Vendor ----
class VendorProfileCreate(BaseModel):
    business_name: str
    description: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    address: Optional[str] = None


class VendorProfileOut(BaseModel):
    id: str
    business_name: str
    description: Optional[str]
    city: Optional[str]
    state: Optional[str]
    cover_image_url: Optional[str]
    logo_url: Optional[str]
    is_verified: bool
    is_instant_book: bool
    rating_avg: float
    rating_count: int

    class Config:
        from_attributes = True


class VendorProfileUpdate(BaseModel):
    business_name: Optional[str] = None
    description: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    address: Optional[str] = None
    cover_image_url: Optional[str] = None
    logo_url: Optional[str] = None
    is_instant_book: Optional[bool] = None


# ---- Category ----
class ServiceCategoryOut(BaseModel):
    id: str
    name: str
    icon: Optional[str]
    description: Optional[str]

    class Config:
        from_attributes = True


# ---- Service ----
class ServiceCreate(BaseModel):
    category_id: str
    name: str
    description: Optional[str] = None
    price: float
    price_unit: str = "per event"
    image_url: Optional[str] = None


class ServiceOut(BaseModel):
    id: str
    vendor_id: str
    category_id: str
    name: str
    description: Optional[str]
    price: float
    price_unit: str
    image_url: Optional[str]
    is_active: bool

    class Config:
        from_attributes = True


# ---- Availability ----
class AvailabilitySet(BaseModel):
    date: date
    status: AvailabilityStatus
    note: Optional[str] = None


class AvailabilityBulkSet(BaseModel):
    entries: List[AvailabilitySet]


class AvailabilityOut(BaseModel):
    id: str
    date: date
    status: AvailabilityStatus
    note: Optional[str]

    class Config:
        from_attributes = True


# ---- Booking ----
class BookingCreate(BaseModel):
    vendor_id: str
    service_id: str
    event_date: date
    guest_count: Optional[int] = None
    notes: Optional[str] = None


class BookingStatusUpdate(BaseModel):
    status: BookingStatus


class BookingOut(BaseModel):
    id: str
    customer_id: str
    vendor_id: str
    service_id: str
    event_date: date
    status: BookingStatus
    guest_count: Optional[int]
    notes: Optional[str]
    total_amount: float
    advance_amount: Optional[float]
    created_at: datetime

    class Config:
        from_attributes = True


# ---- Payment ----
class PaymentCreateOrder(BaseModel):
    booking_id: str
    amount: float


class PaymentOut(BaseModel):
    id: str
    booking_id: str
    amount: float
    provider: str
    provider_order_id: Optional[str]
    status: PaymentStatus

    class Config:
        from_attributes = True


class PaymentVerify(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


# ---- Review ----
class ReviewCreate(BaseModel):
    booking_id: str
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None


class ReviewOut(BaseModel):
    id: str
    booking_id: str
    customer_id: str
    vendor_id: str
    rating: int
    comment: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


# =========================================================================
# SECURITY (password hashing + JWT)
# =========================================================================

def hash_password(password: str) -> str:
    pw_bytes = password.encode("utf-8")[:72]  # bcrypt's hard limit
    return bcrypt.hashpw(pw_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    pw_bytes = plain_password.encode("utf-8")[:72]
    return bcrypt.checkpw(pw_bytes, hashed_password.encode("utf-8"))


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        return None


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exception
    user_id = payload.get("sub")
    if user_id is None:
        raise credentials_exception
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise credentials_exception
    return user


def require_role(*roles: UserRole):
    def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires one of these roles: {[r.value for r in roles]}",
            )
        return current_user
    return role_checker


require_admin = require_role(UserRole.ADMIN)
require_vendor = require_role(UserRole.VENDOR)
require_customer = require_role(UserRole.CUSTOMER)
require_vendor_or_admin = require_role(UserRole.VENDOR, UserRole.ADMIN)


# =========================================================================
# APP SETUP
# =========================================================================

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Vivah Verse API",
    description="Backend powering Vivah Verse — the complete wedding marketplace connecting couples, vendors, and the Vivah Verse team.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["Health"])
def root():
    return {"status": "ok", "service": "Vivah Verse API", "version": "1.0.0"}


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "healthy"}


# =========================================================================
# AUTH ENDPOINTS
# =========================================================================

@app.post("/auth/register", response_model=Token, status_code=201, tags=["Auth"])
def register(payload: UserCreate, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        full_name=payload.full_name,
        email=payload.email,
        phone=payload.phone,
        hashed_password=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token({"sub": user.id, "role": user.role.value})
    return Token(access_token=token, user=UserOut.model_validate(user))


@app.post("/auth/login", response_model=Token, tags=["Auth"])
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    token = create_access_token({"sub": user.id, "role": user.role.value})
    return Token(access_token=token, user=UserOut.model_validate(user))


@app.get("/auth/me", response_model=UserOut, tags=["Auth"])
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


# =========================================================================
# VENDOR ENDPOINTS
# =========================================================================

@app.post("/vendors/profile", response_model=VendorProfileOut, status_code=201, tags=["Vendors"])
def create_vendor_profile(
    payload: VendorProfileCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_vendor),
):
    existing = db.query(VendorProfile).filter(VendorProfile.user_id == current_user.id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Vendor profile already exists")

    profile = VendorProfile(user_id=current_user.id, **payload.model_dump())
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@app.get("/vendors/profile/me", response_model=VendorProfileOut, tags=["Vendors"])
def get_my_vendor_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_vendor),
):
    profile = db.query(VendorProfile).filter(VendorProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Vendor profile not found")
    return profile


@app.patch("/vendors/profile/me", response_model=VendorProfileOut, tags=["Vendors"])
def update_my_vendor_profile(
    payload: VendorProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_vendor),
):
    profile = db.query(VendorProfile).filter(VendorProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Vendor profile not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)

    db.commit()
    db.refresh(profile)
    return profile


@app.get("/vendors", response_model=List[VendorProfileOut], tags=["Vendors"])
def list_vendors(
    city: Optional[str] = None,
    category_id: Optional[str] = None,
    verified_only: bool = False,
    db: Session = Depends(get_db),
):
    query = db.query(VendorProfile)
    if city:
        query = query.filter(VendorProfile.city.ilike(f"%{city}%"))
    if verified_only:
        query = query.filter(VendorProfile.is_verified == True)  # noqa: E712
    if category_id:
        query = query.join(Service).filter(Service.category_id == category_id).distinct()
    return query.all()


@app.get("/vendors/{vendor_id}", response_model=VendorProfileOut, tags=["Vendors"])
def get_vendor(vendor_id: str, db: Session = Depends(get_db)):
    vendor = db.query(VendorProfile).filter(VendorProfile.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


@app.patch("/vendors/{vendor_id}/verify", response_model=VendorProfileOut, tags=["Vendors"])
def verify_vendor(
    vendor_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    vendor = db.query(VendorProfile).filter(VendorProfile.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    vendor.is_verified = True
    db.commit()
    db.refresh(vendor)
    return vendor


# ---- Services (owned by vendor) ----
@app.post("/vendors/services", response_model=ServiceOut, status_code=201, tags=["Vendors"])
def create_service(
    payload: ServiceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_vendor),
):
    profile = db.query(VendorProfile).filter(VendorProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Create a vendor profile first")

    category = db.query(ServiceCategory).filter(ServiceCategory.id == payload.category_id).first()
    if not category:
        raise HTTPException(status_code=404, detail="Invalid category_id")

    service = Service(vendor_id=profile.id, **payload.model_dump())
    db.add(service)
    db.commit()
    db.refresh(service)
    return service


@app.get("/vendors/{vendor_id}/services", response_model=List[ServiceOut], tags=["Vendors"])
def list_vendor_services(vendor_id: str, db: Session = Depends(get_db)):
    return db.query(Service).filter(
        Service.vendor_id == vendor_id, Service.is_active == True  # noqa: E712
    ).all()


@app.delete("/vendors/services/{service_id}", status_code=204, tags=["Vendors"])
def deactivate_service(
    service_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_vendor),
):
    profile = db.query(VendorProfile).filter(VendorProfile.user_id == current_user.id).first()
    service = db.query(Service).filter(Service.id == service_id, Service.vendor_id == profile.id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    service.is_active = False
    db.commit()


# =========================================================================
# AVAILABILITY ENDPOINTS (vendor calendar)
# =========================================================================

def _get_own_vendor_profile(db: Session, user: User) -> VendorProfile:
    profile = db.query(VendorProfile).filter(VendorProfile.user_id == user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Vendor profile not found")
    return profile


@app.put("/availability/me", response_model=List[AvailabilityOut], tags=["Availability"])
def set_my_availability(
    payload: AvailabilityBulkSet,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_vendor),
):
    """Vendor sets/updates their availability for one or more dates (upsert)."""
    profile = _get_own_vendor_profile(db, current_user)
    results = []

    for entry in payload.entries:
        record = db.query(Availability).filter(
            Availability.vendor_id == profile.id,
            Availability.date == entry.date,
        ).first()

        if record:
            if record.status == AvailabilityStatus.BOOKED and entry.status != AvailabilityStatus.BOOKED:
                raise HTTPException(
                    status_code=400,
                    detail=f"{entry.date} has a confirmed booking and can't be changed directly. Cancel the booking first.",
                )
            record.status = entry.status
            record.note = entry.note
        else:
            record = Availability(
                vendor_id=profile.id,
                date=entry.date,
                status=entry.status,
                note=entry.note,
            )
            db.add(record)
        results.append(record)

    db.commit()
    for r in results:
        db.refresh(r)
    return results


@app.get("/availability/me", response_model=List[AvailabilityOut], tags=["Availability"])
def get_my_availability(
    start: Optional[date] = None,
    end: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_vendor),
):
    profile = _get_own_vendor_profile(db, current_user)
    query = db.query(Availability).filter(Availability.vendor_id == profile.id)
    if start:
        query = query.filter(Availability.date >= start)
    if end:
        query = query.filter(Availability.date <= end)
    return query.order_by(Availability.date).all()


@app.get("/availability/vendor/{vendor_id}", response_model=List[AvailabilityOut], tags=["Availability"])
def get_vendor_availability(
    vendor_id: str,
    start: Optional[date] = None,
    end: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """Public endpoint - customers checking a vendor's calendar before booking."""
    query = db.query(Availability).filter(Availability.vendor_id == vendor_id)
    if start:
        query = query.filter(Availability.date >= start)
    if end:
        query = query.filter(Availability.date <= end)
    return query.order_by(Availability.date).all()


# =========================================================================
# BOOKING ENDPOINTS
# =========================================================================

@app.post("/bookings", response_model=BookingOut, status_code=201, tags=["Bookings"])
def create_booking(
    payload: BookingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_customer),
):
    vendor = db.query(VendorProfile).filter(VendorProfile.id == payload.vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    service = db.query(Service).filter(
        Service.id == payload.service_id, Service.vendor_id == vendor.id
    ).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found for this vendor")

    avail = db.query(Availability).filter(
        Availability.vendor_id == vendor.id,
        Availability.date == payload.event_date,
    ).first()
    if avail and avail.status in (AvailabilityStatus.BLOCKED, AvailabilityStatus.BOOKED):
        raise HTTPException(status_code=400, detail="Vendor is not available on this date")

    booking_status = BookingStatus.CONFIRMED if vendor.is_instant_book else BookingStatus.PENDING

    booking = Booking(
        customer_id=current_user.id,
        vendor_id=vendor.id,
        service_id=service.id,
        event_date=payload.event_date,
        guest_count=payload.guest_count,
        notes=payload.notes,
        total_amount=service.price,
        status=booking_status,
    )
    db.add(booking)

    if avail:
        avail.status = AvailabilityStatus.BOOKED if booking_status == BookingStatus.CONFIRMED else AvailabilityStatus.BLOCKED
    else:
        avail = Availability(
            vendor_id=vendor.id,
            date=payload.event_date,
            status=AvailabilityStatus.BOOKED if booking_status == BookingStatus.CONFIRMED else AvailabilityStatus.BLOCKED,
        )
        db.add(avail)

    db.commit()
    db.refresh(booking)
    return booking


@app.get("/bookings/me", response_model=List[BookingOut], tags=["Bookings"])
def get_my_bookings(
    status_filter: Optional[BookingStatus] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role == UserRole.CUSTOMER:
        query = db.query(Booking).filter(Booking.customer_id == current_user.id)
    elif current_user.role == UserRole.VENDOR:
        vendor = db.query(VendorProfile).filter(VendorProfile.user_id == current_user.id).first()
        if not vendor:
            return []
        query = db.query(Booking).filter(Booking.vendor_id == vendor.id)
    else:  # admin sees everything
        query = db.query(Booking)

    if status_filter:
        query = query.filter(Booking.status == status_filter)
    return query.order_by(Booking.created_at.desc()).all()


@app.patch("/bookings/{booking_id}/status", response_model=BookingOut, tags=["Bookings"])
def update_booking_status(
    booking_id: str,
    payload: BookingStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_vendor_or_admin),
):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    if current_user.role == UserRole.VENDOR:
        vendor = db.query(VendorProfile).filter(VendorProfile.user_id == current_user.id).first()
        if not vendor or booking.vendor_id != vendor.id:
            raise HTTPException(status_code=403, detail="Not your booking")

    booking.status = payload.status

    avail = db.query(Availability).filter(
        Availability.vendor_id == booking.vendor_id,
        Availability.date == booking.event_date,
    ).first()

    if payload.status == BookingStatus.CONFIRMED and avail:
        avail.status = AvailabilityStatus.BOOKED
    elif payload.status in (BookingStatus.DECLINED, BookingStatus.CANCELLED) and avail:
        avail.status = AvailabilityStatus.AVAILABLE

    db.commit()
    db.refresh(booking)
    return booking


@app.get("/bookings/{booking_id}", response_model=BookingOut, tags=["Bookings"])
def get_booking(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    if current_user.role == UserRole.CUSTOMER and booking.customer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your booking")
    if current_user.role == UserRole.VENDOR:
        vendor = db.query(VendorProfile).filter(VendorProfile.user_id == current_user.id).first()
        if not vendor or booking.vendor_id != vendor.id:
            raise HTTPException(status_code=403, detail="Not your booking")

    return booking


# =========================================================================
# PAYMENT ENDPOINTS (Razorpay)
# =========================================================================

def get_razorpay_client():
    return razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))


@app.post("/payments/create-order", response_model=PaymentOut, status_code=201, tags=["Payments"])
def create_order(
    payload: PaymentCreateOrder,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_customer),
):
    booking = db.query(Booking).filter(
        Booking.id == payload.booking_id, Booking.customer_id == current_user.id
    ).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    client = get_razorpay_client()
    order = client.order.create({
        "amount": int(payload.amount * 100),  # paise
        "currency": "INR",
        "notes": {"booking_id": booking.id},
    })

    payment = Payment(
        booking_id=booking.id,
        amount=payload.amount,
        provider="razorpay",
        provider_order_id=order["id"],
        status=PaymentStatus.PENDING,
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


@app.post("/payments/verify", response_model=PaymentOut, tags=["Payments"])
def verify_payment(
    payload: PaymentVerify,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_customer),
):
    payment = db.query(Payment).filter(
        Payment.provider_order_id == payload.razorpay_order_id
    ).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment record not found")

    body = f"{payload.razorpay_order_id}|{payload.razorpay_payment_id}"
    expected_signature = hmac.new(
        settings.razorpay_key_secret.encode(),
        body.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, payload.razorpay_signature):
        payment.status = PaymentStatus.FAILED
        db.commit()
        raise HTTPException(status_code=400, detail="Payment signature verification failed")

    payment.status = PaymentStatus.PAID
    payment.provider_payment_id = payload.razorpay_payment_id

    booking = db.query(Booking).filter(Booking.id == payment.booking_id).first()
    if booking and booking.status == BookingStatus.PENDING:
        booking.advance_amount = payment.amount

    db.commit()
    db.refresh(payment)
    return payment


@app.get("/payments/booking/{booking_id}", response_model=List[PaymentOut], tags=["Payments"])
def get_payments_for_booking(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_customer),
):
    booking = db.query(Booking).filter(
        Booking.id == booking_id, Booking.customer_id == current_user.id
    ).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return db.query(Payment).filter(Payment.booking_id == booking_id).all()


# =========================================================================
# CATEGORY ENDPOINTS
# =========================================================================

@app.get("/categories", response_model=List[ServiceCategoryOut], tags=["Categories"])
def list_categories(db: Session = Depends(get_db)):
    return db.query(ServiceCategory).all()


@app.post("/categories", response_model=ServiceCategoryOut, status_code=201, tags=["Categories"])
def create_category(
    name: str,
    icon: Optional[str] = None,
    description: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    existing = db.query(ServiceCategory).filter(ServiceCategory.name == name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Category already exists")
    category = ServiceCategory(name=name, icon=icon, description=description)
    db.add(category)
    db.commit()
    db.refresh(category)
    return category


# =========================================================================
# REVIEW ENDPOINTS
# =========================================================================

@app.post("/reviews", response_model=ReviewOut, status_code=201, tags=["Reviews"])
def create_review(
    payload: ReviewCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_customer),
):
    booking = db.query(Booking).filter(
        Booking.id == payload.booking_id, Booking.customer_id == current_user.id
    ).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.status != BookingStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Can only review completed bookings")
    if booking.review:
        raise HTTPException(status_code=400, detail="Booking already reviewed")

    review = Review(
        booking_id=booking.id,
        customer_id=current_user.id,
        vendor_id=booking.vendor_id,
        rating=payload.rating,
        comment=payload.comment,
    )
    db.add(review)

    vendor = db.query(VendorProfile).filter(VendorProfile.id == booking.vendor_id).first()
    total_score = vendor.rating_avg * vendor.rating_count + payload.rating
    vendor.rating_count += 1
    vendor.rating_avg = round(total_score / vendor.rating_count, 2)

    db.commit()
    db.refresh(review)
    return review


@app.get("/reviews/vendor/{vendor_id}", response_model=List[ReviewOut], tags=["Reviews"])
def get_vendor_reviews(vendor_id: str, db: Session = Depends(get_db)):
    return db.query(Review).filter(Review.vendor_id == vendor_id).all()
