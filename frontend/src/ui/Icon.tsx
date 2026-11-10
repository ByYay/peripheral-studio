import type { CSSProperties } from 'react';

export type IconName = 'grid' | 'keyboard' | 'headset' | 'mouse' | 'battery' | 'sparkles' | 'sliders' | 'keys' | 'macro' | 'profiles' | 'plus' | 'check' | 'arrow' | 'trash' | 'refresh' | 'bolt' | 'clock' | 'edit';
const paths: Record<IconName, string> = {
  grid: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  keyboard: 'M3 6h18v12H3z M6 9h.01 M9 9h.01 M12 9h.01 M15 9h.01 M18 9h.01 M6 12h.01 M9 12h.01 M12 12h.01 M15 12h.01 M18 12h.01 M7 15h10',
  headset: 'M4 14v-3a8 8 0 0 1 16 0v3 M4 12H2v7h5v-7z M20 12h2v7h-5v-7z',
  mouse: 'M12 3a7 7 0 0 0-7 7v4a7 7 0 0 0 14 0v-4a7 7 0 0 0-7-7z M12 3v7',
  battery: 'M2 7h17v10H2z M22 10v4 M5 10v4 M8 10v4 M11 10v4',
  sparkles: 'm12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z',
  sliders: 'M4 4v16 M12 4v16 M20 4v16 M1 8h6 M9 16h6 M17 10h6',
  keys: 'M3 5h7v7H3z M14 5h7v7h-7z M8 16h13 M17 13l4 3-4 3',
  macro: 'm9 4 11 8-11 8z M4 4v16',
  profiles: 'M3 5h7l2 3h9v12H3z',
  plus: 'M12 5v14 M5 12h14',
  check: 'm5 12 4 4L19 6',
  arrow: 'M5 12h14 m-5-5 5 5-5 5',
  trash: 'M3 6h18 M9 6V3h6v3 M6 6l1 15h10l1-15 M10 10v7 M14 10v7',
  refresh: 'M20 7a9 9 0 1 0 1 9 M20 2v6h-6',
  bolt: 'M13 2 4 14h7l-1 8 9-12h-7z',
  clock: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M12 7v5l3 2',
  edit: 'M4 20h4L19 9l-4-4L4 16z M13 7l4 4',
};

export function Icon({ name, size = 20, style }: { name: IconName; size?: number; style?: CSSProperties }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={style}><path d={paths[name]} /></svg>;
}
