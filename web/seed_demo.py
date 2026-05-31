"""
Seed de productos demo para previsualizar Biobella sin tener la cuenta ML
"novara" sincronizada todavía.

Idempotente: solo inserta si la tabla products está vacía. Cuando la sync real
contra ML corra (en Render), basta con un sync que vacíe + repueble — o se
puede borrar manualmente desde un script.

Cada producto demo lleva `mla_id` con prefijo 'DEMO-' para distinguirlo
trivialmente de un MLA real (que es 'MLA' + 10 dígitos).
"""
from datetime import datetime
from web.db import session_scope
from web.models_tienda import Product


DEMO_PRODUCTS = [
    {
        'mla_id':      'DEMO-001',
        'titulo':      'Aceite Facial de Jojoba Orgánico',
        'descripcion': 'Aceite puro de jojoba prensado en frío, 100% natural. Hidrata profundamente sin obstruir poros. Apto para todo tipo de piel, incluso sensible. Frasco de 30ml con cuentagotas.',
        'fotos':       [
            'https://picsum.photos/seed/jojoba1/800/800',
            'https://picsum.photos/seed/jojoba2/800/800',
            'https://picsum.photos/seed/jojoba3/800/800',
        ],
        'stock':       18,
        'precio_ml':   8500.00,
        'slug':        'aceite-facial-jojoba-organico',
    },
    {
        'mla_id':      'DEMO-002',
        'titulo':      'Mascarilla de Arcilla Rosa Detox',
        'descripcion': 'Arcilla rosa francesa pura, ideal para pieles sensibles. Purifica, calma e ilumina el rostro. 120g.',
        'fotos':       [
            'https://picsum.photos/seed/arcilla1/800/800',
            'https://picsum.photos/seed/arcilla2/800/800',
        ],
        'stock':       24,
        'precio_ml':   4200.00,
        'slug':        'mascarilla-arcilla-rosa-detox',
    },
    {
        'mla_id':      'DEMO-003',
        'titulo':      'Crema Corporal de Karité y Coco',
        'descripcion': 'Manteca de karité sin refinar + aceite de coco virgen. Nutrición intensa para piel seca. Frasco de 250ml.',
        'fotos':       [
            'https://picsum.photos/seed/karite1/800/800',
            'https://picsum.photos/seed/karite2/800/800',
            'https://picsum.photos/seed/karite3/800/800',
        ],
        'stock':       12,
        'precio_ml':   6900.00,
        'slug':        'crema-corporal-karite-coco',
    },
    {
        'mla_id':      'DEMO-004',
        'titulo':      'Sérum Iluminador Vitamina C 15%',
        'descripcion': 'Vitamina C estabilizada al 15% + ácido hialurónico. Atenúa manchas, ilumina el tono y combate signos del tiempo. 30ml con dosificador.',
        'fotos':       [
            'https://picsum.photos/seed/serum1/800/800',
            'https://picsum.photos/seed/serum2/800/800',
        ],
        'stock':       9,
        'precio_ml':   12400.00,
        'slug':        'serum-iluminador-vitamina-c',
    },
    {
        'mla_id':      'DEMO-005',
        'titulo':      'Bálsamo Labial Cera de Abejas',
        'descripcion': 'Bálsamo artesanal con cera de abejas, aceite de oliva y miel. Hidrata y repara labios resecos. Pack de 2 unidades de 8g.',
        'fotos':       [
            'https://picsum.photos/seed/balsamo1/800/800',
        ],
        'stock':       40,
        'precio_ml':   2800.00,
        'slug':        'balsamo-labial-cera-abejas',
    },
    {
        'mla_id':      'DEMO-006',
        'titulo':      'Tónico Facial de Hamamelis',
        'descripcion': 'Agua floral de hamamelis pura. Astringente natural que cierra poros y equilibra pieles mixtas. 200ml en frasco de vidrio.',
        'fotos':       [
            'https://picsum.photos/seed/tonico1/800/800',
            'https://picsum.photos/seed/tonico2/800/800',
        ],
        'stock':       16,
        'precio_ml':   5400.00,
        'slug':        'tonico-facial-hamamelis',
    },
]


def seed_if_empty() -> int:
    """Inserta productos demo solo si la tabla está vacía. Devuelve cuántos creó."""
    with session_scope() as s:
        if s.query(Product).count() > 0:
            return 0
        now = datetime.now()
        for p in DEMO_PRODUCTS:
            s.add(Product(
                mla_id=p['mla_id'],
                titulo=p['titulo'],
                descripcion=p['descripcion'],
                fotos=p['fotos'],
                variantes=[],
                stock=p['stock'],
                precio_ml=p['precio_ml'],
                slug=p['slug'],
                activo=True,
                last_sync_at=now,
            ))
        return len(DEMO_PRODUCTS)


def clear_demo() -> int:
    """Borra todos los productos demo (mla_id que arranca con 'DEMO-')."""
    with session_scope() as s:
        n = s.query(Product).filter(Product.mla_id.like('DEMO-%')).delete(synchronize_session=False)
        return n


if __name__ == '__main__':
    n = seed_if_empty()
    print(f'Insertados {n} productos demo' if n else 'Ya hay productos en DB — no inserto demos.')
