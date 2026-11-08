/*
 * JPEG 3000 (.j3k) : décodeur JavaScript, sans aucune dépendance.
 *
 * Donne exactement les mêmes pixels que jpeg3000.py (vérifié par test_jpeg3000.py).
 * Marche dans tous les navigateurs récents et dans Node.js.
 *
 * Navigateur :
 *   <script src="j3k.js"></script>
 *   <img data-j3k="photo.j3k" alt="...">   <!-- J3K.polyfill() remplace l'image par sa version décodée -->
 *   <script>J3K.polyfill();</script>
 *
 *   const image = J3K.decode(new Uint8Array(buffer));  // {width, height, channels, data, toRGBA()}
 *
 * Node.js : const J3K = require("./j3k.js");
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.J3K = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const MAGIC = "J3K3";
  const HEADER_SIZE = 22;
  const MAX_PIXELS = 1 << 28;
  const BIAS = 0.05;
  const BLOCK = 32;
  const TOP = 1 << 24;
  const MIX_W0 = 19660;
  const MIX_SHIFT = 11;

  // Ondelette CDF 9/7 et énergie de ses fonctions de base (mêmes nombres que jpeg3000.py).
  const ALPHA = -1.586134342059924, BETA = -0.052980118572961, GAMMA = 0.882911075530934;
  const DELTA = 0.443506852043971, K = 1.230174104914001;
  const LOW = [1.0, 1.4021081679297438, 2.030371856081801, 2.9011625562785768, 4.115285175175844,
    5.824510863772892, 8.238759934572657, 11.651954647921327, 16.47856064706484];
  const HIGH = [1.0, 0.721261382508076, 0.9834713041227895, 1.4419624041394556, 2.0737604196716712,
    2.94732487653393, 4.173589458929619, 5.904302327552499, 8.350639020782582];
  const LL = 0, HL = 1, LH = 2, HH = 3;

  const SBIN = [0, 1, 2, 3, 3, 4, 4, 5, 5, 5, 6, 6, 6, 6, 7, 7];
  const MLOG = [0, 1, 2, 3, 3, 4, 4, 5, 5, 5, 5, 6, 6, 6, 6, 6, 6];
  for (let i = 0; i < 12; i++) MLOG.push(7);
  for (let i = 0; i < 20; i++) MLOG.push(8);
  for (let i = 0; i < 15; i++) MLOG.push(9);

  // Mélange logistique : tables entières identiques à celles de jpeg3000.py.
  const SQT = [1, 2, 3, 6, 10, 16, 27, 45, 73, 120, 194, 310, 488, 747, 1101, 1546, 2047, 2549, 2994,
    3348, 3607, 3785, 3901, 3975, 4022, 4050, 4068, 4079, 4085, 4089, 4092, 4093, 4094];
  const SQUASH = new Int32Array(4095);
  for (let d = -2047; d <= 2047; d++) {
    const w = d & 127, i = (d >> 7) + 16;
    SQUASH[d + 2047] = Math.max(1, (SQT[i] * (128 - w) + SQT[i + 1] * w + 64) >> 7);
  }
  const STRETCH = new Int32Array(4096).fill(2047);
  for (let d = -2047, lo = 0; d <= 2047; d++) {
    const top = SQUASH[d + 2047];
    for (let p = lo; p <= top; p++) STRETCH[p] = d;
    lo = Math.max(lo, top + 1);
  }

  // ---------------------------------------------------------------------------
  // Décodeur arithmétique (miroir exact de la classe Decoder de jpeg3000.py)
  // ---------------------------------------------------------------------------

  class RangeDecoder {
    constructor(data, pos) {
      this.data = data;
      this.pos = pos;
      this.range = 0xFFFFFFFF;
      this.val = 0;
      for (let i = 0; i < 4; i++) this.val = this.val * 256 + this.byte();
    }

    byte() {
      const p = this.pos++;
      return p < this.data.length ? this.data[p] : 0;
    }

    norm() {
      while (this.range < TOP) {
        this.range *= 256;
        this.val = this.val * 256 + this.byte();
      }
    }

    bit(probs, i) {
      i += i;
      const pf = probs[i], ps = probs[i + 1];
      const bound = (this.range >>> 12) * ((pf + ps) >> 5);
      let b;
      if (this.val < bound) {
        this.range = bound;
        probs[i] = pf + ((65536 - pf) >> 4);
        probs[i + 1] = ps + ((65536 - ps) >> 7);
        b = 0;
      } else {
        this.val -= bound;
        this.range -= bound;
        probs[i] = pf - (pf >> 4);
        probs[i + 1] = ps - (ps >> 7);
        b = 1;
      }
      this.norm();
      return b;
    }

    code(p0) {
      const bound = (this.range >>> 12) * p0;
      let b;
      if (this.val < bound) {
        this.range = bound;
        b = 0;
      } else {
        this.val -= bound;
        this.range -= bound;
        b = 1;
      }
      this.norm();
      return b;
    }

    bypass() {
      this.range = this.range >>> 1;
      let b = 0;
      if (this.val >= this.range) {
        this.val -= this.range;
        b = 1;
      }
      this.norm();
      return b;
    }

    ue(probs, base) {
      let n = 0;
      while (this.bit(probs, base + (n < 19 ? n : 19))) {
        if (++n > 40) throw new Error("corrupted .j3k file (impossible value)");
      }
      let v = 1;
      for (let k = 0; k < n; k++) v = v * 2 + this.bypass();
      return v - 1;
    }
  }

  // ---------------------------------------------------------------------------
  // Modèles de contexte
  // ---------------------------------------------------------------------------

  const probs = (n) => new Int32Array(2 * n).fill(32768);

  function bandModel() {
    const mw = new Float64Array(40);
    for (let i = 0; i < 40; i++) mw[i] = i % 5 === 4 ? 0 : MIX_W0;
    return {
      blk: probs(18), gt1: probs(20), gt2: probs(20), eg: probs(400), sgn: probs(27),
      sfa: new Int32Array(48).fill(32768), ssa: new Int32Array(48).fill(32768),
      smb: new Int32Array(162).fill(32768), smc: new Int32Array(108).fill(32768), mw,
    };
  }

  function planeModels() {
    const bands = [];
    for (let i = 0; i < 8; i++) bands.push(bandModel());
    return { dc: probs(60), bands };
  }

  function codeLL(rc, pr, R, h, w) {
    for (let y = 0, i = 0; y < h; y++) {
      for (let x = 0; x < w; x++, i++) {
        let pred, k;
        if (y && x) {
          const a = R[i - 1], b = R[i - w], c = R[i - w - 1];
          const lo = a < b ? a : b, hi = a < b ? b : a;
          pred = c >= hi ? lo : c <= lo ? hi : a + b - c;
          const act = Math.abs(a - c) + Math.abs(b - c);
          k = act < 4 ? 0 : act < 32 ? 1 : 2;
        } else if (x) {
          pred = R[i - 1]; k = 2;
        } else if (y) {
          pred = R[i - w]; k = 2;
        } else {
          pred = 0; k = 2;
        }
        const u = rc.ue(pr, 20 * k);
        R[i] = pred + (u % 2 === 0 ? u / 2 : -(u + 1) / 2);
      }
    }
  }

  function blockAny(m, h, w) {
    const nbw = Math.ceil(w / BLOCK), out = new Uint8Array(Math.ceil(h / BLOCK) * nbw);
    for (let y = 0, i = 0; y < h; y++) {
      const row = ((y / BLOCK) | 0) * nbw;
      for (let x = 0; x < w; x++, i++) if (m[i]) out[row + ((x / BLOCK) | 0)] = 1;
    }
    return out;
  }

  // Bande haute fréquence : miroir exact de _code_band() dans jpeg3000.py.
  function codeBand(rc, md, band, P, C, X) {
    const h = band.h, w = band.w, R = band.d, n = h * w;
    if (!h || !w) return;
    let pv = null, xv = null;
    if (P && P.h && P.w) {
      pv = new Int32Array(n);
      for (let y = 0, i = 0; y < h; y++) {
        const py = Math.min(y >> 1, P.h - 1) * P.w;
        for (let x = 0; x < w; x++, i++) pv[i] = P.d[py + Math.min(x >> 1, P.w - 1)];
      }
    }
    const sameScale = (b, target, add) => {
      for (let y = 0, i = 0; y < h; y++) {
        const by = Math.min(y, b.h - 1) * b.w;
        for (let x = 0; x < w; x++, i++) {
          const v = b.d[by + Math.min(x, b.w - 1)];
          if (add) target[i] += Math.abs(v); else target[i] = v;
        }
      }
    };
    if (X) {
      xv = new Int32Array(n);
      sameScale(X, xv, false);
    }
    const cv = new Int32Array(n);
    for (const c of C) if (c.h && c.w) sameScale(c, cv, true);
    const pb = pv ? blockAny(pv, h, w) : null;
    const xb = xv ? blockAny(xv, h, w) : null;
    const pm = new Int32Array(n), xm = new Int32Array(n), cm = new Int32Array(n);
    const xs = xv ? new Int32Array(n) : null;
    for (let i = 0; i < n; i++) {
      if (pv) pm[i] = Math.min(Math.abs(pv[i]), 2);
      if (xv) {
        xm[i] = Math.min(Math.abs(xv[i]), 2);
        xs[i] = Math.sign(xv[i]) + 1;
      }
      cm[i] = Math.min(cv[i], 2);
    }
    const { blk, gt1, gt2, eg, sgn, sfa, ssa, smb, smc, mw } = md;
    const nbh = Math.ceil(h / BLOCK), nbw = Math.ceil(w / BLOCK);
    const flags = new Uint8Array(nbh * nbw);
    for (let by = 0; by < nbh; by++) {
      const y0 = by * BLOCK, y1 = Math.min(y0 + BLOCK, h);
      for (let bx = 0; bx < nbw; bx++) {
        const x0 = bx * BLOCK, x1 = Math.min(x0 + BLOCK, w), bi = by * nbw + bx;
        // a) Le bloc contient-il au moins un coefficient non nul ?
        const c = (by ? flags[bi - nbw] : 0) + (bx ? flags[bi - 1] : 0);
        const pc = pb ? pb[bi] : 2, lb = xb ? xb[bi] : 0;
        if (!(flags[bi] = rc.bit(blk, 2 * (3 * c + pc) + lb))) continue;
        // b) Les coefficients du bloc, un par un.
        for (let y = y0; y < y1; y++) {
          for (let x = x0; x < x1; x++) {
            const i = y * w + x;
            const l1 = x ? Math.abs(R[i - 1]) : 0;
            let u1 = 0, dg = 0;
            if (y) {
              u1 = Math.abs(R[i - w]);
              if (x) dg = Math.abs(R[i - w - 1]);
              if (x + 1 < w) dg += Math.abs(R[i - w + 1]);
            }
            const sn = 2 * (l1 + u1) + dg;
            const p = pm[i];
            const lc = xs && xs[i] !== 1 ? 1 : 0;
            // Nul ou pas ? Mélange de 4 prédictions.
            const sb = SBIN[sn < 15 ? sn : 15];
            const ca = 2 * (3 * sb + p) + lc;
            const cb = ((((l1 < 2 ? l1 : 2) * 3 + (u1 < 2 ? u1 : 2)) * 3 + (dg < 2 ? dg : 2)) * 3 + p) * 2 + lc;
            const fr = (x > 1 ? Math.abs(R[i - 2]) : 0) + (y > 1 ? Math.abs(R[i - 2 * w]) : 0);
            const cc = (((fr < 3 ? fr : 3) * 3 + cm[i]) * 3 + xm[i]) * 3 + p;
            const pa = sfa[ca], ps = ssa[ca], pbb = smb[cb], pcc = smc[cc];
            const s0 = STRETCH[pa >> 4], s1 = STRETCH[ps >> 4], s2 = STRETCH[pbb >> 4], s3 = STRETCH[pcc >> 4];
            const k = 5 * sb;
            const dot = Math.floor((mw[k] * s0 + mw[k + 1] * s1 + mw[k + 2] * s2 + mw[k + 3] * s3 + mw[k + 4] * 256) / 65536);
            const p1 = dot > -2047 ? SQUASH[(dot < 2047 ? dot : 2047) + 2047] : 1;
            const b = rc.code(4096 - p1);
            const err = b * 4096 - p1;
            mw[k] += (s0 * err) >> MIX_SHIFT;
            mw[k + 1] += (s1 * err) >> MIX_SHIFT;
            mw[k + 2] += (s2 * err) >> MIX_SHIFT;
            mw[k + 3] += (s3 * err) >> MIX_SHIFT;
            mw[k + 4] += (256 * err) >> MIX_SHIFT;
            if (!b) {
              sfa[ca] = pa - (pa >> 4);
              ssa[ca] = ps - (ps >> 7);
              smb[cb] = pbb - (pbb >> 5);
              smc[cc] = pcc - (pcc >> 5);
              continue;
            }
            sfa[ca] = pa + ((65536 - pa) >> 4);
            ssa[ca] = ps + ((65536 - ps) >> 7);
            smb[cb] = pbb + ((65536 - pbb) >> 5);
            smc[cc] = pcc + ((65536 - pcc) >> 5);
            // Amplitude.
            let mc = sn + 2 * p;
            mc = 2 * MLOG[mc < 64 ? mc : 63] + lc;
            let m = 1;
            if (rc.bit(gt1, mc)) m = rc.bit(gt2, mc) ? 3 + rc.ue(eg, 20 * mc) : 2;
            // Signe.
            const sl = x ? R[i - 1] : 0, su = y ? R[i - w] : 0;
            let sc = 3 * (Math.sign(sl) + 1) + Math.sign(su) + 1;
            if (xs) sc = 3 * sc + xs[i];
            R[i] = rc.bit(sgn, sc) ? -m : m;
          }
        }
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Ondelette inverse, couleurs
  // ---------------------------------------------------------------------------

  function levelSizes(h, w, levels) {
    const sizes = [[h, w]];
    for (let i = 0; i < levels; i++) {
      h = (h + 1) >> 1;
      w = (w + 1) >> 1;
      sizes.push([h, w]);
    }
    return sizes;
  }

  function bandSpecs(h, w, levels) {
    const sizes = levelSizes(h, w, levels);
    const [lh0, lw0] = sizes[levels];
    const specs = [{ lev: levels, o: LL, y0: 0, y1: lh0, x0: 0, x1: lw0, norm: LOW[levels] * LOW[levels] }];
    for (let lev = levels; lev >= 1; lev--) {
      const [bh, bw] = sizes[lev - 1], [lh, lw] = sizes[lev];
      specs.push({ lev, o: HL, y0: 0, y1: lh, x0: lw, x1: bw, norm: HIGH[lev] * LOW[lev] });
      specs.push({ lev, o: LH, y0: lh, y1: bh, x0: 0, x1: lw, norm: LOW[lev] * HIGH[lev] });
      specs.push({ lev, o: HH, y0: lh, y1: bh, x0: lw, x1: bw, norm: HIGH[lev] * HIGH[lev] });
    }
    return specs;
  }

  // Une étape de synthèse : x[0..ns) = basses fréquences, x[ns..ns+nd) = hautes -> out.
  function inv1d(x, ns, nd, out) {
    if (!nd) {
      for (let k = 0; k < ns; k++) out[k] = x[k];
      return;
    }
    const s = new Float64Array(ns), d = new Float64Array(nd);
    for (let k = 0; k < ns; k++) s[k] = x[k] * K;
    for (let k = 0; k < nd; k++) d[k] = x[ns + k] / K;
    const dPrev = (k) => d[k ? k - 1 : 0], dCur = (k) => d[k < nd ? k : nd - 1], sNext = (k) => s[k + 1 < ns ? k + 1 : ns - 1];
    for (let k = 0; k < ns; k++) s[k] = s[k] - DELTA * (dPrev(k) + dCur(k));
    for (let k = 0; k < nd; k++) d[k] = d[k] - GAMMA * (s[k] + sNext(k));
    for (let k = 0; k < ns; k++) s[k] = s[k] - BETA * (dPrev(k) + dCur(k));
    for (let k = 0; k < nd; k++) d[k] = d[k] - ALPHA * (s[k] + sNext(k));
    for (let k = 0; k < ns; k++) out[2 * k] = s[k];
    for (let k = 0; k < nd; k++) out[2 * k + 1] = d[k];
  }

  function idwt2(a, H, W, levels) {
    const sizes = levelSizes(H, W, levels);
    const buf = new Float64Array(Math.max(H, W)), out = new Float64Array(Math.max(H, W));
    for (let lev = levels; lev >= 1; lev--) {
      const [h, w] = sizes[lev - 1], [lh, lw] = sizes[lev];
      for (let x = 0; x < w; x++) {  // colonnes d'abord...
        for (let y = 0; y < h; y++) buf[y] = a[y * W + x];
        inv1d(buf, lh, h - lh, out);
        for (let y = 0; y < h; y++) a[y * W + x] = out[y];
      }
      for (let y = 0; y < h; y++) {  // ... puis lignes
        const row = y * W;
        for (let x = 0; x < w; x++) buf[x] = a[row + x];
        inv1d(buf, lw, w - lw, out);
        for (let x = 0; x < w; x++) a[row + x] = out[x];
      }
    }
  }

  // Arrondi « au pair le plus proche » comme numpy.round, puis 0..255.
  function toByte(v) {
    let r = Math.floor(v);
    const f = v - r;
    if (f > 0.5 || (f === 0.5 && r % 2 !== 0)) r += 1;
    return r < 0 ? 0 : r > 255 ? 255 : r;
  }

  // ---------------------------------------------------------------------------
  // API
  // ---------------------------------------------------------------------------

  function readHeader(bytes) {
    if (bytes.length < HEADER_SIZE || String.fromCharCode(bytes[0], bytes[1], bytes[2], bytes[3]) !== MAGIC) {
      if (bytes.length >= 4 && String.fromCharCode(bytes[0], bytes[1], bytes[2]) === "J3K") {
        throw new Error("JPEG 3000 file from another version (J3K" + String.fromCharCode(bytes[3]) +
          "): this decoder reads " + MAGIC);
      }
      throw new Error("not a JPEG 3000 (.j3k) file");
    }
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const hd = {
      width: dv.getUint32(4, true), height: dv.getUint32(8, true), channels: bytes[12], levels: bytes[13],
      step: dv.getFloat32(14, true), chroma: dv.getFloat32(18, true),
    };
    if (!(hd.channels >= 1 && hd.channels <= 4 && hd.levels >= 1 && hd.levels <= 8 && hd.width && hd.height &&
      hd.width * hd.height <= MAX_PIXELS && hd.step > 0 && isFinite(hd.step) && hd.chroma > 0 && isFinite(hd.chroma))) {
      throw new Error("corrupted .j3k file (invalid header)");
    }
    return hd;
  }

  function decode(bytes) {
    if (!(bytes instanceof Uint8Array)) bytes = new Uint8Array(bytes);
    const hd = readHeader(bytes);
    const { width: w, height: h, channels: nch, levels, step, chroma } = hd;
    const ncolor = nch <= 2 ? 1 : 3, n = w * h;
    const rc = new RangeDecoder(bytes, HEADER_SIZE);
    const specs = bandSpecs(h, w, levels);
    const luma = planeModels(), chromaModels = planeModels();
    const planesBands = [];
    for (let ci = 0; ci < ncolor; ci++) {
      const md = ci ? chromaModels : luma, parents = {}, bands = [];
      specs.forEach((sp, bi) => {
        const bh = sp.y1 - sp.y0, bw = sp.x1 - sp.x0;
        const band = { d: new Int32Array(bh * bw), h: bh, w: bw };
        if (sp.o === LL) {
          codeLL(rc, md.dc, band.d, bh, bw);
        } else {
          const C = [];
          for (let k = 1; k < sp.o; k++) C.push(bands[bi - k]);
          codeBand(rc, md.bands[2 * (Math.min(sp.lev, 4) - 1) + (sp.o === HH ? 1 : 0)], band,
            parents[sp.o] || null, C, ci ? planesBands[0][bi] : null);
          parents[sp.o] = band;
        }
        bands.push(band);
      });
      planesBands.push(bands);
    }
    let alpha = null;
    if (nch === 2 || nch === 4) {
      alpha = new Int32Array(n);
      codeLL(rc, probs(60), alpha, h, w);
    }
    // Reconstruction : déquantification puis ondelette inverse, plan par plan.
    const planes = planesBands.map((bands, ci) => {
      const st = step * (ci ? chroma : 1.0), a = new Float64Array(n);
      specs.forEach((sp, bi) => {
        const s = st / sp.norm, q = bands[bi].d;
        for (let y = sp.y0, j = 0; y < sp.y1; y++) {
          for (let x = sp.x0; x < sp.x1; x++, j++) {
            const v = q[j];
            a[y * w + x] = sp.o === LL ? v * s : Math.sign(v) * (Math.abs(v) + BIAS) * s;
          }
        }
      });
      idwt2(a, h, w, levels);
      return a;
    });
    const data = new Uint8Array(n * nch);
    for (let i = 0; i < n; i++) {
      let o = i * nch;
      if (ncolor === 1) {
        data[o++] = toByte(planes[0][i] + 128.0);
      } else {
        const y = planes[0][i] + 128.0, cb = planes[1][i], cr = planes[2][i];
        data[o++] = toByte(y + 1.402 * cr);
        data[o++] = toByte(y - 0.344136 * cb - 0.714136 * cr);
        data[o++] = toByte(y + 1.772 * cb);
      }
      if (alpha) data[o] = alpha[i] < 0 ? 0 : alpha[i] > 255 ? 255 : alpha[i];
    }
    return {
      width: w, height: h, channels: nch, data, header: hd,
      /** Pixels RGBA pour un <canvas> (ImageData). */
      toRGBA() {
        const out = new Uint8ClampedArray(n * 4);
        for (let i = 0; i < n; i++) {
          const o = i * nch;
          if (ncolor === 1) out[4 * i] = out[4 * i + 1] = out[4 * i + 2] = data[o];
          else {
            out[4 * i] = data[o];
            out[4 * i + 1] = data[o + 1];
            out[4 * i + 2] = data[o + 2];
          }
          out[4 * i + 3] = alpha ? data[o + nch - 1] : 255;
        }
        return out;
      },
    };
  }

  /** Décode un .j3k en image PNG (Blob) dans le navigateur. */
  async function toBlob(bytes, type) {
    const im = decode(bytes);
    const canvas = document.createElement("canvas");
    canvas.width = im.width;
    canvas.height = im.height;
    canvas.getContext("2d").putImageData(new ImageData(im.toRGBA(), im.width, im.height), 0, 0);
    return new Promise((resolve) => canvas.toBlob(resolve, type || "image/png"));
  }

  /** Affiche les <img data-j3k="..."> (et <img src="....j3k">) de la page. */
  function polyfill(doc) {
    doc = doc || document;
    const imgs = doc.querySelectorAll('img[data-j3k], img[src$=".j3k"]');
    return Promise.all(Array.from(imgs).map(async (img) => {
      const url = img.getAttribute("data-j3k") || img.getAttribute("src");
      try {
        const res = await fetch(url);
        if (!res.ok) throw new Error(res.status + " " + res.statusText);
        const blob = await toBlob(new Uint8Array(await res.arrayBuffer()));
        img.removeAttribute("data-j3k");
        img.src = URL.createObjectURL(blob);
      } catch (e) {
        console.error("J3K: cannot display " + url, e);
      }
    }));
  }

  return { decode, readHeader, toBlob, polyfill, MAGIC };
});
