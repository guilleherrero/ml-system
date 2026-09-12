"""
Modelos SQLAlchemy del sistema contable.

Principio de diseño: UN SOLO LIBRO.

Todo movimiento de plata — venta de ML, cargo de comisión, cargo de envío,
Product Ads, almacenamiento Full, percepción impositiva, cobro o gasto de
Mercado Pago, y el gasto que Guille carga a mano — cae en la misma tabla
(`cont_movimientos`). No hay tablas paralelas por fuente. Eso es lo que
permite garantizar "ni un dato sin contabilizar": si está en el libro, se
cuenta; si no se pudo clasificar, queda con rubro NULL y aparece en la
bandeja de pendientes, nunca se descarta en silencio.

Tablas:
- cont_rubros        — plan de rubros (ingresos, costos, gastos, impuestos, personal)
- cont_movimientos   — el libro único
- cont_reglas        — reglas de clasificación automática por prioridad
- cont_import_runs   — auditoría de cada corrida de importación
- cont_cuentas_mp    — cuentas de Mercado Pago asociadas (multicuenta)
- cont_costos        — costo de mercadería por publicación (COGS)

Convención de signos: `monto` viene firmado. Positivo = entra plata,
negativo = sale plata. Así el resultado es una suma y no hay que recordar
qué rubro se resta.
"""
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, JSON,
    Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from web.db import Base


# ── Vocabulario controlado ────────────────────────────────────────────────────

# Tipo de rubro: define cómo participa en el resultado
TIPO_INGRESO    = 'INGRESO'
TIPO_COSTO      = 'COSTO'       # costo de mercadería vendida
TIPO_GASTO      = 'GASTO'       # gasto operativo o de plataforma
TIPO_IMPUESTO   = 'IMPUESTO'
TIPO_FINANCIERO = 'FINANCIERO'
TIPO_NEUTRO     = 'NEUTRO'      # traspasos, personal: no afectan resultado

TIPOS_RUBRO = (
    TIPO_INGRESO, TIPO_COSTO, TIPO_GASTO,
    TIPO_IMPUESTO, TIPO_FINANCIERO, TIPO_NEUTRO,
)

# Ámbito: separa la plata del negocio de la personal
AMBITO_NEGOCIO  = 'negocio'
AMBITO_PERSONAL = 'personal'

# Origen del movimiento — de dónde salió el dato
ORIGEN_ML_BILLING     = 'ml_billing'      # /billing/integration/.../details
ORIGEN_ML_SUMMARY     = 'ml_summary'      # /billing/.../summary/details (control)
ORIGEN_ML_PERCEPCION  = 'ml_percepcion'   # /billing/.../perceptions/summary
ORIGEN_ML_ORDER       = 'ml_order'        # /orders/search — la venta en sí
ORIGEN_MP_PAYMENT     = 'mp_payment'      # /v1/payments/search
ORIGEN_MANUAL         = 'manual'          # cargado a mano por Guille
ORIGEN_CMV            = 'cmv'             # derivado: costo de lo vendido

ORIGENES = (
    ORIGEN_ML_BILLING, ORIGEN_ML_SUMMARY, ORIGEN_ML_PERCEPCION,
    ORIGEN_ML_ORDER, ORIGEN_MP_PAYMENT, ORIGEN_MANUAL, ORIGEN_CMV,
)

# Código del rubro que actúa como bandeja de entrada
RUBRO_SIN_CLASIFICAR = 'SIN_CLASIF'


