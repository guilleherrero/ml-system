/* Enviar a Cerebro — captura una pagina de resultados de MercadoLibre.
 *
 * ML bloqueo el acceso programatico a /sites/MLA/search, asi que el sistema ya
 * no puede ver quien aparece en una busqueda. Este script corre en el navegador
 * del usuario, con su sesion real, sobre la pagina que el usuario esta mirando:
 * es la unica via que queda.
 *
 * Lee la pagina de forma defensiva. ML cambia los nombres de clase seguido, asi
 * que en vez de depender de una clase concreta se busca por estructura —un link
 * a /MLA-123..., y alrededor el titulo, el precio y las señales de envio y
 * stock—. Si ML cambia el maquetado, se degrada de a poco en vez de romperse.
 */
(function () {
  var CFG = window.__CEREBRO_CFG__ || {};
  var BASE = CFG.base, ALIAS = CFG.alias, TOKEN = CFG.token || '';

  function aviso(texto, color) {
    var d = document.getElementById('cerebro-aviso') || document.createElement('div');
    d.id = 'cerebro-aviso';
    d.style.cssText = 'position:fixed;z-index:2147483647;top:16px;right:16px;' +
      'max-width:340px;padding:14px 16px;border-radius:10px;font:14px/1.5 system-ui,sans-serif;' +
      'color:#fff;box-shadow:0 8px 24px rgba(0,0,0,.25);background:' + (color || '#1e293b');
    d.textContent = texto;
    document.body.appendChild(d);
    return d;
  }

  if (!/mercadolibre\.com\.ar/.test(location.hostname)) {
    aviso('Esto se usa sobre una búsqueda de MercadoLibre. Buscá el producto como lo buscaría un comprador y volvé a hacer clic.', '#b45309');
    setTimeout(function () { var e = document.getElementById('cerebro-aviso'); if (e) e.remove(); }, 6000);
    return;
  }

  var cargando = aviso('Leyendo la página…');

  function texto(el) { return (el && el.textContent || '').replace(/\s+/g, ' ').trim(); }

  function precioDe(scope) {
    var f = scope.querySelector('[class*="money-amount__fraction"]');
    if (!f) return 0;
    var ent = texto(f).replace(/[^\d]/g, '');
    var cent = scope.querySelector('[class*="money-amount__cents"]');
    var n = parseFloat(ent + (cent ? '.' + texto(cent).replace(/[^\d]/g, '') : ''));
    return isNaN(n) ? 0 : n;
  }

  // Cada resultado es el ancestro comun mas chico que contiene el link al item
  function contenedor(a) {
    var n = a, saltos = 0;
    while (n && saltos < 8) {
      if (n.matches && n.matches('li, [class*="ui-search-layout__item"], [class*="poly-card"], [class*="ui-search-result"]')) return n;
      n = n.parentElement; saltos++;
    }
    return a.parentElement || a;
  }

  var vistos = {}, filas = [];
  var links = document.querySelectorAll('a[href*="/MLA-"], a[href*="/p/MLA"]');
  for (var i = 0; i < links.length; i++) {
    var href = links[i].getAttribute('href') || '';
    var m = href.match(/MLA-?(\d{6,})/);
    if (!m) continue;
    var id = 'MLA' + m[1];
    if (vistos[id]) continue;

    var box = contenedor(links[i]);
    var t = texto(box.querySelector('[class*="poly-component__title"], h2, h3')) || texto(links[i]);
    if (!t || t.length < 8) continue;
    vistos[id] = true;

    var todo = texto(box);
    var img = box.querySelector('img');
    var vend = box.querySelector('[class*="seller"], [class*="brand"]');
    var vendidos = todo.match(/(\d+)\s*vendidos?/i);
    var stock = todo.match(/[ÚU]ltim[ao]s?\s*(\d+)?[^.|]*/i);

    filas.push({
      id: id,
      title: t.slice(0, 200),
      price: precioDe(box),
      permalink: href.split('#')[0],
      thumbnail: img ? (img.getAttribute('data-src') || img.src || '') : '',
      seller: texto(vend) || '-',
      free_ship: /env[íi]o gratis/i.test(todo),
      sold_quantity: vendidos ? parseInt(vendidos[1], 10) : null,
      senal_stock: stock ? stock[0].slice(0, 60) : '',
      posicion: filas.length + 1,
    });
  }

  if (!filas.length) {
    cargando.remove();
    aviso('No encontré publicaciones en esta página. Tiene que ser una página de resultados de búsqueda.', '#b45309');
    setTimeout(function () { var e = document.getElementById('cerebro-aviso'); if (e) e.remove(); }, 6000);
    return;
  }

  var q = '';
  try {
    q = new URLSearchParams(location.search).get('q') || '';
    if (!q) {
      var partes = location.pathname.split('/').filter(Boolean);
      q = decodeURIComponent(partes[partes.length - 1] || '').replace(/-/g, ' ');
    }
  } catch (e) { q = ''; }

  cargando.textContent = 'Enviando ' + filas.length + ' publicaciones a Cerebro…';

  fetch(BASE + '/api/capturar-competidor', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Cerebro-Token': TOKEN},
    body: JSON.stringify({alias: ALIAS, query: q, token: TOKEN, competidores: filas}),
  })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      cargando.remove();
      if (!j.ok) { aviso('Cerebro no lo aceptó: ' + (j.error || ''), '#b91c1c'); return; }
      var msg = j.guardados + ' competidores capturados de "' + (j.query || 'esta búsqueda') + '".';
      if (j.mis_posiciones && j.mis_posiciones.length) {
        msg += ' Vos aparecés en el puesto ' +
          j.mis_posiciones.map(function (p) { return p.posicion; }).join(' y ') + '.';
      } else {
        msg += ' No apareciste en esta página.';
      }
      msg += ' Confirmá cuáles son directos en Cerebro → Competidores.';
      var d = aviso(msg, '#166534');
      setTimeout(function () { d.remove(); }, 9000);
    })
    .catch(function (e) {
      cargando.remove();
      aviso('No pude llegar al sistema: ' + e, '#b91c1c');
    });
})();
