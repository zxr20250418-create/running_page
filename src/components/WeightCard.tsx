import { useEffect, useMemo, useState } from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { useLocale } from '../hooks/useLocale';
import weightUrl from '@/static/weight.json?url';

// Written by the RunSync iOS app from Apple Health: one entry per day (daily average).
interface WeightEntry {
  date: string; // YYYY-MM-DD, local time
  kg: number;
}

const RANGES = [
  { key: '3m', days: 90, zh: '3个月', en: '3M' },
  { key: '1y', days: 365, zh: '1年', en: '1Y' },
  { key: 'all', days: Infinity, zh: '全部', en: 'All' },
] as const;
type RangeKey = (typeof RANGES)[number]['key'];

const DAY = 86400000;

// Entries within `days` of the latest one (entries sorted ascending).
const inRange = (entries: WeightEntry[], days: number) => {
  if (!entries.length || days === Infinity) return entries;
  const last = Date.parse(entries[entries.length - 1].date);
  return entries.filter((e) => last - Date.parse(e.date) < days * DAY);
};

export function WeightCard() {
  const { locale } = useLocale();
  const zh = locale === 'zh';
  const [entries, setEntries] = useState<WeightEntry[]>([]);
  const [range, setRange] = useState<RangeKey>('3m');

  useEffect(() => {
    const controller = new AbortController();
    fetch(weightUrl, { signal: controller.signal })
      .then((r) => (r.ok ? r.json() : []))
      .then((data: WeightEntry[]) => {
        const sorted = [...data].sort((a, b) => a.date.localeCompare(b.date));
        setEntries(sorted);
        // Sparse history: start on the shortest range that still draws a line
        setRange(
          RANGES.find((r) => inRange(sorted, r.days).length >= 2)?.key ?? 'all'
        );
      })
      .catch(() => {});
    return () => controller.abort();
  }, []);

  const shown = useMemo(
    () => inRange(entries, RANGES.find((r) => r.key === range)!.days),
    [entries, range]
  );

  if (!entries.length) return null;

  const latest = entries[entries.length - 1];
  const change = shown.length > 1 ? latest.kg - shown[0].kg : 0;
  const kgs = shown.map((e) => e.kg);
  // Time axis so gaps between weigh-ins keep their real length
  const points = shown.map((e) => ({ ...e, t: Date.parse(e.date) }));
  const multiYear =
    points.length > 1 && points[points.length - 1].t - points[0].t > 330 * DAY;
  const isoDate = (t: number) => new Date(t).toISOString().slice(0, 10);
  const pad = 0.5;
  const domain = [
    Math.floor(Math.min(...kgs) - pad),
    Math.ceil(Math.max(...kgs) + pad),
  ];

  return (
    <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] px-4 py-3 hover:border-[var(--color-accent)]/30 hover:bg-[var(--color-accent)]/5 hover:shadow-[var(--color-accent)]/5 hover:shadow-lg">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <svg
            className="h-4 w-4 text-[var(--color-accent)]"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M3 6h18M6 6l-3 8a3 3 0 006 0L6 6zm12 0l-3 8a3 3 0 006 0l-3-8zM12 3v18m-4 0h8"
            />
          </svg>
          {zh ? '体重' : 'Weight'}
        </h3>
        <div className="flex gap-1">
          {RANGES.map((r) => (
            <button
              key={r.key}
              type="button"
              onClick={() => setRange(r.key)}
              className={`rounded-md px-2 py-0.5 text-xs ${
                range === r.key
                  ? 'bg-[var(--color-accent)] text-[var(--color-on-accent)]'
                  : 'text-[var(--color-muted)] hover:bg-[var(--color-bg)]'
              }`}
            >
              {zh ? r.zh : r.en}
            </button>
          ))}
        </div>
      </div>

      <div className="flex items-baseline gap-2">
        <span className="font-mono text-2xl font-bold">
          {latest.kg.toFixed(1)}
        </span>
        <span className="text-xs text-[var(--color-muted)]">kg</span>
        {shown.length > 1 && (
          <span
            className={`ml-auto font-mono text-xs font-bold ${
              change <= 0
                ? 'text-[var(--color-accent)]'
                : 'text-[var(--color-muted)]'
            }`}
          >
            {change > 0 ? '+' : ''}
            {change.toFixed(1)} kg
          </span>
        )}
      </div>
      <p className="text-xs text-[var(--color-muted)]">
        {zh ? '最近记录' : 'Last weighed'} · {latest.date}
      </p>

      <div className="mt-2 h-40">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            data={points}
            margin={{ top: 8, right: 4, bottom: 0, left: -24 }}
          >
            <CartesianGrid
              vertical={false}
              stroke="var(--color-border)"
              strokeDasharray="3 3"
            />
            <XAxis
              dataKey="t"
              type="number"
              scale="time"
              domain={['dataMin', 'dataMax']}
              tick={{ fill: 'var(--color-muted)', fontSize: 11 }}
              tickFormatter={(t: number) =>
                multiYear ? isoDate(t).slice(0, 7) : isoDate(t).slice(5)
              }
              axisLine={false}
              tickLine={false}
              minTickGap={24}
            />
            <YAxis
              domain={domain}
              tick={{ fill: 'var(--color-muted)', fontSize: 11 }}
              axisLine={false}
              tickLine={false}
            />
            <Tooltip
              contentStyle={{
                background: 'var(--color-card)',
                borderColor: 'var(--color-border)',
                borderRadius: 8,
                color: 'var(--color-text)',
              }}
              labelFormatter={(t) => isoDate(Number(t))}
              formatter={(v) => [
                `${Number(v).toFixed(1)} kg`,
                zh ? '体重' : 'Weight',
              ]}
            />
            <Line
              type="linear"
              dataKey="kg"
              stroke="var(--color-accent)"
              strokeWidth={2}
              dot={shown.length <= 60}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