class Rubro(Base):
    """
    Plan de rubros. Es la columna vertebral del "separarlos por rubros".

    `afecta_resultado` es lo que permite que los movimientos personales y los
    traspasos entre cuentas propias queden registrados y auditables pero no
    ensucien el cálculo de cuánto gana el negocio.
    """
    __tablename__ = 'cont_rubros'

    id               = Column(Integer, primary_key=True)
    codigo           = Column(String(32), nullable=False, unique=True, index=True)
    nombre           = Column(String(120), nullable=False)
    tipo             = Column(String(16), nullable=False)
    ambito           = Column(String(16), nullable=False, default=AMBITO_NEGOCIO)

    # Si es False, el movimiento se guarda y se ve, pero no entra al resultado.
    afecta_resultado = Column(Boolean, nullable=False, default=True)

    # Para agrupar en la UI ("Plataforma", "Impuestos", "Operativos"...)
    grupo            = Column(String(60), nullable=True)
    orden            = Column(Integer, nullable=False, default=100)
    descripcion      = Column(Text, nullable=True)

    # Los rubros del sistema no se pueden borrar desde la UI
    es_sistema       = Column(Boolean, nullable=False, default=False)
    activo           = Column(Boolean, nullable=False, default=True)
    created_at       = Column(DateTime, nullable=False, default=datetime.now)

    movimientos = relationship('Movimiento', back_populates='rubro')
    reglas      = relationship('ReglaClasificacion', back_populates='rubro')

    def __repr__(self):
        return f'<Rubro {self.codigo} {self.nombre}>'


class Movimiento(Base):
    """
    El libro único. Una fila por movimiento de plata, venga de donde venga.

    Idempotencia: la constraint (cuenta_alias, origen, external_id) permite
    reimportar el mismo período N veces sin duplicar nada. El importador hace
    upsert sobre esa clave.
    """
    __tablename__ = 'cont_movimientos'
    __table_args__ = (
        UniqueConstraint('cuenta_alias', 'origen', 'external_id',
                         name='uq_mov_cuenta_origen_extid'),
        Index('ix_mov_fecha', 'fecha'),
        Index('ix_mov_cuenta_fecha', 'cuenta_alias', 'fecha'),
        Index('ix_mov_rubro', 'rubro_id'),
        Index('ix_mov_periodo', 'periodo'),
        Index('ix_mov_computable', 'computable'),
    )

    id            = Column(Integer, primary_key=True)

    # ── Identidad y procedencia ──
    cuenta_alias  = Column(String(80), nullable=False, index=True)
    origen        = Column(String(24), nullable=False, index=True)
    external_id   = Column(String(120), nullable=False)

    # Período contable 'YYYY-MM' — denormalizado a propósito: los resúmenes
    # mensuales son la consulta más frecuente y así no hay date_trunc por fila.
    periodo       = Column(String(7), nullable=False)
    fecha         = Column(DateTime, nullable=False)

    # ── Qué fue ──
    concepto      = Column(String(300), nullable=False, default='')
    # Subtipo nativo de ML (CV, CXD, PADS, CFLX...) o tipo de pago de MP.
    # Se guarda crudo para poder clasificar subtipos nuevos sin reimportar.
    subtipo       = Column(String(40), nullable=True, index=True)

    # ── Plata ──
    # Firmado: + entra, - sale. Es el campo que se suma.
    monto         = Column(Numeric(16, 2), nullable=False, default=0)
    moneda        = Column(String(8), nullable=False, default='ARS')

    # Desagregado, para habilitar la vista neta de IVA a futuro sin reimportar
    monto_bruto   = Column(Numeric(16, 2), nullable=True)
    iva           = Column(Numeric(16, 2), nullable=True)
    percepciones  = Column(Numeric(16, 2), nullable=True)
    retenciones   = Column(Numeric(16, 2), nullable=True)
    comision      = Column(Numeric(16, 2), nullable=True)
    descuento     = Column(Numeric(16, 2), nullable=True)

    # ── Clasificación ──
    # NULL = sin clasificar → cae en la bandeja de pendientes. Nunca se descarta.
    rubro_id      = Column(Integer, ForeignKey('cont_rubros.id'), nullable=True)
    ambito        = Column(String(16), nullable=False, default=AMBITO_NEGOCIO)
    # True cuando un humano confirmó el rubro: el reclasificador no lo pisa.
    rubro_manual  = Column(Boolean, nullable=False, default=False)
    regla_id      = Column(Integer, nullable=True)  # qué regla lo clasificó

    # ── Estado ──
    estado        = Column(String(40), nullable=True)  # approved, refunded, rejected...
    # False para rechazados, cancelados y anulados: quedan en el libro para
    # auditoría pero fuera de los totales.
    computable    = Column(Boolean, nullable=False, default=True)
    # Marca de "mirá esto": monto raro, traspaso grande, subtipo desconocido.
    revisar       = Column(Boolean, nullable=False, default=False)
    nota_revision = Column(String(300), nullable=True)

    # ── Trazabilidad al negocio ──
    order_id      = Column(String(40), nullable=True, index=True)
    item_id       = Column(String(40), nullable=True, index=True)
    payment_id    = Column(String(40), nullable=True)
    shipping_id   = Column(String(40), nullable=True)
    documento     = Column(String(60), nullable=True)  # nro de factura legal
    cantidad      = Column(Integer, nullable=True)     # unidades, si aplica

    # ── Campos de carga manual ──
    proveedor        = Column(String(160), nullable=True)
    tipo_comprobante = Column(String(40), nullable=True)   # Factura A, Ticket...
    nro_comprobante  = Column(String(60), nullable=True)
    medio_pago       = Column(String(60), nullable=True)
    notas            = Column(Text, nullable=True)

    # Payload crudo de la API. Es el seguro: si mañana descubrimos que un campo
    # importaba, está acá y no hay que volver a pegarle a la API.
    raw           = Column(JSON, nullable=True)

    created_at    = Column(DateTime, nullable=False, default=datetime.now)
    updated_at    = Column(DateTime, nullable=False, default=datetime.now,
                           onupdate=datetime.now)

    rubro = relationship('Rubro', back_populates='movimientos')

    def __repr__(self):
        return (f'<Mov {self.cuenta_alias} {self.fecha:%Y-%m-%d} '
                f'{self.concepto[:30]} {self.monto}>')


