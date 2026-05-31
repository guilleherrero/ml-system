"""
Modelos SQLAlchemy para la tienda Biobella.

Tablas:
- products        — catálogo sincronizado desde la cuenta ML (novara)
- product_locks   — campos lockeados para que la sync no los pise (override admin)
- sync_log        — historial de sincronizaciones (auto cada 6h + manuales)
- app_settings    — config key/value editable desde /admin/ajustes
"""
from datetime import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON,
    Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from web.db import Base


LOCKABLE_FIELDS = {'titulo', 'descripcion', 'fotos', 'variantes', 'stock', 'precio'}


class Product(Base):
    __tablename__ = 'products'

    id                       = Column(Integer, primary_key=True)
    mla_id                   = Column(String(32), nullable=False, unique=True, index=True)

    # Campos sincronizados desde ML (la sync los pisa salvo lock)
    titulo                   = Column(String(512), nullable=False, default='')
    descripcion              = Column(Text, nullable=False, default='')
    fotos                    = Column(JSON, nullable=False, default=list)   # [url, url, …]
    variantes                = Column(JSON, nullable=False, default=list)   # raw ML variations
    stock                    = Column(Integer, nullable=False, default=0)
    precio_ml                = Column(Numeric(12, 2), nullable=False, default=0)

    # Override local (NUNCA tocado por sync)
    precio_tienda_override   = Column(Numeric(12, 2), nullable=True)
    margen_override          = Column(Numeric(6, 2),  nullable=True)  # porcentaje, p.ej. 35.00

    # SEO / storefront
    slug                     = Column(String(255), nullable=True, unique=True, index=True)
    meta_title               = Column(String(255), nullable=True)
    meta_description         = Column(Text,        nullable=True)

    # Estado
    activo                   = Column(Boolean, nullable=False, default=True)
    created_at               = Column(DateTime, nullable=False, default=datetime.now)
    updated_at               = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    last_sync_at             = Column(DateTime, nullable=True)

    locks = relationship('ProductLock', back_populates='product', cascade='all, delete-orphan')


class ProductLock(Base):
    """
    Marca un campo de un producto como "lockeado": la sync no lo pisará.
    Si el row existe → ese campo está bajo control del admin.
    Si no existe    → la sync puede sobreescribirlo.
    """
    __tablename__ = 'product_locks'
    __table_args__ = (
        UniqueConstraint('product_id', 'field', name='uq_product_lock_field'),
        Index('ix_product_lock_product', 'product_id'),
    )

    id          = Column(Integer, primary_key=True)
    product_id  = Column(Integer, ForeignKey('products.id', ondelete='CASCADE'), nullable=False)
    field       = Column(String(32), nullable=False)  # ver LOCKABLE_FIELDS
    locked_at   = Column(DateTime, nullable=False, default=datetime.now)
    locked_by   = Column(String(64), nullable=True)   # username del admin

    product = relationship('Product', back_populates='locks')


class SyncLog(Base):
    __tablename__ = 'sync_log'
    __table_args__ = (
        Index('ix_synclog_started', 'started_at'),
    )

    id                       = Column(Integer, primary_key=True)
    started_at               = Column(DateTime, nullable=False, default=datetime.now)
    finished_at              = Column(DateTime, nullable=True)
    trigger                  = Column(String(16), nullable=False)  # 'auto' | 'manual'
    triggered_by             = Column(String(64), nullable=True)   # username si manual
    status                   = Column(String(16), nullable=False, default='running')  # running|ok|error|partial
    productos_actualizados   = Column(Integer, nullable=False, default=0)
    productos_creados        = Column(Integer, nullable=False, default=0)
    productos_desactivados   = Column(Integer, nullable=False, default=0)
    errores                  = Column(JSON, nullable=False, default=list)  # [{mla_id, msg}, …]
    error_resumen            = Column(Text, nullable=True)


