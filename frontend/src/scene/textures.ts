import { CanvasTexture, NoColorSpace, RepeatWrapping, SRGBColorSpace } from 'three';
import type { KeySpec } from './geometry';
import { KEYBOARD } from './geometry';

// All textures are generated at runtime from code: no image files, fonts or downloaded assets.

function context(width: number, height: number) {
  const canvas = document.createElement('canvas');
  canvas.width = width; canvas.height = height;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('Canvas 2D is unavailable for procedural textures');
  return { canvas, ctx };
}

/** Deterministic PRNG so the generated grain is identical on every launch. */
function random(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Tileable value noise (periodic lattice), returned as a 0..1 field. */
function periodicNoise(size: number, cells: number, rand: () => number) {
  const lattice = Float32Array.from({ length: cells * cells }, rand);
  const field = new Float32Array(size * size);
  const fade = (t: number) => t * t * (3 - 2 * t);
  for (let y = 0; y < size; y++) {
    const gy = (y / size) * cells, y0 = Math.floor(gy), ty = fade(gy - y0), y1 = (y0 + 1) % cells;
    for (let x = 0; x < size; x++) {
      const gx = (x / size) * cells, x0 = Math.floor(gx), tx = fade(gx - x0), x1 = (x0 + 1) % cells;
      const a = lattice[y0 * cells + x0], b = lattice[y0 * cells + x1], c = lattice[y1 * cells + x0], d = lattice[y1 * cells + x1];
      field[y * size + x] = (a + (b - a) * tx) * (1 - ty) + (c + (d - c) * tx) * ty;
    }
  }
  return field;
}

function grayTexture(size: number, value: (i: number) => number, colorSpace = NoColorSpace) {
  const { canvas, ctx } = context(size, size);
  const image = ctx.createImageData(size, size);
  for (let i = 0; i < size * size; i++) {
    const v = Math.max(0, Math.min(255, Math.round(value(i))));
    image.data[i * 4] = v; image.data[i * 4 + 1] = v; image.data[i * 4 + 2] = v; image.data[i * 4 + 3] = 255;
  }
  ctx.putImageData(image, 0, 0);
  const texture = new CanvasTexture(canvas);
  texture.wrapS = texture.wrapT = RepeatWrapping;
  texture.colorSpace = colorSpace;
  texture.anisotropy = 8;
  return texture;
}

/** Matte desk grain: fine speckle plus faint low-frequency variation (roughness + bump). */
export function createDeskTexture() {
  const size = 512, rand = random(733);
  const low = periodicNoise(size, 6, rand), mid = periodicNoise(size, 48, rand), fine = periodicNoise(size, 256, rand);
  return grayTexture(size, i => 222 + (low[i] - 0.5) * 12 + (mid[i] - 0.5) * 8 + (fine[i] - 0.5) * 26 + (rand() - 0.5) * 16);
}

/** Woven sports-mesh style bump for cushions and the suspension strap. */
export function createFabricTexture() {
  const size = 256, rand = random(1733), threads = 48;
  const grain = periodicNoise(size, 64, rand);
  return grayTexture(size, i => {
    const x = i % size, y = Math.floor(i / size);
    const u = (x / size) * threads * Math.PI * 2, v = (y / size) * threads * Math.PI * 2;
    const over = (Math.floor((x / size) * threads) + Math.floor((y / size) * threads)) % 2 === 0;
    const weave = over ? Math.abs(Math.sin(u / 2)) : Math.abs(Math.sin(v / 2));
    return 70 + weave * 130 + (grain[i] - 0.5) * 50;
  });
}

/** Soft radial falloff for additive glow sprites and decals (white on black). */
export function createGlowTexture() {
  const { canvas, ctx } = context(128, 128);
  const gradient = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
  gradient.addColorStop(0, 'rgba(255,255,255,1)');
  gradient.addColorStop(0.22, 'rgba(255,255,255,0.55)');
  gradient.addColorStop(0.55, 'rgba(255,255,255,0.14)');
  gradient.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 128, 128);
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  return texture;
}

