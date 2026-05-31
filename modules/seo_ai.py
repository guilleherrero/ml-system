"""
Generador de meta_title y meta_description optimizados con Claude Haiku.

Por qué Haiku y no Opus/Sonnet:
- Tarea simple: dar título corto + descripción de ~155 chars
- 80-100 tokens de output por producto → Haiku basta y cuesta ~$0.00007 por producto
- Para 100 productos del catálogo = $0.007 total

Costos estimados (Haiku 4.5): input $0.25/Mtok, output $1.25/Mtok.
Input típico ~400 tok + output ~120 tok = ~$0.00025 / producto. Bulk de 200 prod = $0.05.
"""
import os
import re
import json
import anthropic

CLAUDE_MODEL = 'claude-haiku-4-5-20251001'


def _build_prompt(titulo: str, descripcion: str, precio: float, store_name: str) -> str:
    desc_short = (descripcion or '').strip().replace('\n', ' ')[:600]
    return f"""Sos un experto en SEO para e-commerce y Google Shopping en Argentina.

Generá meta_title y meta_description optimizados para este producto:

Producto: {titulo}
Marca/tienda: {store_name}
Precio: ARS ${precio:,.0f}
Descripción del vendedor:
{desc_short}

Reglas estrictas:
1. meta_title: entre 50 y 60 caracteres EXACTOS. Incluí la keyword principal del producto + nombre marca al final. No uses MAYÚSCULAS innecesarias ni emojis.
2. meta_description: entre 150 y 160 caracteres EXACTOS. Incluí beneficio principal + CTA + envío. No empieces con "Descubrí". Usá voseo argentino. No incluyas precio ni "$".
3. Optimizá para Google Shopping: lenguaje natural, palabras clave que la gente escribe en Google.
4. NO inventes propiedades que no estén en la descripción.

Respondé EXCLUSIVAMENTE con un JSON válido en una sola línea, formato:
{{"meta_title": "...", "meta_description": "..."}}

Sin texto adicional, sin markdown, sin explicación. Solo el JSON."""


def generar_seo_ia(titulo: str, descripcion: str, precio: float, store_name: str = 'Biobella') -> dict:
    """
    Llama a Claude Haiku con el producto y devuelve {meta_title, meta_description}.
    Si falla, lanza excepción — el caller debe manejar fallback.
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('ANTHROPIC_API_KEY no configurada en env')

    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_prompt(titulo, descripcion, precio, store_name)

    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=256,
        messages=[{'role': 'user', 'content': prompt}],
    )

    # Extraer texto
    txt = ''
    for block in resp.content:
        if hasattr(block, 'text'):
            txt += block.text
    txt = txt.strip()

    # Limpiar fences si Claude las puso
    txt = re.sub(r'^```(?:json)?\s*', '', txt)
    txt = re.sub(r'\s*```$', '', txt)
    txt = txt.strip()

    try:
        data = json.loads(txt)
    except json.JSONDecodeError as e:
        # Intento de fallback: extraer con regex
        m_title = re.search(r'"meta_title"\s*:\s*"([^"]+)"', txt)
        m_desc  = re.search(r'"meta_description"\s*:\s*"([^"]+)"', txt)
        if m_title and m_desc:
            data = {'meta_title': m_title.group(1), 'meta_description': m_desc.group(1)}
        else:
            raise ValueError(f'Claude respondió formato inválido: {txt[:200]}') from e

    mt = (data.get('meta_title') or '').strip()
    md = (data.get('meta_description') or '').strip()
    if not mt or not md:
        raise ValueError(f'Claude devolvió campos vacíos: {data}')

    # Trunco por seguridad — el modelo a veces se pasa
    if len(mt) > 70:
        mt = mt[:69].rstrip() + '…'
    if len(md) > 170:
        md = md[:169].rstrip() + '…'

    # Estimar costo aproximado para logging
    in_tokens  = getattr(resp.usage, 'input_tokens', 0)
    out_tokens = getattr(resp.usage, 'output_tokens', 0)
    cost_usd = (in_tokens * 0.25 + out_tokens * 1.25) / 1_000_000

    return {
        'meta_title':       mt,
        'meta_description': md,
        'tokens_input':     in_tokens,
        'tokens_output':    out_tokens,
        'cost_usd':         round(cost_usd, 6),
    }