class ReglaClasificacion(Base):
    """
    Regla de clasificación automática. Se evalúan por `prioridad` ascendente
    y gana la primera que matchea.

    Existen para que Guille pueda enseñarle al sistema desde la bandeja de
    pendientes: clasifica un movimiento a mano, y de ahí sale una regla que
    resuelve todos los parecidos en adelante.
    """
    __tablename__ = 'cont_reglas'
    __table_args__ = (
        Index('ix_regla_prioridad', 'prioridad'),
    )

    id            = Column(Integer, primary_key=True)
    nombre        = Column(String(160), nullable=False, default='')
    prioridad     = Column(Integer, nullable=False, default=100)

    # Campo del movimiento a evaluar: concepto | subtipo | origen | proveedor
    campo         = Column(String(24), nullable=False, default='concepto')
    # igual | contiene | empieza | regex | en_lista
    operador      = Column(String(16), nullable=False, default='contiene')
    valor         = Column(String(400), nullable=False)
    # Restringe la regla a un origen puntual (opcional)
    solo_origen   = Column(String(24), nullable=True)
    # Restringe por signo: 'positivo', 'negativo' o None para cualquiera
    solo_signo    = Column(String(10), nullable=True)

    rubro_id      = Column(Integer, ForeignKey('cont_rubros.id'), nullable=False)
    ambito        = Column(String(16), nullable=True)  # None = hereda del rubro
    marcar_revisar = Column(Boolean, nullable=False, default=False)

    es_sistema    = Column(Boolean, nullable=False, default=False)
    activo        = Column(Boolean, nullable=False, default=True)
    veces_aplicada = Column(Integer, nullable=False, default=0)
    created_at    = Column(DateTime, nullable=False, default=datetime.now)

    rubro = relationship('Rubro', back_populates='reglas')

    def __repr__(self):
        return f'<Regla {self.prioridad} {self.campo} {self.operador} {self.valor!r}>'


