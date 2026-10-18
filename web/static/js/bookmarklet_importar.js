/* Importar a ml-system — boton de favoritos (separado del de competidores).
 *
 * ML bloqueo leer publicaciones ajenas por API (CEREBRO correccion 59): este
 * script lee la pagina de producto que el usuario esta mirando y la manda a
 * ml-system. No usa CORS ni token: abre una ventana del sistema (con la sesion
 * del usuario) y le pasa los datos con postMessage, solo a ese origen.
 *
 * Lee: titulo, precio, caracteristicas, fotos (solo como referencia) y si la
 * pagina es de catalogo. NO lee la descripcion: no se copia.
 */
(function () {
  var CFG = window.__MLSYS_IMPORTAR__ || {};
  var BASE = CFG.base, ALIAS = CFG.alias;
  function t(s) { return (s || '').replace(/\s+/g, ' ').trim(); }

  if (!/(^|\.)mercadolibre\.com\.ar$/.test(location.hostname)) {
    alert('Abrí la página de un producto de MercadoLibre y volvé a tocar el botón.');
    return;
  }
  var h1 = document.querySelector('h1.ui-pdp-title') || document.querySelector('h1');
  var titulo = t(h1 && h1.textContent);
  var precioMeta = document.querySelector('meta[itemprop="price"]');
  if (!titulo || !precioMeta) {
    alert('Esta no parece la página de un producto. Entrá a una publicación y volvé a tocar el botón.');
    return;
  }
  var specs = [];
  document.querySelectorAll('.andes-table__row').forEach(function (r) {
    var n = t(r.querySelector('th') && r.querySelector('th').textContent);
    var v = t(r.querySelector('td') && r.querySelector('td').textContent);
    if (n && v) specs.push([n, v]);
  });
  var fotos = [];
  document.querySelectorAll('.ui-pdp-gallery img, figure img').forEach(function (i) {
    var u = i.getAttribute('data-zoom') || i.getAttribute('data-src') || i.src || '';
    if (u.indexOf('https://http2.mlstatic.com/') === 0 && fotos.indexOf(u) < 0) fotos.push(u);
  });
  var m = document.documentElement.innerHTML.match(/"category_id":"(MLA\d+)"/);
  var datos = {
    url: location.href.split('#')[0], titulo: titulo, precio: precioMeta.content,
    specs: specs, fotos: fotos.slice(0, 20), categoria_pagina: m ? m[1] : null
  };

  var win = window.open(BASE + '/importar/recibir?alias=' + encodeURIComponent(ALIAS), 'mlsys_importar',
                        'width=560,height=620');
  if (!win) { alert('El navegador bloqueó la ventana. Permití ventanas emergentes para mercadolibre.com.ar.'); return; }
  function onMsg(ev) {
    if (ev.origin !== BASE || ev.source !== win || !ev.data || ev.data.tipo !== 'mlsys-importar-listo') return;
    win.postMessage({tipo: 'mlsys-importar-datos', datos: datos}, BASE);
    window.removeEventListener('message', onMsg);
  }
  window.addEventListener('message', onMsg);
})();