/** Blurred footprint used as an alpha map for soft contact occlusion under a device. */
export function createContactTexture(width: number, depth: number, blur: number, radius: number) {
  const scale = 220 / Math.max(width, depth);
  const pad = blur * 2.2;
  const w = Math.ceil((width + pad * 2) * scale), h = Math.ceil((depth + pad * 2) * scale);
  const { canvas, ctx } = context(w, h);
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, w, h);
  const shift = w * 3;
  ctx.shadowColor = '#fff';
  ctx.shadowBlur = blur * scale;
  ctx.shadowOffsetX = shift;
  ctx.fillStyle = '#fff';
  ctx.beginPath();
  ctx.roundRect(pad * scale - shift, pad * scale, width * scale, depth * scale, radius * scale);
  ctx.fill();
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = NoColorSpace;
  return { texture, width: width + pad * 2, depth: depth + pad * 2 };
}

function legendFont(px: number, weight = 500) {
  return `${weight} ${px}px "Segoe UI", "SF Pro Text", "Helvetica Neue", Arial, sans-serif`;
}

/** Dotted pattern of the elastic suspension strap (light specks on black). */
export function createStrapTexture() {
  const size = 256, rand = random(9733);
  const { canvas, ctx } = context(size, size);
  ctx.fillStyle = '#121214';
  ctx.fillRect(0, 0, size, size);
  for (let row = 0; row < 32; row++) {
    for (let col = 0; col < 32; col++) {
      if (rand() < 0.35) continue;
      const x = (col + (row % 2) * 0.5) * 8 + (rand() - 0.5) * 2, y = row * 8 + (rand() - 0.5) * 2;
      ctx.fillStyle = `rgba(150, 154, 160, ${0.45 + rand() * 0.45})`;
      ctx.beginPath();
      ctx.arc(x, y, 0.9 + rand() * 1.1, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  const texture = new CanvasTexture(canvas);
  texture.wrapS = texture.wrapT = RepeatWrapping;
  texture.colorSpace = SRGBColorSpace;
  texture.anisotropy = 8;
  return texture;
}

type Ctx = CanvasRenderingContext2D;

/** Simple line icons in a 24 x 24 box centred on the origin (original drawings, no trademarked artwork). */
function drawIcon(ctx: Ctx, name: string, cx: number, cy: number, size: number) {
  ctx.save();
  ctx.translate(cx, cy);
  ctx.scale(size / 24, size / 24);
  ctx.lineWidth = 2;
  const line = (x0: number, y0: number, x1: number, y1: number) => { ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke(); };
  const speaker = () => {
    ctx.beginPath();
    ctx.moveTo(-10, -3.5); ctx.lineTo(-6, -3.5); ctx.lineTo(-1, -8); ctx.lineTo(-1, 8); ctx.lineTo(-6, 3.5); ctx.lineTo(-10, 3.5);
    ctx.closePath(); ctx.fill();
  };
  const mic = () => {
    ctx.beginPath(); ctx.roundRect(-3, -10, 6, 12, 3); ctx.stroke();
    ctx.beginPath(); ctx.arc(0, -1, 6.5, 0.1 * Math.PI, 0.9 * Math.PI); ctx.stroke();
    line(0, 5.5, 0, 9);
  };
  switch (name) {
    case 'lock':
      ctx.beginPath(); ctx.roundRect(-6, -1, 12, 10, 2); ctx.fill();
      ctx.lineWidth = 2.4; ctx.beginPath(); ctx.arc(0, -1, 4.2, Math.PI, 0); ctx.stroke();
      break;
    case 'laptop':
      ctx.beginPath(); ctx.roundRect(-8, -7, 16, 11, 1.5); ctx.stroke();
      ctx.lineWidth = 2.6; line(-11, 7, 11, 7);
      break;
    case 'backlight-down': case 'backlight-up': {
      ctx.beginPath(); ctx.roundRect(-9, 1, 18, 8, 1.5); ctx.stroke();
      const reach = name === 'backlight-up' ? 6 : 2.5;
      for (const a of [-0.75, -0.25, 0.25, 0.75]) {
        const angle = -Math.PI / 2 + a * 0.9;
        line(Math.cos(angle) * 5, -1 + Math.sin(angle) * 3, Math.cos(angle) * (5 + reach), -1 + Math.sin(angle) * (3 + reach));
      }
      break;
    }
    case 'dictation': mic(); line(7, -9, 9, -11); line(8.5, -5, 11, -5); break;
    case 'emoji':
      ctx.beginPath(); ctx.arc(0, 0, 9.5, 0, Math.PI * 2); ctx.stroke();
      for (const x of [-3.4, 3.4]) { ctx.beginPath(); ctx.arc(x, -2.5, 1.4, 0, Math.PI * 2); ctx.fill(); }
      ctx.beginPath(); ctx.arc(0, 1, 5, 0.18 * Math.PI, 0.82 * Math.PI); ctx.stroke();
      break;
    case 'screenshot':
      ctx.beginPath(); ctx.roundRect(-10, -5, 20, 14, 2); ctx.stroke();
      ctx.beginPath(); ctx.arc(0, 2, 3.8, 0, Math.PI * 2); ctx.stroke();
      ctx.beginPath(); ctx.roundRect(-4, -9, 8, 4, 1); ctx.fill();
      break;
    case 'mic-mute': mic(); ctx.lineWidth = 2.2; line(-9, -10, 9, 10); break;
    case 'play-pause':
      ctx.beginPath(); ctx.moveTo(-10, -7); ctx.lineTo(-10, 7); ctx.lineTo(0, 0); ctx.closePath(); ctx.fill();
      ctx.fillRect(3, -7, 2.8, 14); ctx.fillRect(7.6, -7, 2.8, 14);
      break;
    case 'mute': speaker(); line(3, -4, 10, 4); line(10, -4, 3, 4); break;
    case 'volume-down': speaker(); ctx.beginPath(); ctx.arc(0, 0, 5.5, -0.3 * Math.PI, 0.3 * Math.PI); ctx.stroke(); break;
    case 'volume-up':
      speaker();
      for (const r of [5.5, 10]) { ctx.beginPath(); ctx.arc(0, 0, r, -0.3 * Math.PI, 0.3 * Math.PI); ctx.stroke(); }
      break;
    case '↑': case '↓': case '←': case '→': {
      const turns: Record<string, number> = { '↑': 0, '→': 0.5, '↓': 1, '←': 1.5 };
      ctx.rotate(turns[name] * Math.PI);
      ctx.beginPath(); ctx.moveTo(0, -6); ctx.lineTo(6.5, 4); ctx.lineTo(-6.5, 4); ctx.closePath(); ctx.fill();
      break;
    }
    default: break;
  }
  ctx.restore();
}

/** Draws every legend of the TR-Q layout onto one canvas (planar keyboard UV: u = x, v = z). */
function drawLegends(keys: KeySpec[], width: number, background: string, ink: string, led: string) {
  const height = Math.round((width * KEYBOARD.depth) / KEYBOARD.width), px = width / KEYBOARD.width;
  const { canvas, ctx } = context(width, height);
  ctx.fillStyle = background;
  ctx.fillRect(0, 0, width, height);
  const X = (x: number) => (x + KEYBOARD.width / 2) * px, Y = (z: number) => (z + KEYBOARD.depth / 2) * px;
  ctx.fillStyle = ink; ctx.strokeStyle = ink; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  const text = (value: string, x: number, z: number, size: number, align: CanvasTextAlign = 'center', weight = 400) => {
    ctx.font = legendFont(Math.round(size * px), weight);
    ctx.textAlign = align;
    ctx.textBaseline = 'middle';
    ctx.fillText(value, X(x), Y(z));
  };
  const icon = (name: string, x: number, z: number, size: number) => drawIcon(ctx, name, X(x), Y(z), size * px);
  for (const key of keys) {
    const { x, z, width: w, depth: d } = key;
    const right = x + w / 2, left = x - w / 2, bottom = z + d / 2, top = z - d / 2;
    switch (key.style) {
      case 'letter':
        text(key.label, x, z - 0.004, 0.05);
        if (key.alt) text(key.alt, right - 0.024, bottom - 0.028, 0.03, 'right');
        if (key.label === 'F' || key.label === 'J') {
          ctx.lineWidth = 0.006 * px;
          ctx.beginPath(); ctx.moveTo(X(x - 0.016), Y(z + 0.044)); ctx.lineTo(X(x + 0.016), Y(z + 0.044)); ctx.stroke();
        }
        break;
      case 'dual':
        if (key.shift) text(key.shift, x, z - 0.034, 0.04);
        text(key.label, key.alt ? x - 0.008 : x, key.shift ? z + 0.03 : z, 0.043);
        if (key.alt) text(key.alt, right - 0.022, z + 0.032, 0.029, 'right');
        break;
      case 'function':
        if (key.icon === 'switch') {
          text(key.label, x - 0.032, z + 0.002, 0.034);
          icon('laptop', x + 0.012, z, 0.052);
          ctx.fillStyle = led;
          ctx.beginPath(); ctx.arc(X(left + 0.02), Y(top + 0.018), 0.0055 * px, 0, Math.PI * 2); ctx.fill();
          ctx.fillStyle = ink;
        } else if (key.icon) icon(key.icon, x, z - 0.006, 0.056);
        if (key.sub) text(key.sub, right - 0.016, bottom - 0.018, 0.019, 'right', 500);
        break;
      case 'mod':
        if (key.label === 'esc' || key.label === 'del') { text(key.label, x - 0.012, z - 0.014, 0.033); icon('lock', right - 0.03, bottom - 0.027, 0.03); }
        else if (key.label === 'enter') text(key.label, x, top + 0.078, 0.033);
        else if (key.label === 'backspace' || key.label === 'ctrl' || key.label === 'fn') text(key.label, x, z, 0.033);
        else if (key.label === 'shift' && x > 0) text(key.label, right - 0.032, z, 0.033, 'right');
        else text(key.label, left + 0.03, z, 0.033, 'left');
        break;
      case 'split':
        ctx.lineWidth = 0.0035 * px;
        ctx.beginPath(); ctx.moveTo(X(x), Y(top + 0.03)); ctx.lineTo(X(x), Y(bottom - 0.03)); ctx.stroke();
        if (key.glyph) text(key.glyph, x - w / 4, z - 0.03, 0.03);
        text(key.label, x - w / 4, key.glyph ? z + 0.014 : z, 0.027);
        if (key.second?.includes(' ')) key.second.split(' ').forEach((part, i) => text(part, x + w / 4, z - 0.016 + i * 0.034, 0.027));
        else text(key.second ?? '', x + w / 4, z, 0.027);
        break;
      case 'arrow': icon(key.label, x, z, 0.034); break;
      default: break;
    }
  }
  return canvas;
}

/**
 * TR-Q keycap legends mapped planar over the deck (u = x, v = z). `color` is the cap albedo with printed
 * white legends; `glow` is the backlight mask used as an emissive map (Easy-Switch LEDs stay unlit).
 */
export function createLegendTextures(keys: KeySpec[]) {
  const make = (canvas: HTMLCanvasElement) => {
    const texture = new CanvasTexture(canvas);
    texture.colorSpace = SRGBColorSpace;
    texture.anisotropy = 8;
    return texture;
  };
  return {
    color: make(drawLegends(keys, 3072, '#2b2d31', '#e4e6ea', '#55585e')),
    glow: make(drawLegends(keys, 2048, '#000000', '#ffffff', '#000000')),
  };
}

