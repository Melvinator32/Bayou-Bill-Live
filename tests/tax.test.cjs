// Tests for tax.js (sales tax worksheets). Run from the repo root:
//   node --test tests/tax.test.cjs
// Every expected value is worked out by hand from the three orders below.
const test = require('node:test');
const assert = require('node:assert/strict');
const T = require('../tax.js');

const ORDERS = [
  // Louisiana, Etsy collected tax: gross 26 + 2 = 28
  {date: '2026-10-19', state: 'LA', country: 'US', city: 'Baton Rouge', zip: '70808',
   items: 26, discount: 0, shipping: 2, tax: 2.5, total: 30.5, refunds: 0},
  // Texas: gross 37 + 3 = 40
  {date: '2026-10-01', state: 'TX', country: 'US', items: 37, discount: 0, shipping: 3, tax: 0, total: 40, refunds: 0},
  // Louisiana, no tax: gross 25 - 2 = 23, refund 5 of 23 -> net 18
  {date: '2026-10-10', state: 'LA', country: 'US', city: 'New Orleans', zip: '70115',
   items: 25, discount: 2, shipping: 0, tax: 0, total: 23, refunds: 5},
  // September, outside the October period
  {date: '2026-09-30', state: 'LA', country: 'US', city: 'Metairie', zip: '70001',
   items: 10, discount: 0, shipping: 0, tax: 1, total: 11, refunds: 0},
];
const OCT = ['2026-10-01', '2026-10-31'];

test('classifies orders and nets out refunds', () => {
  const [a, b, c] = ORDERS.map(T.enrich);
  assert.deepEqual([a.cat, b.cat, c.cat], ['mf', 'oos', 'notax']);
  assert.equal(a.net, 28);
  assert.equal(c.gross, 23);
  assert.equal(c.net, 18);
});

test('treats a missing country as domestic and spelled-out states as Louisiana', () => {
  assert.equal(T.isLouisiana({state: 'Louisiana', country: ''}), true);
  assert.equal(T.isLouisiana({state: 'LA', country: 'CA'}), false);   // shipped abroad
  assert.equal(T.isLouisiana({state: 'TX', country: 'US'}), false);
});

test('Louisiana worksheet lines for October', () => {
  const r = T.worksheet(ORDERS, ...OCT, 0.05, 0.0445);
  assert.equal(r.total, 86);        // 28 + 40 + 18
  assert.equal(r.outOfState, 40);
  assert.equal(r.laSales, 46);
  assert.equal(r.mf, 28);
  assert.equal(r.noTax, 18);
  assert.equal(r.stateDue, 0.9);    // 18 x 5%
  assert.equal(r.localDue, 0.8);    // 18 x 4.45% = 0.801
  assert.equal(r.etsyLaTax, 2.5);
  assert.equal(r.refunds, 5);
  assert.equal(r.noTaxCount, 1);
  assert.equal(r.list.length, 3);
});

test('parish tax stays blank until a rate is entered', () => {
  assert.equal(T.worksheet(ORDERS, ...OCT, 0.05, null).localDue, null);
});

test('all-states summary for October', () => {
  assert.deepEqual(T.byState(ORDERS, ...OCT), [
    {state: 'LA', orders: 2, sales: 46, shipping: 2, tax: 2.5},
    {state: 'TX', orders: 1, sales: 40, shipping: 3, tax: 0},
  ]);
});

test('Louisiana places for parish lookup', () => {
  assert.deepEqual(T.laPlaces(ORDERS, ...OCT), [
    {city: 'New Orleans', zip: '70115', orders: 1, sales: 18, tax: 0},
    {city: 'Baton Rouge', zip: '70808', orders: 1, sales: 28, tax: 2.5},
  ].sort((x, y) => y.sales - x.sales));
});

test('filing periods: months, quarters and years, newest first', () => {
  const p = T.periods(ORDERS);
  const keys = p.map(x => x.key);
  assert.deepEqual(keys, ['2026-10', '2026-09', '2026-Q4', '2026-Q3', '2026', 'all']);
  const q4 = p.find(x => x.key === '2026-Q4');
  assert.deepEqual([q4.from, q4.to, q4.label], ['2026-10-01', '2026-12-31', 'Q4 2026']);
  assert.equal(p[0].label, 'October 2026');
});

test('CSV quotes commas, quotes and newlines', () => {
  assert.equal(T.toCSV([['a', 'b,c', 'say "hi"', 'x\ny', 1.5]]),
               'a,"b,c","say ""hi""","x\ny",1.5\r\n');
});

test('all-time period spans the first to the last order', () => {
  const all = T.periods(ORDERS).find(x => x.key === 'all');
  assert.deepEqual([all.kind, all.label, all.from, all.to], ['all', 'All time', '2026-09-30', '2026-10-19']);
  const r = T.worksheet(ORDERS, all.from, all.to, 0.05, null);
  assert.equal(r.list.length, 4);
  assert.equal(r.total, 96);        // October's 86 + September's 10 (tax isn't sales)
  assert.equal(r.mf, 38);           // 28 + 10
});