class Review(Base):
    """Reseña de cliente sobre un producto. Modera el admin antes de mostrarla."""
    __tablename__ = 'reviews'
    __table_args__ = (
        Index('ix_review_product', 'product_id'),
        Index('ix_review_aprobado', 'aprobado'),
    )

    id          = Column(Integer, primary_key=True)
    product_id  = Column(Integer, ForeignKey('products.id', ondelete='CASCADE'), nullable=False)
    nombre      = Column(String(80), nullable=False)
    email       = Column(String(160), nullable=True)
    rating      = Column(Integer, nullable=False, default=5)  # 1–5
    titulo      = Column(String(160), nullable=True)
    comentario  = Column(Text, nullable=False)
    aprobado    = Column(Boolean, nullable=False, default=False)
    created_at  = Column(DateTime, nullable=False, default=datetime.now)
    moderated_at = Column(DateTime, nullable=True)
    moderated_by = Column(String(64), nullable=True)
    ip_addr     = Column(String(64), nullable=True)  # anti-spam tracking

    product = relationship('Product')


class Order(Base):
    """Orden de compra creada al iniciar checkout. Estado se actualiza vía webhook MP."""
    __tablename__ = 'orders'
    __table_args__ = (
        Index('ix_order_created', 'created_at'),
        Index('ix_order_status', 'status'),
    )

    id                  = Column(Integer, primary_key=True)
    mp_preference_id    = Column(String(64), nullable=True, index=True)
    mp_payment_id       = Column(String(64), nullable=True, index=True)
    mp_status           = Column(String(32), nullable=True)  # approved|pending|rejected|in_process|cancelled|refunded
    status              = Column(String(16), nullable=False, default='pending')  # pending|paid|failed|cancelled
    cliente_nombre      = Column(String(120), nullable=False)
    cliente_email       = Column(String(160), nullable=False)
    cliente_telefono    = Column(String(40), nullable=True)
    direccion_calle     = Column(String(200), nullable=True)
    direccion_numero    = Column(String(20), nullable=True)
    direccion_piso      = Column(String(40), nullable=True)
    direccion_localidad = Column(String(120), nullable=True)
    direccion_provincia = Column(String(60), nullable=True)
    direccion_cp        = Column(String(20), nullable=True)
    notas               = Column(Text, nullable=True)
    subtotal            = Column(Numeric(12, 2), nullable=False, default=0)
    envio               = Column(Numeric(12, 2), nullable=False, default=0)
    total               = Column(Numeric(12, 2), nullable=False, default=0)
    created_at          = Column(DateTime, nullable=False, default=datetime.now)
    paid_at             = Column(DateTime, nullable=True)
    raw_webhook         = Column(JSON, nullable=True)  # último payload MP recibido

    items = relationship('OrderItem', back_populates='order', cascade='all, delete-orphan')


class OrderItem(Base):
    __tablename__ = 'order_items'

    id            = Column(Integer, primary_key=True)
    order_id      = Column(Integer, ForeignKey('orders.id', ondelete='CASCADE'), nullable=False)
    product_id    = Column(Integer, ForeignKey('products.id', ondelete='SET NULL'), nullable=True)
    mla_id        = Column(String(32), nullable=True)
    titulo        = Column(String(512), nullable=False)
    foto          = Column(String(512), nullable=True)
    precio_unit   = Column(Numeric(12, 2), nullable=False)
    cantidad      = Column(Integer, nullable=False, default=1)
    subtotal      = Column(Numeric(12, 2), nullable=False)

    order = relationship('Order', back_populates='items')


class AppSetting(Base):
    """
    Config global key/value. Valores son JSON serializados como TEXT para soportar
    int/float/str/dict.

    Keys esperados:
    - store_name              → "Biobella"
    - margen_global_default   → 35.0 (porcentaje aplicado a productos nuevos)
    - mp_access_token         → token MercadoPago
    - mp_public_key           → public key MP
    """
    __tablename__ = 'app_settings'

    key         = Column(String(64), primary_key=True)
    value       = Column(Text, nullable=False)  # JSON-encoded
    updated_at  = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


# ── Helpers ───────────────────────────────────────────────────────────────────

def calc_precio_tienda(precio_ml, margen_pct, override=None):
    """
    Calcula precio_tienda final.
    Si override está definido → ese gana.
    Si no → round(precio_ml × (1 + margen/100)) al entero más cercano.
    """
    if override is not None:
        return float(override)
    if precio_ml is None or margen_pct is None:
        return None
    return round(float(precio_ml) * (1.0 + float(margen_pct) / 100.0))
