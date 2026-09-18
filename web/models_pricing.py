"""
Modelos SQLAlchemy de la Calculadora de Estrategia de Precios.

Ver docs/CEREBRO.md seccion 12 y modules/precio_motor.py para el motor. Solo
`PricingConfig` existe por ahora (Sprint 1/2): guarda los cargos e IIBB/
percepcion de IVA + los 3 perfiles (Batalla/Medio/Compensa) por cuenta, con
override opcional por item_id. `precio_experimentos`, `snapshots_diarios` y
`curva_demanda` se agregan en los sprints 3/4 cuando haya algo que escribir
ahi (aplicar un cambio de precio y medirlo dia a dia).
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, JSON, Numeric, String, UniqueConstraint

from web.db import Base


class PricingConfig(Base):
    """Cargos y perfiles de publicacion para la calculadora de precios.

    `item_id` NULL = configuracion por defecto de la cuenta. Una fila con
    `item_id` puesto sobreescribe la de la cuenta para ese producto puntual
    (ej. un producto con costo de envio real distinto al estimado).
    """
    __tablename__ = 'pricing_config'
    __table_args__ = (
        UniqueConstraint('alias', 'item_id', name='uq_pricing_config_alias_item'),
        Index('ix_pricing_config_alias', 'alias'),
    )

    id                = Column(Integer, primary_key=True)
    alias             = Column(String(80), nullable=False)
    item_id           = Column(String(40), nullable=True)

    iibb              = Column(Numeric(6, 3), nullable=False, default=3.0)
    percepcion_iva    = Column(Numeric(6, 3), nullable=False, default=7.0)
    envio             = Column(Numeric(12, 2), nullable=False, default=5000)
    umbral_envio      = Column(Numeric(12, 2), nullable=False, default=33000)
    envio_bajo_umbral = Column(Boolean, nullable=False, default=False)
    fijo_menos_15k    = Column(Numeric(12, 2), nullable=False, default=1115)
    fijo_15k_25k      = Column(Numeric(12, 2), nullable=False, default=2300)
    fijo_25k_umbral   = Column(Numeric(12, 2), nullable=False, default=2810)
    redondeo          = Column(Boolean, nullable=False, default=True)

    # Lista de 3 perfiles: [{"nombre","comision","costo_cuotas",...}, ...]
    perfiles          = Column(JSON, nullable=False)

    actualizado_en    = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    def __repr__(self):
        return f'<PricingConfig {self.alias}/{self.item_id or "default"}>'
