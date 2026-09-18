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

from sqlalchemy import (
    Boolean, Column, DateTime, Index, Integer, JSON, Numeric, String, UniqueConstraint,
)

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


class PrecioExperimento(Base):
    """Un cambio de precio/estrategia aplicado, a medir dia a dia.

    `item_ids` son las publicaciones que participan — hoy siempre una sola
    (la existente, editada por /api/pricing/aplicar); cuando el generador de
    trio (sprint 5) cree las dos nuevas, se suman aca.
    """
    __tablename__ = 'precio_experimentos'
    __table_args__ = (
        Index('ix_experimento_alias_producto', 'alias', 'producto_key'),
        Index('ix_experimento_abiertos', 'alias', 'cerrado_en'),
    )

    id                  = Column(Integer, primary_key=True)
    alias               = Column(String(80), nullable=False)
    producto_key        = Column(String(120), nullable=False)
    item_ids            = Column(JSON, nullable=False)          # ["MLA123", ...]

    estrategia          = Column(String(20), nullable=False)    # rent | comp | vel | meta
    modo                = Column(String(10), nullable=False)    # roi | margen
    objetivo            = Column(Numeric(8, 2), nullable=False)
    piso                = Column(Numeric(8, 2), nullable=False)
    costo               = Column(Numeric(12, 2), nullable=False)

    precios_antes       = Column(JSON, nullable=False)          # {item_id: precio}
    precios_despues     = Column(JSON, nullable=False)
    ganancia_venta      = Column(JSON, nullable=False)          # {item_id: ganancia}

    ventas_dia_previas  = Column(Numeric(10, 2), nullable=False)
    ganancia_dia_previa = Column(Numeric(14, 2), nullable=False)
    meta_ventas_dia     = Column(Numeric(10, 2), nullable=False)
    publicidad_dia      = Column(Numeric(12, 2), nullable=False, default=0)

    competidor_min      = Column(Numeric(12, 2), nullable=True)
    competidor_max      = Column(Numeric(12, 2), nullable=True)

    iniciado_en         = Column(DateTime, nullable=False, default=datetime.now)
    cerrado_en          = Column(DateTime, nullable=True)
    veredicto           = Column(String(20), nullable=False, default='pendiente')
    # pendiente | conviene | no_conviene | inconcluso
    notas               = Column(String(2000), nullable=True)

    def __repr__(self):
        return f'<PrecioExperimento {self.id} {self.producto_key} {self.estrategia}>'


class SnapshotDiario(Base):
    """Foto diaria por publicacion de un experimento abierto (job 06:00 ART)."""
    __tablename__ = 'snapshots_diarios'
    __table_args__ = (
        UniqueConstraint('alias', 'item_id', 'fecha', name='uq_snapshot_alias_item_fecha'),
        Index('ix_snapshot_item_fecha', 'item_id', 'fecha'),
    )

    id                  = Column(Integer, primary_key=True)
    alias               = Column(String(80), nullable=False)
    item_id             = Column(String(40), nullable=False)
    fecha               = Column(String(10), nullable=False)    # YYYY-MM-DD (ART)

    precio              = Column(Numeric(12, 2), nullable=True)
    listing_type        = Column(String(30), nullable=True)
    cuotas              = Column(Integer, nullable=True)
    ventas_dia          = Column(Numeric(10, 2), nullable=True)  # delta de sold_quantity vs ayer
    sold_quantity_acum  = Column(Integer, nullable=True)         # crudo, para calcular el proximo delta
    visitas_dia         = Column(Integer, nullable=True)
    stock               = Column(Integer, nullable=True)
    competidor_min      = Column(Numeric(12, 2), nullable=True)
    fee_rate            = Column(Numeric(6, 3), nullable=True)

    def __repr__(self):
        return f'<SnapshotDiario {self.item_id} {self.fecha}>'


class CurvaDemanda(Base):
    """Un punto de la curva de demanda del producto, uno por experimento cerrado."""
    __tablename__ = 'curva_demanda'
    __table_args__ = (
        Index('ix_curva_alias_producto', 'alias', 'producto_key'),
    )

    id              = Column(Integer, primary_key=True)
    alias           = Column(String(80), nullable=False)
    producto_key    = Column(String(120), nullable=False)
    experimento_id  = Column(Integer, nullable=False)

    precio          = Column(Numeric(12, 2), nullable=False)
    ventas_dia      = Column(Numeric(10, 2), nullable=False)
    ganancia_dia    = Column(Numeric(14, 2), nullable=False)
    dias_medidos    = Column(Integer, nullable=False)
    registrado_en   = Column(DateTime, nullable=False, default=datetime.now)

    def __repr__(self):
        return f'<CurvaDemanda {self.producto_key} ${self.precio}>'
