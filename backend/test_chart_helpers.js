// Self-check for the price chart's pure helpers in frontend/index.html
// (fixedLows, lowCandle, measureLabel, fmtPrice). Run: node backend/test_chart_helpers.js
const fs = require("fs"), path = require("path"), assert = require("assert");
const html = fs.readFileSync(path.join(__dirname, "..", "frontend", "index.html"), "utf8");
const grab = name => html.match(new RegExp(`  function ${name}\\([\\s\\S]*?\\r?\\n  \\}\\r?\\n`))[0];
const { fixedLows, lowCandle, measureLabel, fmtPrice } = new Function(
  ["fmtPrice", "fixedLows", "lowCandle", "measureLabel"].map(grab).join("\n") +
  "return {fixedLows, lowCandle, measureLabel, fmtPrice};")();

const DAY = 86400, T0 = Date.UTC(2026, 6, 1) / 1000;          // 2026-07-01 00:00 UTC
// 90 daily candles: 60d low on day 40 ($0.50), 14d low on day 80 ($0.80), last close $1.00
const daily = Array.from({length: 90}, (_, i) => {
  const low = i === 40 ? 0.5 : i === 80 ? 0.8 : 0.9;
  return {time: T0 + i * DAY, open: 1, high: 1.1, low, close: 1};
});
const fx = fixedLows(daily);
assert.deepStrictEqual(fx.lows.map(l => [l.label, l.time, l.price]),
  [["14d low", T0 + 80 * DAY, 0.8], ["60d low", T0 + 40 * DAY, 0.5]]);
assert.strictEqual(fx.now, 1);
assert.deepStrictEqual(fixedLows(daily.slice(-30)).lows.map(l => l.label), ["14d low"]);   // < 60 days: no 60d low
assert.deepStrictEqual(fixedLows([]), {now: null, lows: []});

// 4H candles over the whole range, 15m candles over the last 10 days only
const h4 = [], m15 = [];
for (let t = T0; t < T0 + 90 * DAY; t += 4 * 3600) {
  const d = Math.floor((t - T0) / DAY), hour = (t - T0) % DAY / 3600;
  const low = (d === 40 && hour === 8) ? 0.5 : (d === 80 && hour === 16) ? 0.8 : 0.95;
  h4.push({time: t, open: 1, high: 1.1, low, close: 1});
}
for (let t = T0 + 80 * DAY; t < T0 + 90 * DAY; t += 900)
  m15.push({time: t, open: 1, high: 1.1, low: t === T0 + 80 * DAY + 16 * 3600 + 900 ? 0.8 : 0.97, close: 1});

// The lows are FIXED (same price + day) - only the candle they are drawn on depends on the timeframe
for (const [name, candles] of [["1D", daily], ["4H", h4], ["15m", m15]]) {
  const placed = fx.lows.map(l => lowCandle(candles, l.time));
  if (name === "1D") assert.deepStrictEqual(placed.map(c => c.time), [T0 + 80 * DAY, T0 + 40 * DAY]);
  if (name === "4H") assert.deepStrictEqual(placed.map(c => c.time), [T0 + 80 * DAY + 16 * 3600, T0 + 40 * DAY + 8 * 3600]);
  if (name === "15m") {
    assert.strictEqual(placed[0].time, T0 + 80 * DAY + 16 * 3600 + 900);                        // the exact 15-minute candle
    assert.strictEqual(placed[1], null);                                                        // 60d low before the 15m range
  }
}
assert.deepStrictEqual(fx.lows.map(l => l.price), [0.8, 0.5]);                               // unchanged after all switches

// Measure: % change, price change, bars, time span
const a = {time: T0, price: 0.0001}, b = {time: T0 + 34 * DAY + 10 * 3600, price: 0.0001123};
const cs = Array.from({length: 40}, (_, i) => ({time: i === 34 ? b.time : T0 + i * DAY}));
assert.strictEqual(measureLabel(a, b, cs), "+12.30% · +$0.0000123 · 34 bars · 34d 10h");
assert.strictEqual(measureLabel(b, a, cs), "−10.95% · −$0.0000123 · 34 bars · 34d 10h");
assert.strictEqual(measureLabel({time: T0, price: 2}, {time: T0 + 5400, price: 2}, [{time: T0}, {time: T0 + 5400}]),
  "+0.00% · +$0 · 1 bar · 1h 30m");
console.log("chart helpers self-check: ok");