class ImportRun(Base):
    """
    Auditoría de cada corrida de importación. Sirve para dos cosas:
    saber hasta dónde se importó (y no volver a pedir lo mismo, que en billing
    es causa de 429), y tener el rastro de errores cuando un total no cierra.
    """
    __tablename__ = 'cont_import_runs'
    __table_args__ = (
        Index('ix_import_cuenta_fuente', 'cuenta_alias', 'fuente'),
    )

    id            = Column(Integer, primary_key=True)
    cuenta_alias  = Column(String(80), nullable=False)
    fuente        = Column(String(40), nullable=False)   # ver ORIGENES
    periodo       = Column(String(7), nullable=True)     # 'YYYY-MM' si aplica
    desde         = Column(Date, nullable=True)
    hasta         = Column(Date, nullable=True)

    iniciado_at   = Column(DateTime, nullable=False, default=datetime.now)
    terminado_at  = Column(DateTime, nullable=True)
    estado        = Column(String(20), nullable=False, default='corriendo')
    # corriendo | ok | parcial | error

    leidos        = Column(Integer, nullable=False, default=0)
    nuevos        = Column(Integer, nullable=False, default=0)
    actualizados  = Column(Integer, nullable=False, default=0)
    sin_clasificar = Column(Integer, nullable=False, default=0)
    paginas       = Column(Integer, nullable=False, default=0)

    mensaje       = Column(Text, nullable=True)
    errores       = Column(JSON, nullable=True)

    def __repr__(self):
        return (f'<ImportRun {self.cuenta_alias} {self.fuente} '
                f'{self.periodo or ""} {self.estado}>')


class CuentaMP(Base):
    """
    Cuenta de Mercado Pago asociada. Permite el requisito de multicuenta:
    varias cuentas ML y MP, con vista global y vista separada.

    El token se resuelve con prioridad a la variable de entorno (`token_env`),
    para no guardar secretos en la base si no hace falta. Si no hay env var,
    cae al valor guardado.
    """
    __tablename__ = 'cont_cuentas_mp'

    id            = Column(Integer, primary_key=True)
    alias         = Column(String(80), nullable=False, unique=True, index=True)
    # Alias de la cuenta ML con la que se corresponde (para conciliar)
    ml_alias      = Column(String(80), nullable=True, index=True)

    collector_id  = Column(String(40), nullable=True)
    nickname      = Column(String(120), nullable=True)

    token_env     = Column(String(80), nullable=True)
    access_token  = Column(Text, nullable=True)

    activo        = Column(Boolean, nullable=False, default=True)
    last_import_at = Column(DateTime, nullable=True)
    created_at    = Column(DateTime, nullable=False, default=datetime.now)

    def __repr__(self):
        return f'<CuentaMP {self.alias}>'


class CostoProducto(Base):
    """
    Costo de mercadería por publicación (COGS). Sin esto no hay ganancia
    posible, solo facturación.

    `vigente_desde` permite versionar: si un lote nuevo entró más caro, se
    agrega una fila y las ventas anteriores siguen valuadas al costo viejo.
    """
    __tablename__ = 'cont_costos'
    __table_args__ = (
        UniqueConstraint('item_id', 'variacion', 'vigente_desde',
                         name='uq_costo_item_var_desde'),
        Index('ix_costo_item', 'item_id'),
    )

    id            = Column(Integer, primary_key=True)
    item_id       = Column(String(40), nullable=False)
    variacion     = Column(String(120), nullable=False, default='')
    sku           = Column(String(80), nullable=True)
    titulo        = Column(String(300), nullable=True)

    # Costo unitario puesto en depósito (FOB + flete + despacho + impuestos)
    costo_unitario = Column(Numeric(16, 2), nullable=False, default=0)
    moneda_costo   = Column(String(8), nullable=False, default='ARS')
    # Desagregado opcional, para trazar de dónde sale el costo
    fob           = Column(Numeric(16, 2), nullable=True)
    flete_prorrateado = Column(Numeric(16, 2), nullable=True)
    impuestos_import  = Column(Numeric(16, 2), nullable=True)

    vigente_desde = Column(Date, nullable=False)
    origen_dato   = Column(String(40), nullable=False, default='manual')
    # manual | excel | costos_json | calculadora
    notas         = Column(Text, nullable=True)
    created_at    = Column(DateTime, nullable=False, default=datetime.now)
    updated_at    = Column(DateTime, nullable=False, default=datetime.now,
                           onupdate=datetime.now)

    def __repr__(self):
        return f'<Costo {self.item_id} {self.costo_unitario}>'
