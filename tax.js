// Sales tax worksheets for the Bayou Bill dashboard.
//
// Works on data.json's tax.orders (one row per Etsy order: date, ship-to state,
// item value, discount, shipping, tax Etsy collected, total, refunds).
// Sales = items - discount + shipping, less refunds; a refund also backs out
// its share of the tax. Loaded by index.html as window.BayouTax and by the
// tests in tests/tax.test.cjs.
(function (root) {
  const round2 = n => Math.round((n + Number.EPSILON) * 100) / 100;
  const norm = s => String(s || '').toLowerCase().replace(/[^a-z]/g, '');

  function isLouisiana(o) {
    const c = norm(o.country);
    const domestic = !c || c === 'us' || c === 'usa' || c === 'unitedstates';
    const st = norm(o.state);
    return domestic && (st === 'la' || st === 'louisiana');
  }

  function enrich(o) {
    const gross = round2((o.items || 0) - (o.discount || 0) + (o.shipping || 0));
    const share = o.total > 0 ? Math.min(o.refunds || 0, o.total) / o.total : 0;
    const net = round2(gross * (1 - share));
    const taxNet = round2((o.tax || 0) * (1 - share));
    const la = isLouisiana(o);
    const cat = !la ? 'oos' : ((o.tax || 0) > 0 ? 'mf' : 'notax');
    return {...o, gross, net, taxNet, la, cat};
  }

  const inRange = (orders, from, to) =>
    orders.filter(o => o.date >= from && o.date <= to).map(enrich);
  const sum = (list, f) => round2(list.reduce((a, o) => a + f(o), 0));

  // Louisiana return worksheet (lines 1-7) for one filing period.
  function worksheet(orders, from, to, stateRate, localRate) {
    const list = inRange(orders, from, to);
    const total = sum(list, o => o.net);
    const outOfState = sum(list.filter(o => !o.la), o => o.net);
    const laSales = round2(total - outOfState);
    const mf = sum(list.filter(o => o.cat === 'mf'), o => o.net);
    const noTax = round2(laSales - mf);
    return {
      list, total, outOfState, laSales, mf, noTax, stateRate, localRate,
      stateDue: round2(noTax * stateRate),
      localDue: localRate == null ? null : round2(noTax * localRate),
      etsyLaTax: sum(list.filter(o => o.la), o => o.taxNet),
      refunds: sum(list, o => o.gross - o.net),
      noTaxCount: list.filter(o => o.cat === 'notax' && o.net > 0).length,
    };
  }

  // Sales, shipping and Etsy-collected tax by ship-to state.
  function byState(orders, from, to) {
    const rows = new Map();
    for (const o of inRange(orders, from, to)) {
      const key = (o.state || '').toUpperCase() || '(unknown)';
      const r = rows.get(key) || {state: key, orders: 0, sales: 0, shipping: 0, tax: 0};
      r.orders += 1; r.sales += o.net; r.shipping += o.shipping || 0; r.tax += o.taxNet;
      rows.set(key, r);
    }
    return [...rows.values()]
      .map(r => ({...r, sales: round2(r.sales), shipping: round2(r.shipping), tax: round2(r.tax)}))
      .sort((a, b) => b.sales - a.sales || a.state.localeCompare(b.state));
  }

  // Louisiana orders by city / ZIP, to work out which parish each belongs to.
  function laPlaces(orders, from, to) {
    const rows = new Map();
    for (const o of inRange(orders, from, to).filter(o => o.la)) {
      const key = `${o.city || ''}|${o.zip || ''}`;
      const r = rows.get(key) || {city: o.city || '(blank)', zip: o.zip || '', orders: 0, sales: 0, tax: 0};
      r.orders += 1; r.sales += o.net; r.tax += o.taxNet;
      rows.set(key, r);
    }
    return [...rows.values()]
      .map(r => ({...r, sales: round2(r.sales), tax: round2(r.tax)}))
      .sort((a, b) => b.sales - a.sales);
  }

  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                  'August', 'September', 'October', 'November', 'December'];
  const lastDay = (y, m) => new Date(Date.UTC(y, m, 0)).getUTCDate();   // m is 1-based
  const pad = n => String(n).padStart(2, '0');

  // Filing periods that contain orders: months, quarters, years, then all time.
  function periods(orders) {
    const months = new Set(), quarters = new Set(), years = new Set();
    for (const o of orders) {
      if (!o.date) continue;
      const y = +o.date.slice(0, 4), m = +o.date.slice(5, 7);
      months.add(`${y}-${pad(m)}`);
      quarters.add(`${y}-Q${Math.ceil(m / 3)}`);
      years.add(String(y));
    }
    const desc = s => [...s].sort().reverse();
    const dates = orders.map(o => o.date).filter(Boolean).sort();
    return [
      ...desc(months).map(k => {
        const y = +k.slice(0, 4), m = +k.slice(5);
        return {key: k, kind: 'month', label: `${MONTHS[m - 1]} ${y}`,
                from: `${k}-01`, to: `${k}-${pad(lastDay(y, m))}`};
      }),
      ...desc(quarters).map(k => {
        const y = +k.slice(0, 4), q = +k.slice(-1), m1 = q * 3 - 2, m3 = q * 3;
        return {key: k, kind: 'quarter', label: `Q${q} ${y}`,
                from: `${y}-${pad(m1)}-01`, to: `${y}-${pad(m3)}-${pad(lastDay(y, m3))}`};
      }),
      ...desc(years).map(k => ({key: k, kind: 'year', label: k, from: `${k}-01-01`, to: `${k}-12-31`})),
      ...(dates.length ? [{key: 'all', kind: 'all', label: 'All time', from: dates[0], to: dates[dates.length - 1]}] : []),
    ];
  }

  function toCSV(rows) {
    const cell = v => {
      const s = v == null ? '' : String(v);
      return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    return rows.map(r => r.map(cell).join(',')).join('\r\n') + '\r\n';
  }

  const api = {round2, isLouisiana, enrich, worksheet, byState, laPlaces, periods, toCSV};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.BayouTax = api;
})(this);
