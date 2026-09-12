/* Enviar a Cerebro — elegir competidores mirando la foto.
 *
 * ML bloqueo el acceso programatico a los resultados de busqueda, asi que el
 * sistema no puede ver quien aparece arriba tuyo. Este script corre en el
 * navegador del usuario, sobre la pagina que esta mirando.
 *
 * No captura la pagina entera: abre un panel con lo que encontro y el usuario
 * tilda cuales son competidores de verdad. Dos publicaciones con titulos
 * parecidos pueden ser productos distintos, y eso se ve en la foto, no en el
 * texto. Por eso el panel muestra la imagen de cada una y deja elegir a que
 * publicacion propia pertenece el grupo antes de guardar.
 */
(function () {
  var CFG = window.__CEREBRO_CFG__ || {};
  var BASE = CFG.base, ALIAS = CFG.alias, TOKEN = CFG.token || '';
  var ID = 'cerebro-panel';

  var viejo = document.getElementById(ID);
  if (viejo) viejo.remove();

  function el(tag, css, txt) {
    var e = document.createElement(tag);
    if (css) e.style.cssText = css;
    if (txt != null) e.textContent = txt;
    return e;
  }
  function txt(n) { return (n && n.textContent || '').replace(/\s+/g, ' ').trim(); }

  if (!/mercadolibre\.com\.ar/.test(location.hostname)) {
    alert('Abrí una búsqueda de MercadoLibre y volvé a hacer clic.');
    return;
  }

  // ── Leer las tarjetas de resultado ────────────────────────────────────────
  function precio(c) {
    var f = c.querySelector('[class*="money-amount__fraction"]');
    if (!f) return 0;
    var n = parseFloat(txt(f).replace(/[^\d]/g, ''));
    return isNaN(n) ? 0 : n;
  }

  function leer() {
    var cards = document.querySelectorAll('li[class*="ui-search-layout__item"]');
    if (!cards.length) cards = document.querySelectorAll('[class*="poly-card--grid"], [class*="poly-card--list"]');
    var vistos = {}, filas = [];
    for (var i = 0; i < cards.length; i++) {
      var c = cards[i];
      var aT = c.querySelector('a[class*="poly-component__title"]') || c.querySelector('a[href]');
      if (!aT) continue;
      var href = aT.getAttribute('href') || '';
      var mItem = href.match(/\/(MLA-?\d{9,})/);
      var aF = c.querySelector('a[href*="/p/MLA"]');
      var mF = (aF && (aF.getAttribute('href') || '').match(/\/p\/(MLA\d+)/)) || href.match(/\/p\/(MLA\d+)/);
      var id = mItem ? mItem[1].replace('-', '') : null;
      var cpid = mF ? mF[1] : null;
      var esFicha = !id;
      if (!id && cpid) id = cpid;
      if (!id || vistos[id]) continue;
      vistos[id] = true;
      var todo = txt(c), img = c.querySelector('img');
      var sold = todo.match(/([\d.]+)\s*vendidos?/i);
      var stock = todo.match(/[ÚU]ltim[ao]s?\s*\d*[^.|]{0,30}/i);
      filas.push({
        id: id, es_ficha: esFicha, catalog_product_id: cpid,
        title: txt(c.querySelector('[class*="poly-component__title"]')) || txt(aT),
        price: precio(c), permalink: href.split('?')[0],
        thumbnail: img ? (img.getAttribute('data-src') || img.src || '') : '',
        seller: txt(c.querySelector('[class*="poly-component__seller"]')) || '-',
        free_ship: /(env[íi]o|llega)\s+gratis/i.test(todo),
        sold_quantity: sold ? parseInt(sold[1].replace(/\./g, ''), 10) : null,
        senal_stock: stock ? stock[0].slice(0, 60) : '',
        posicion: filas.length + 1,
      });
    }
    return filas;
  }

  // ── Panel ─────────────────────────────────────────────────────────────────
  var panel = el('div', 'position:fixed;z-index:2147483647;top:0;right:0;height:100vh;' +
    'width:420px;max-width:92vw;background:#fff;box-shadow:-8px 0 32px rgba(0,0,0,.28);' +
    'display:flex;flex-direction:column;font:14px/1.5 system-ui,-apple-system,sans-serif;color:#0f172a');
  panel.id = ID;

  var head = el('div', 'padding:14px 16px;background:#0f172a;color:#fff;flex:0 0 auto');
  head.appendChild(el('div', 'font-weight:800;font-size:15px', 'Elegí tus competidores'));
  var sub = el('div', 'font-size:12px;opacity:.8;margin-top:2px', 'Leyendo la página…');
  head.appendChild(sub);
  var cerrar = el('div', 'position:absolute;top:12px;right:14px;cursor:pointer;font-size:20px;color:#fff', '×');
  cerrar.onclick = function () { panel.remove(); };
  head.style.position = 'relative';
  head.appendChild(cerrar);
  panel.appendChild(head);

  var selWrap = el('div', 'padding:10px 16px;border-bottom:1px solid #e2e8f0;flex:0 0 auto;background:#f8fafc');
  selWrap.appendChild(el('div', 'font-size:12px;font-weight:700;margin-bottom:4px',
    '¿De cuál de tus publicaciones son competencia?'));
  var sel = el('select', 'width:100%;padding:7px;border:1px solid #cbd5e1;border-radius:7px;font-size:13px');
  sel.appendChild(new Option('— el sistema elige por parecido —', ''));
  selWrap.appendChild(sel);
  panel.appendChild(selWrap);

  var lista = el('div', 'flex:1 1 auto;overflow-y:auto;padding:8px 10px');
  panel.appendChild(lista);

  var pie = el('div', 'padding:12px 16px;border-top:1px solid #e2e8f0;flex:0 0 auto;background:#f8fafc;' +
    'display:flex;gap:8px;align-items:center');
  var btnTodos = el('button', 'padding:7px 10px;border:1px solid #cbd5e1;background:#fff;' +
    'border-radius:7px;cursor:pointer;font-size:12px', 'Todos');
  var btnGuardar = el('button', 'flex:1;padding:9px 12px;border:none;background:#2563eb;color:#fff;' +
    'border-radius:7px;cursor:pointer;font-weight:700;font-size:13px', 'Guardar seleccionados');
  pie.appendChild(btnTodos); pie.appendChild(btnGuardar);
  panel.appendChild(pie);
  document.body.appendChild(panel);

  fetch(BASE + '/api/mis-publicaciones-cors/' + encodeURIComponent(ALIAS))
    .then(function (r) { return r.json(); })
    .then(function (j) {
      (j.items || []).forEach(function (it) {
        sel.appendChild(new Option(it.titulo.slice(0, 70), it.id));
      });
    }).catch(function () {});

  var filas = [], marcados = {};

  function pintar() {
    lista.innerHTML = '';
    filas.forEach(function (f, i) {
      var row = el('label', 'display:flex;gap:10px;padding:8px;border-radius:9px;cursor:pointer;' +
        'align-items:flex-start;border:1px solid ' + (marcados[i] ? '#2563eb' : '#e2e8f0') +
        ';margin-bottom:6px;background:' + (marcados[i] ? '#eff6ff' : '#fff'));
      var chk = el('input', 'margin-top:3px;flex:0 0 auto');
      chk.type = 'checkbox'; chk.checked = !!marcados[i];
      chk.onchange = function () { marcados[i] = chk.checked; pintar(); actualizar(); };
      row.appendChild(chk);
      if (f.thumbnail) {
        var im = el('img', 'width:56px;height:56px;object-fit:contain;border-radius:6px;' +
          'background:#f1f5f9;flex:0 0 auto');
        im.src = f.thumbnail;
        row.appendChild(im);
      }
      var info = el('div', 'flex:1 1 auto;min-width:0');
      info.appendChild(el('div', 'font-size:12.5px;font-weight:600;line-height:1.35', f.title.slice(0, 80)));
      var meta = '$' + (f.price || 0).toLocaleString('es-AR');
      if (f.seller && f.seller !== '-') meta += ' · ' + f.seller;
      if (f.sold_quantity) meta += ' · ' + f.sold_quantity + ' vendidos';
      if (f.es_ficha) meta += ' · catálogo';
      info.appendChild(el('div', 'font-size:11.5px;color:#64748b;margin-top:2px', meta));
      row.appendChild(info);
      lista.appendChild(row);
    });
  }

  function actualizar() {
    var n = Object.keys(marcados).filter(function (k) { return marcados[k]; }).length;
    btnGuardar.textContent = n ? 'Guardar ' + n + ' seleccionados' : 'Guardar seleccionados';
    btnGuardar.style.opacity = n ? '1' : '.5';
  }

  btnTodos.onclick = function () {
    var alguno = Object.keys(marcados).some(function (k) { return marcados[k]; });
    filas.forEach(function (_, i) { marcados[i] = !alguno; });
    pintar(); actualizar();
  };

  btnGuardar.onclick = function () {
    var elegidas = filas.filter(function (_, i) { return marcados[i]; });
    if (!elegidas.length) { sub.textContent = 'No marcaste ninguna.'; return; }
    btnGuardar.disabled = true;
    btnGuardar.textContent = 'Guardando…';
    var q = '';
    try {
      q = new URLSearchParams(location.search).get('q') || '';
      if (!q) {
        var p = location.pathname.split('/').filter(Boolean);
        q = decodeURIComponent(p[p.length - 1] || '').replace(/-/g, ' ');
      }
    } catch (e) {}

    fetch(BASE + '/api/capturar-competidor', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Cerebro-Token': TOKEN},
      body: JSON.stringify({alias: ALIAS, query: q, token: TOKEN,
                            item_propio: sel.value || '', competidores: elegidas}),
    }).then(function (r) { return r.json(); }).then(function (j) {
      btnGuardar.disabled = false;
      if (!j.ok) { sub.textContent = 'No se pudo: ' + (j.error || ''); actualizar(); return; }
      var m = j.guardados + ' guardados.';
      if (j.mis_posiciones && j.mis_posiciones.length) {
        m += ' Vos estás en el puesto ' +
          j.mis_posiciones.map(function (p) { return p.posicion; }).join(' y ') + '.';
      }
      sub.textContent = m + ' Miralos en Cerebro → Competidores.';
      btnGuardar.textContent = 'Guardado ✓';
      btnGuardar.style.background = '#16a34a';
    }).catch(function (e) {
      btnGuardar.disabled = false;
      sub.textContent = 'No pude llegar al sistema: ' + e;
      actualizar();
    });
  };

  // La grilla de ML se arma despues del HTML: si se hace clic apenas carga,
  // todavia no hay nada que leer.
  var intentos = 0;
  (function esperar() {
    filas = leer();
    if (filas.length) {
      sub.textContent = filas.length + ' publicaciones en esta página. Tildá las que te compiten.';
      pintar(); actualizar();
      return;
    }
    if (++intentos > 12) {
      sub.textContent = 'No encontré publicaciones. Esperá a que cargue la página de resultados.';
      return;
    }
    setTimeout(esperar, 700);
  })();
})();
