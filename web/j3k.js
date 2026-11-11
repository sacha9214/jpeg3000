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

  const MAGIC = "J3K5";
  const OLD_MAGICS = ["J3K3", "J3K4"];  // versions encore lues (J3K3 : sans ondelette directionnelle ; J3K4 : v4)
  const HEADER_SIZE = 22;
  const MAX_PIXELS = 1 << 28;
  const BIAS = 0.05;  // anciens formats
  const BLOCK = 32;
  const TOP = 1 << 24;
  const MIX_W0 = 19660;
  const MIX_SHIFT = 11;
  const LEVEL1_STEP = 1.1;
  // Quantification dépendante (J3K5) : deux grilles décalées, choisies par une machine à 4 états.
  const DQ_SCALE = 0.6, DQ_BIAS = 0.28;
  const DQ_NEXT = [0, 2, 2, 0, 1, 3, 3, 1];  // état suivant = DQ_NEXT[2 * état + parité de la valeur]
  const DQ_QS = [0, 0, 1, 2];                // groupe de contextes de chaque état
  // Ondelette directionnelle (depuis J3K4) : décalage latéral de chaque direction, blocs de 16.
  const OFFS = [-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0];
  const ZERO = 3;
  const DIR_BLOCK = 16;

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

  function mixWeights(sets) {
    const w = new Float64Array(5 * sets);
    for (let i = 0; i < w.length; i++) w[i] = i % 5 === 4 ? 0 : MIX_W0;
    return w;
  }

  // dq : contextes de la quantification dépendante (J3K5), qui dépendent aussi de l'état.
  function bandModel(dq, prior) {
    if (prior) {
      const m = { gt1: probs(20) };
      for (const name in prior) m[name] = prior[name].slice();
      return m;
    }
    const k = dq ? 2 : 1, flat = (n) => new Int32Array(n).fill(32768);
    return {
      blk: probs(18), gt1: probs(20), gt2: probs(20), eg: probs(400), sgn: probs(27),
      sfa: flat(dq ? 144 : 48), ssa: flat(dq ? 144 : 48), smb: flat(162 * k), smc: flat(108 * k), mw: mixWeights(8 * k),
      gfa: flat(20 * k), gsa: flat(20 * k), gmb: flat(384 * k), gmc: flat(108 * k), gw: mixWeights(10 * k),
    };
  }

  function planeModels(dq, prior) {
    const bands = [];
    for (let i = 0; i < 8; i++) bands.push(bandModel(dq, prior ? prior.bands[i] : null));
    return { dc: prior ? prior.dc.slice() : probs(60), bands };
  }

  // Table PRIORS -> probabilités de départ du format J3K5 (miroir de _priors() dans jpeg3000.py).
  // Un caractère par contexte (64 niveaux dans le domaine logistique), « . » + un caractère = une
  // série de contextes neutres ; les poids des mélangeurs prennent deux caractères.
  const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  let priorCache = null;

  function priors() {
    if (priorCache) return priorCache;
    const idx = {}, vals = [];
    for (let i = 0; i < 64; i++) idx[B64[i]] = i;
    for (let i = 0; i < PRIORS.length; i++) {
      if (PRIORS[i] === ".") {
        for (let n = idx[PRIORS[++i]] + 2; n > 0; n--) vals.push(32);
      } else {
        vals.push(idx[PRIORS[i]]);
      }
    }
    let pos = 0;
    const take = (n, pair) => {
      const out = new Int32Array(pair ? 2 * n : n);
      for (let i = 0; i < n; i++) {
        const p = SQUASH[Math.max(64 * (vals[pos++] - 32), -2047) + 2047] << 4;
        if (pair) out[2 * i] = out[2 * i + 1] = p;
        else out[i] = p;
      }
      return out;
    };
    const weights = (n) => {
      const out = new Float64Array(n);
      for (let i = 0; i < n; i++, pos += 2) out[i] = (vals[pos] * 64 + vals[pos + 1] - 2048) * 64;
      return out;
    };
    const planes = [];
    for (let kind = 0; kind < 2; kind++) {
      const m = { dc: take(60, true), bands: [] };
      for (let b = 0; b < 8; b++) {
        const blk = take(18, true), gt2 = take(20, true), eg = take(400, true), sgn = take(27, true);
        const ssa = take(144), smb = take(324), smc = take(216), gsa = take(40), gmb = take(768), gmc = take(216);
        const mw = weights(80), gw = weights(100);
        m.bands.push({ blk, gt2, eg, sgn, sfa: ssa.slice(), ssa, smb, smc, mw, gfa: gsa.slice(), gsa, gmb, gmc, gw });
      }
      planes.push(m);
    }
    const dirs = take(2 * OFFS.length * 8, true);
    if (pos !== vals.length) throw new Error("inconsistent PRIORS table");
    return (priorCache = { planes, dirs });
  }

  // État de départ du décodeur : modèles de la luminosité, des couleurs, probabilités des directions.
  function startState(dq) {
    if (!dq || !PRIORS) return { luma: planeModels(dq), colors: planeModels(dq), dirs: probs(2 * OFFS.length * 8) };
    const pr = priors();
    return { luma: planeModels(true, pr.planes[0]), colors: planeModels(true, pr.planes[1]), dirs: pr.dirs.slice() };
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

  // Ce que le décodeur sait déjà avant de lire une bande : parent, cousines, luminosité.
  function bandContext(band, P, C, X) {
    const h = band.h, w = band.w, n = h * w;
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
    return { pm, xm, cm, xs, pb, xb };
  }

  // Bande haute fréquence des anciens formats (J3K3, J3K4) : miroir exact de _code_band() dans jpeg3000.py.
  function codeBand(rc, md, band, P, C, X, v4) {
    const h = band.h, w = band.w, R = band.d;
    if (!h || !w) return;
    const { pm, xm, cm, xs, pb, xb } = bandContext(band, P, C, X);
    const { blk, gt1, gt2, eg, sgn, sfa, ssa, smb, smc, mw, gfa, gsa, gmb, gmc, gw } = md;
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
            let gb1;
            if (v4) {  // « amplitude > 1 ? » : mélange de 4 modèles, comme pour « nul ? »
              const ga = mc;
              const gb = ((((l1 < 3 ? l1 : 3) * 4 + (u1 < 3 ? u1 : 3)) * 4 + (dg < 3 ? dg : 3)) * 3 + p) * 2 + lc;
              const gc = cc;
              const qa = gfa[ga], qs = gsa[ga], qb = gmb[gb], qc = gmc[gc];
              const t0 = STRETCH[qa >> 4], t1 = STRETCH[qs >> 4], t2 = STRETCH[qb >> 4], t3 = STRETCH[qc >> 4];
              const kg = 5 * (mc >> 1);
              const gdot = Math.floor((gw[kg] * t0 + gw[kg + 1] * t1 + gw[kg + 2] * t2 + gw[kg + 3] * t3 + gw[kg + 4] * 256) / 65536);
              const g1p = gdot > -2047 ? SQUASH[(gdot < 2047 ? gdot : 2047) + 2047] : 1;
              gb1 = rc.code(4096 - g1p);
              const gerr = gb1 * 4096 - g1p;
              gw[kg] += (t0 * gerr) >> MIX_SHIFT;
              gw[kg + 1] += (t1 * gerr) >> MIX_SHIFT;
              gw[kg + 2] += (t2 * gerr) >> MIX_SHIFT;
              gw[kg + 3] += (t3 * gerr) >> MIX_SHIFT;
              gw[kg + 4] += (256 * gerr) >> MIX_SHIFT;
              if (gb1) {
                gfa[ga] = qa + ((65536 - qa) >> 4);
                gsa[ga] = qs + ((65536 - qs) >> 7);
                gmb[gb] = qb + ((65536 - qb) >> 5);
                gmc[gc] = qc + ((65536 - qc) >> 5);
              } else {
                gfa[ga] = qa - (qa >> 4);
                gsa[ga] = qs - (qs >> 7);
                gmb[gb] = qb - (qb >> 5);
                gmc[gc] = qc - (qc >> 5);
              }
            } else {
              gb1 = rc.bit(gt1, mc);
            }
            let m = 1;
            if (gb1) m = rc.bit(gt2, mc) ? 3 + rc.ue(eg, 20 * mc) : 2;
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

  // Bande haute fréquence en quantification dépendante (J3K5) : miroir exact du décodage de
  // _code_band_dq() dans jpeg3000.py. band.d = valeurs lues (contexte), band.n = niveaux de reconstruction.
  function codeBandDq(rc, md, band, P, C, X) {
    const h = band.h, w = band.w, R = band.d, N = band.n;
    if (!h || !w) return;
    const { pm, xm, cm, xs, pb, xb } = bandContext(band, P, C, X);
    const { blk, gt2, eg, sgn, sfa, ssa, smb, smc, mw, gfa, gsa, gmb, gmc, gw } = md;
    const nbh = Math.ceil(h / BLOCK), nbw = Math.ceil(w / BLOCK);
    const flags = new Uint8Array(nbh * nbw);
    for (let by = 0; by < nbh; by++) {
      const y0 = by * BLOCK, y1 = Math.min(y0 + BLOCK, h);
      for (let bx = 0; bx < nbw; bx++) {
        const x0 = bx * BLOCK, x1 = Math.min(x0 + BLOCK, w), bi = by * nbw + bx;
        const c = (by ? flags[bi - nbw] : 0) + (bx ? flags[bi - 1] : 0);
        const pc = pb ? pb[bi] : 2, lb = xb ? xb[bi] : 0;
        if (!(flags[bi] = rc.bit(blk, 2 * (3 * c + pc) + lb))) continue;
        let state = 0;
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
            const qq = state >> 1;
            // Nul ou pas ? Mélange de 4 prédictions ; les contextes dépendent aussi de l'état.
            const sb = SBIN[sn < 15 ? sn : 15];
            const ca = 2 * (3 * sb + p) + lc + 48 * DQ_QS[state];
            const cb = (((((l1 < 2 ? l1 : 2) * 3 + (u1 < 2 ? u1 : 2)) * 3 + (dg < 2 ? dg : 2)) * 3 + p) * 2 + lc) * 2 + qq;
            const fr = (x > 1 ? Math.abs(R[i - 2]) : 0) + (y > 1 ? Math.abs(R[i - 2 * w]) : 0);
            const cc = ((((fr < 3 ? fr : 3) * 3 + cm[i]) * 3 + xm[i]) * 3 + p) * 2 + qq;
            const pa = sfa[ca], ps = ssa[ca], pbb = smb[cb], pcc = smc[cc];
            const s0 = STRETCH[pa >> 4], s1 = STRETCH[ps >> 4], s2 = STRETCH[pbb >> 4], s3 = STRETCH[pcc >> 4];
            const k = 5 * (sb + 8 * qq);
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
              state = DQ_NEXT[2 * state];
              continue;
            }
            sfa[ca] = pa + ((65536 - pa) >> 4);
            ssa[ca] = ps + ((65536 - ps) >> 7);
            smb[cb] = pbb + ((65536 - pbb) >> 5);
            smc[cc] = pcc + ((65536 - pcc) >> 5);
            // Amplitude.
            let mc = sn + 2 * p;
            mc = 2 * MLOG[mc < 64 ? mc : 63] + lc;
            const ga = mc + 20 * qq;
            const gb = (((((l1 < 3 ? l1 : 3) * 4 + (u1 < 3 ? u1 : 3)) * 4 + (dg < 3 ? dg : 3)) * 3 + p) * 2 + lc) * 2 + qq;
            const qa = gfa[ga], qs = gsa[ga], qb = gmb[gb], qc = gmc[cc];
            const t0 = STRETCH[qa >> 4], t1 = STRETCH[qs >> 4], t2 = STRETCH[qb >> 4], t3 = STRETCH[qc >> 4];
            const kg = 5 * ((mc >> 1) + 10 * qq);
            const gdot = Math.floor((gw[kg] * t0 + gw[kg + 1] * t1 + gw[kg + 2] * t2 + gw[kg + 3] * t3 + gw[kg + 4] * 256) / 65536);
            const g1p = gdot > -2047 ? SQUASH[(gdot < 2047 ? gdot : 2047) + 2047] : 1;
            const gb1 = rc.code(4096 - g1p);
            const gerr = gb1 * 4096 - g1p;
            gw[kg] += (t0 * gerr) >> MIX_SHIFT;
            gw[kg + 1] += (t1 * gerr) >> MIX_SHIFT;
            gw[kg + 2] += (t2 * gerr) >> MIX_SHIFT;
            gw[kg + 3] += (t3 * gerr) >> MIX_SHIFT;
            gw[kg + 4] += (256 * gerr) >> MIX_SHIFT;
            let m = 1;
            if (gb1) {
              gfa[ga] = qa + ((65536 - qa) >> 4);
              gsa[ga] = qs + ((65536 - qs) >> 7);
              gmb[gb] = qb + ((65536 - qb) >> 5);
              gmc[cc] = qc + ((65536 - qc) >> 5);
              m = rc.bit(gt2, mc) ? 3 + rc.ue(eg, 20 * mc) : 2;
            } else {
              gfa[ga] = qa - (qa >> 4);
              gsa[ga] = qs - (qs >> 7);
              gmb[gb] = qb - (qb >> 5);
              gmc[cc] = qc - (qc >> 5);
            }
            // Signe, puis niveau de reconstruction : pair ou impair selon l'état.
            const sl = x ? R[i - 1] : 0, su = y ? R[i - w] : 0;
            let sc = 3 * (Math.sign(sl) + 1) + Math.sign(su) + 1;
            if (xs) sc = 3 * sc + xs[i];
            const level = 2 * m - qq;
            if (rc.bit(sgn, sc)) {
              R[i] = -m;
              N[i] = -level;
            } else {
              R[i] = m;
              N[i] = level;
            }
            state = DQ_NEXT[2 * state + (m & 1)];
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

  // --- Ondelette directionnelle inverse (J3K4) : miroir de _inv_dir / idwt2_dir de jpeg3000.py ---

  // Colonne col de A (m lignes de n valeurs), lue à la ligne i + off (interpolation linéaire, bords répétés).
  function sh(A, n, m, i, col, off) {
    const lo = Math.floor(off), f = off - lo;
    let i0 = i + lo;
    i0 = i0 < 0 ? 0 : i0 >= m ? m - 1 : i0;
    if (f === 0) return A[i0 * n + col];
    let i1 = i + lo + 1;
    i1 = i1 < 0 ? 0 : i1 >= m ? m - 1 : i1;
    return A[i0 * n + col] * (1 - f) + A[i1 * n + col] * f;
  }

  // Synthèse le long des lignes : s (m x ns) basses fréquences, d (m x nd) hautes, dm (m x (ns+nd)) directions.
  function invDir(s, d, m, ns, nd, dm) {
    const n = ns + nd, x = new Float64Array(m * n);
    if (!nd) {
      x.set(s);
      return x;
    }
    const S = new Float64Array(m * ns), D = new Float64Array(m * nd);
    for (let i = 0; i < m * ns; i++) S[i] = s[i] * K;
    for (let i = 0; i < m * nd; i++) D[i] = d[i] / K;
    const updateS = (c) => {  // S -= c * (d[k-1] + d[k]), voisins pris selon la direction
      for (let i = 0; i < m; i++) {
        for (let k = 0; k < ns; k++) {
          const off = OFFS[dm[i * n + 2 * k]];
          const a = sh(D, nd, m, i, k ? k - 1 : 0, -off), b = sh(D, nd, m, i, k < nd ? k : nd - 1, off);
          S[i * ns + k] = S[i * ns + k] - c * (a + b);
        }
      }
    };
    const updateD = (c) => {  // D -= c * (s[k] + s[k+1])
      for (let i = 0; i < m; i++) {
        for (let k = 0; k < nd; k++) {
          const off = OFFS[dm[i * n + 2 * k + 1]];
          const a = sh(S, ns, m, i, k, -off), b = sh(S, ns, m, i, k + 1 < ns ? k + 1 : ns - 1, off);
          D[i * nd + k] = D[i * nd + k] - c * (a + b);
        }
      }
    };
    updateS(DELTA);
    updateD(GAMMA);
    updateS(BETA);
    updateD(ALPHA);
    for (let i = 0; i < m; i++) {
      for (let k = 0; k < ns; k++) x[i * n + 2 * k] = S[i * ns + k];
      for (let k = 0; k < nd; k++) x[i * n + 2 * k + 1] = D[i * nd + k];
    }
    return x;
  }

  function expandDirs(grid, m, n) {
    const out = new Int32Array(m * n);
    for (let i = 0; i < m; i++) {
      const row = ((i / DIR_BLOCK) | 0) * grid.nc;
      for (let k = 0; k < n; k++) out[i * n + k] = grid.g[row + ((k / DIR_BLOCK) | 0)];
    }
    return out;
  }

  function dirShapes(h, w, levels) {
    const grid = (m, n) => [Math.ceil(m / DIR_BLOCK), Math.ceil(n / DIR_BLOCK)];
    const shapes = [];
    levelSizes(h, w, levels).slice(0, -1).forEach(([hh, ww]) => shapes.push(grid(ww, hh), grid(hh, ww)));
    return shapes;
  }

  function codeDirs(rc, pr, shapes) {
    const out = [];
    shapes.forEach(([nr, nc], gi) => {
      const g = new Int32Array(nr * nc);
      let prev = ZERO;
      for (let i = 0; i < nr * nc; i++) {
        const base = ((gi & 1) * OFFS.length + prev) * 8;
        let node = 1;
        for (let b = 0; b < 3; b++) node = 2 * node + rc.bit(pr, base + node);
        const sym = node - 8;
        if (sym >= OFFS.length) throw new Error("corrupted .j3k file (impossible direction)");
        g[i] = prev = sym;
      }
      out.push({ g, nr, nc });
    });
    return out;
  }

  function idwt2Dir(a, H, W, levels, dirs) {
    const sizes = levelSizes(H, W, levels);
    for (let lev = levels; lev >= 1; lev--) {
      const [h, w] = sizes[lev - 1], [lh, lw] = sizes[lev];
      const vb = dirs[2 * (lev - 1)], hb = dirs[2 * (lev - 1) + 1];
      // Passe horizontale (le long des lignes).
      const s = new Float64Array(h * lw), d = new Float64Array(h * (w - lw));
      for (let y = 0; y < h; y++) {
        for (let x = 0; x < lw; x++) s[y * lw + x] = a[y * W + x];
        for (let x = lw; x < w; x++) d[y * (w - lw) + x - lw] = a[y * W + x];
      }
      const v = invDir(s, d, h, lw, w - lw, expandDirs(hb, h, w));
      // Passe verticale (le long des colonnes, sur la transposée).
      const st = new Float64Array(w * lh), dt = new Float64Array(w * (h - lh));
      for (let x = 0; x < w; x++) {
        for (let y = 0; y < lh; y++) st[x * lh + y] = v[y * w + x];
        for (let y = lh; y < h; y++) dt[x * (h - lh) + y - lh] = v[y * w + x];
      }
      const r = invDir(st, dt, w, lh, h - lh, expandDirs(vb, w, h));
      for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) a[y * W + x] = r[x * h + y];
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
  // Probabilités de départ du format J3K5 (même table que dans jpeg3000.py)
  // ---------------------------------------------------------------------------

  // --- priors: begin (table produite par tools/train_priors.py ; fait partie du format J3K5)
  const PRIORS =
    ".Seff.Pccddeff.Lh.Ff.Df.Fhgigjgighgegdgd._.2h.lhh.kiih.jhjjh.ifhjih.hdfiji.hdegiih.ghedigejif.QQgUgZgbgbgdgd" +
    "gdgfgdgdgfgfgf.BhghgighghgjgigigkgUgZgdgege.Bf.Bh.DhghghgjghgigjghgigkghgiglgWgagegege.Fh.Dighgigjghgigjghgi" +
    "gkghgiglgRV.AUY.AZd.AZb.Aad.Acf.Adf.Ad.Beh.Acf.Acf.Aeh.Acf.Acf.Adh.Ad.Bd.Beh.Fh.Ahi.Bh.Bh.Ahj.Bh.Bi.Ahj.Ac.B" +
    "dh.Afj.Ad.Bdh.Afi.Afh.Afh.Bj.Afh.Afh.Bi.Afi.Afi.Bj.Bi.Bi.Ahk.Bh.Bh.Ahi.Bh.Ahi.Aij.Ahi.Aij.Ajl.Bh.Bi.Aik.Bh.B" +
    "i.Aik.Ahh.Ahi.Aik.Bh.Bh.Aij.Ahh.Ahi.Ajk.Aii.Aij.Akl.Ehh.Aii.Ahh.Ahi.Ajk.Ajj.Ajk.Alm.ATXWabf.Kbdcffh.Kdee.Ai." +
    "KZbadeh.Kege.Aj.Kf.Ahhj.Kdfdfgi.Lhghhj.Lhhiik.Kfhfhhk.Khihjil.Kiiijjl.Kfgfgdgcgcgd.Bigjgjgegegbgbgbgd.Bigjgj" +
    "gcc.Aba.Aba.Aec.Adb.Acc.Aed.Aed.Aed.Aff.Aff.Aff.Aed.Adc.Adc.Aec.Adc.Adc.Aee.Aed.Add.Aff.Aff.Aff.Eff.Aff.Aff." +
    "Aff.Aff.Aff.Aff.Aff.Aff.Aff.Aff.wdb.Aca.Abb.Aed.Adc.Add.Aee.Aed.Aee.Aff.Aff.Aff.Aee.Aed.Aee.Aed.Add.Add.Aee." +
    "Aee.Ade.Aff.Aff.Af.Rff.Af.Bf.Bff.Af.Bf.Bf.Bf.Vhh.Ihh.Jh.Aff.Aff.Af.Bff.Aff.Aff.Eff.Af.Zf.Bff.Af.Ff.Oh.hh.Fh." +
    "Ahh.Uhh.Ihh.Bh.Ahh.Aij.Ihh.Ihh.Ihh.Fh.Ahi.Ihh.Ihi.Fh.Aii.Ahh.Ahh.Aii.Uhh.Ihi.Ahh.Ahh.Ajj.Ihh.Ihi.Fh.Aii.Ahi." +
    "Aii.Akk.Abbaabb.Keeedee.Kffffff.Kcbbbcc.Keeedee.Kffff.Mddddee.Kfeeeff.Mf.Nffff.Of.Ah.Lhghhi.KhzmYiGlXZYj9kwk" +
    "PlaeijtkXkej7e1jej0lpkNe5jskIk4jegPkAkVk-jOhMkbkjk5jjhXkVkwlNj_hxiwlsiqmFazkDkmkPk0epj0kZlPiPgzjgkHlIjKg5kQk" +
    "jk2iwiSkgkhkujniKkqkkk6kVh0kYkslMkqh6lUk4lGlOfalBk1k7lEfql0k_lHlYfAlvk7k5lSewlWkyk4lNevkskTlHk2eykFkOlDkRgCk" +
    "RknlBkLgykqk0lCkogdk0k1k_k4gNlLk3lAlQfelHk3k_lOfglUk0k2lQe9lFknkxlJenkqkbk0lCeqkOj2lWkge7j5kKlEj6gekQkslFkHg" +
    "6ksk4k_kr.Ak1k1lAk4gNh.Lf.Fhghgigighgfgfgf._._.dhh.khhh.jhihh.jhih.ifghhh.hf.ljjhigeged.QQgWgagdgdgegdgdgege" +
    "gegfgfgf.FhghghgighghgigVgagegegf.Bf.Bh.DhghghgighghgighghgighghgjgXgbgegfgf.Fh.Dh.BhgighghgighghgighghgjgRV" +
    ".AUY.AYc.Aac.Aad.Acf.Aef.Ad.Beh.Ade.Acf.Ae.Bdf.Adf.Aeh.Af.Be.Bfi.Jh.Fh.Bi.Bh.Bh.Ahj.Acf.Ac.Beh.Ae.Bd.Bfh.Afh" +
    ".Afh.Bi.Af.Bfh.Afi.Afh.Afh.Afi.Bh.Bh.Bi.Fh.Ahh.Bh.Bh.Ahi.Ahh.Ahi.Aij.Fh.Bi.Bh.Bh.Ahi.Bh.Ahh.Ahj.Fh.Ahi.Bh.Ah" +
    "h.Ahj.Ahh.Ahi.Aik.Fh.Ahh.Ahh.Ahh.Aii.Ahi.Aii.Ajk.APUTYYc.KVZXbaf.Kadbedh.Kabaccf.Kcdbed.Lefegfi.Keeefe.Lefeg" +
    "eh.Kfhfhgj.Kfffgf.Lfgf.Ah.Khihihj.Kf.Begdgdgdgfghghghgegfgdgcgcgd.Bhghghgec.Adb.Aca.Afd.Aec.Adc.Afe.Aed.Aed." +
    "Bf.Aff.Aff.Afd.Aed.Aed.Aed.Aec.Add.Afe.Aee.Aee.Aff.Aff.Aff.Iff.Bf.Aff.Aff.Eff.Aff.Iff.wed.Adc.Adc.Aee.Aed.Ad" +
    "d.Afe.Afe.Aee.Bf.Aff.Aff.Aff.Afe.Aee.Aee.Aed.Add.Aff.Aee.Aee.Af.Bff.Aff.Qff.Aff.Eff.Af.6h.Ff.Aff.Eff.Aff.Ff." +
    "Aff.cff.Af.Fff.Af.6h.hh.Ihh.th.hh.Ihh.Vh.Jh.Ihh.Uhh.Ihh.Bh.Ahh.Aii.Afeecec.Kecdcdb.Keddcdc.Kfeeded.Keddcdc.K" +
    "eddcdd.Kfffefe.Kfeeded.Keeedee.Lfffff.Kfffffe.Kffffff.KjqkWjUmPctkpktknlrfGkjklkhlKfEkSkakmlIfRkVkiklksf-kfk" +
    "qkokfghkvkwkvkrgjkykyk0kxghj0kgjwnDdIkfkrkjlbfTkPkkkfkkgNkHkdkbkrghkjkpklkihBkukskpkyg-kxkvkyk6gtkwkxk4lBgkl" +
    "Ck1k7k6fsk5k0k2k4f1lck6lAk_fYlulAk-k9fIlbk7k2k7fMlDkyk0kzfKklkpkukrf4krkxkwkpgRk1k1k1kzgJk0kzk1k0gClKk3lBk_f" +
    "nlEk2k9k_fqlgk6lAk-fRldk0k1k6fElNkwkzk6fGkskikzkvfPkekmkukkgHkukzkykpgXk1k0k2kzgIkzkzk0k0gB.Uhghghghgfgegcgc" +
    "._._._.Fhh.jfhih.iegiih.hdehiih.gcdfhiih.gfehgfih.RSgXgbgdgdgegegegfgegegf.Dh.BhgighghgjghgigkgWgbgegfgf.Fh." +
    "Dh.Bhgi.Bhgj.BhgjghgiglgYgcgfgfgf.Fh.Dh.Bhgighghgj.BhgjghgiglgRW.AVZ.Aad.Aac.Abe.Adf.Aef.Ae.Bfh.Adf.Adf.Afh." +
    "Adf.Ad.Beh.Ae.Beh.Afi.Ihh.Fh.Ahi.Bh.Bh.Ahj.Ad.Bdh.Afi.Ae.Be.Bfi.Bh.Afh.Ahj.Af.Bfh.Bh.Afh.Afh.Ahj.Bh.Bi.Aik.I" +
    "hh.Bh.Bh.Ahi.Ahi.Aij.Ajl.Fh.Aij.Fh.Aij.Bh.Ahi.Ajk.Fh.Ahi.Bh.Ahh.Aij.Ahi.Aij.Akm.Ihh.Ehh.Ajj.Aij.Ajk.Alm.AUYX" +
    "cc.Ldeeffh.Keff.Ah.Kacbefi.Kfgfghi.Nhij.Kefeghj.Nhhj.Lhghij.Lhghik.Khhhijl.Khiijkl.Kf.Begdgdge.Bigjgkgfgfgdg" +
    "cgdgeghgigkgkged.Adb.Adc.Afd.Aed.Aed.Aff.Afe.Afe.Ff.Efe.Afe.Afe.Afe.Aed.Afe.Aff.Afe.Aff.Bf.Aff.Bf._.bhh.Aed." +
    "Aec.Aed.Afe.Aee.Aee.Aff.Aff.Aff.Mff.Aff.Aff.Afe.Aee.Aff.Aff.Aff.Aff.5h.Uhh.Ihh.Fh.Ahi.shh.shh.shh.Vh.Ihh.Bh." +
    "Ahh.Aij.Ihh.Ihh.Ihh.Fh.Aii.Jh.Ihh.Iii.Bh.Ahh.Ajj.Uhh.Ihh.Bh.Ahh.Ajj.Ihh.Ihh.Iii.Ahi.Aii.Akl.Adccbdd.Kffffff." +
    "Nf.Mdddcee.Kffffgf.Ph.Keeeeff.Kffff.Qhh.Ohh.Oii.Khhhhjj.KjqkZjTlCb5lFk0k7ljemkpklkqkVe7kYkelOkffAkSkik3kGgHk" +
    "dkkkxjwg6kpkjkukKhEkWkilCj-iAj9kmj2lVcekukvk0lDe5kekpk8jUgskXkkk-jyg2k1kuk5j8hwlAksk1kTh5lAktk5krhpkmknlCkmi" +
    "elEk2k8lAflk6k0k3k7fwljk8lAlKfJmBlGlAlMetlwlDk8lJeslEkxk7k6e7kZkikzkkf9kakokrkYglklkvk2km.Akok2k8kzgalMk3lCl" +
    "IfhlGk2k-lHfklwk_lFlUe8lzk-k8lNeolXk2k2lEexktkelEk0fKkTkgkxkTgPkbkrkvkTgskpkzkykoglksk5k8k2gb.Whghgh.Begege." +
    "_._._.Fh.lhhh.ifghh.iefhhh.hefghhh.ghh.Dff.QSgZgdgegegfgfgegfgfgf.Fh.Dh.BhgighghgjgYgdgfgf.Nh.Dh.Bhgi.Di.Bhg" +
    "jgagdgfgf.Nh.Dh.Bhgh.DighghgjgSV.AWa.Abe.Acd.Ace.Ae.Bef.Af.Ch.Aef.Aef.Af.Bef.Ae.Bfh.Af.Bf.Ci.Jh.Jh.Bh.Bh.Aij" +
    ".Aef.Ae.Bf.Bf.Bf.Ch.Fh.Ahi.Af.Bf.Ch.Af.Bfh.Bh.Bh.Bh.Ahj.Jh.Fh.Ahh.Ahh.Ahi.Ajk.Ihh.Ihh.Bh.Ahh.Aij.Ihh.Fh.Ahi." +
    "Ahh.Ahi.Ajk.Ihh.Fh.Aii.Ahi.Aij.Akl.ARVWabe.KYbaddf.Kbed.Ai.Kbccdef.Kdedff.Lfgfhhj.Keffff.Lfgf.Ohghij.Kfffff." +
    "Lfgf.Ah.Khiijjk.Ofgfgfgf.Bhgigigfgfgegegegf.Bigigigfe.Aed.Aee.Afe.Afe.Afe.Aff.Aff.Aff.Mff.Afe.Aff.Aff.Afe.Af" +
    "f.Bf.Aff.Aff._.nhh.Aff.Afe.Aff.Aff.Aff.Aff.Aff.Aff.Aff.Mff.Aff.Aff.Aff.Aff.Aff.Aff.Aff.Af.6h.shi.th.shh.shh." +
    "ph.Aii.shh.Vh.Jh.Fh.Aii.hh.Fh.Aii.Uhh.Ihh.Ahh.Ahh.Ajj.Afefeff.Kfefefe.Kfeeeff.Kfefeff.Kfefefe.Kfeeeff.Lfgfgf" +
    ".Kffffff.Kffff.gffgf.Ohh.KlCkAkLl7dJlPk3lClqe7lJk1k2lce4k4kvkzlYfHkpktkvk_f7kskskrkygckzkukuk1gkkwkskukxg8k-" +
    "kfkpmcdkk1kyk3ldfTkrkvktlEf4krkukvlKgbk2kxkxlCg4k9kykylIhBk8kyk0lIg4k3kskylIhMk5k0k2k2fyk2k0k1k1f3lLk3k5k5ff" +
    "lkk-k6k7fJlZk9k3k6fMlHk3k0k0fNkpkskvkvf3kqkvksktgUkykzkxk0gRk1k0k2k3gKlFk2k-k6fpk-k1k5k6ftllk9lCk-fOl0lCk9k-" +
    "e6lhk_k5k9fBlBk0k0k3fOkmkrkuktgDkukwkrktgZkzkzkxk0gSk2k1k4k4gL.Yh.Bfgegdgb._._._.sfhh.jeghh.ideghh.hcdeghiih" +
    ".ffehgfh.SYgbgegfgfgfgfgf.Bfgf.Fh.Dh.Dighghglgbgegf.Ph.Dh.Dh.Dh.Bhglgcgegf.Ph.Dh.Dh.Di.BhglgWY.AYb.Abe.Ade.A" +
    "df.Ae.Bf.Bf.Ch.Af.Bf.Bf.Bf.Bf.Bf.Bf.Bf.Ch.Jh.Jh.Fh.Aij.Af.Bf.Ch.Af.Bf.Ch.Ihi.Jh.Fh.Bh.Bh.Bh.Aij.Uhh.Ahh.Ahh." +
    "Akl.Iii.Ihh.Fh.Ajj.Ihh.Ihi.Ahh.Ahh.Akl.Ihh.Iii.Aii.Aij.Amn.AXaZdd.Lfff.Ah.Kfff.Ah.Kdedfgi.Ohh.Ohi.Kfgfghj.Oh" +
    "h.Oii.Lhhijk.Lhhhjl.Khhhilm.Qfgfgfghgigjgk.Dfgegegfghgigjgkgff.Afe.Afe.Aff.Aff.Aff.Bf.Bf.Bf.Nf.Bf.Bf.Aff.Aff" +
    ".Aff.Ff.Bf._.nhh.Aff.Aff.Aff.Bf.Aff.Aff.df.Ff.Aff.Aff.Vh.th.sii._.bhh.shh.sij.Ihh.Jh.Uii.Uhh.Ihh.Iij.hh.Fh.A" +
    "jj.Uhh.Ihh.Ahh.Ahi.All.Afefdff.ufefeff.Nf.Qhh.Kffff.ihh.Oii.Oii.Khhhhjk.Klukhk4lRdelVk4lHlae7lGk1k5k-fIk8kzk" +
    "9kzfRkvkxk3kngNk4kyk4kngslDk0k4kvg4k6kkkxkWh0lpk1lclddvk-k0k-lBfRk0kyk5kngOk4kzk6kxghlFk1k-k2hIlRk3lBlEhZlSk" +
    "3lFlOhVlMkulElCiWk1kzk0k1f3k0kzk0k0f6k_k1k2k3folTk5k3k6fSlUk6k3k4fNlDk3k1kwfPkskvk0ktgDkykxktksghk7kzkzk1gik" +
    "5k5k9k5glk9k1k4k5fuk4k0k2k4fylTk5k8lAfXlslAk9lCe-lck-k4k9fCk-k0k4kzfTkukwk0kpgQk7k0kxkqgolAk1k1k0gmk7k6k6k4g" +
    "l.eegegd._._._._.Tfghh.iefghh.hdefghh.7Ygdgfgfgf.Bfgf.Rh.Dh.Bhgjgdgf.dh.Dh.Djgegf.dh.Dh.DjgVZ.AZc.Ade.Aef.Ae" +
    "f.Af.Bf.Bf.Fff.Af.Ff.Bf.Oh.Vh.Iii.Af.Bf.Jf.Oh.Vh.Ihi.Uhh.Ehh.Ajj.Ihh.Jh.Iii.Uhh.Ehh.Ajj.Uhh.Ahh.Ahi.Akl.AWZa" +
    "ddf.Kbddfe.Ldfe.Ai.Keeefff.Kfffgf.Ohhi.Kfff.ihij.uhhhijl.Qfgfgf.Bhgigi.Dfgfgfgfghghgigi.Af.Aff.Aff.Bf.Bf.Bf." +
    "Zf.Bf.Ff.Bf._.3hh.Bf.Bf.Jf._.4h.sii._.ch.th.sii.shh.sii.sii.ghh.Bh.Ahh.Ajk.Bfgfgf.Kffffff.Lfff.Nfgf.Nfffgf.K" +
    "ffff.hf.Qhh.xhii.Kmqk5lolxdmlIk2k_lSfNlGk2k4lKfMk7k0k1lGfbkzkzk0k7gCk5k0k1k8gYk7k0k3k8gflCktktk2g-mFk9l4lwd5" +
    "k5k0k5lEffk0kzk0lAf2k1kzk0lCgOk6k0k2lDglk_k1k4lIgvlDk1k-lPgvlLk0k7lPhTk0kzk0k0f8k0kzkzk0f8k5k0k0k0fwlEk2k1k1" +
    "fjlEk2k0k1fik_k1kzkzfjkykykzk1gDk3kzkyk2gWk8k1k0k6gWk8k2k2k8gTk5k0k2k1fyk2kzk0k0f3lGk2k4k3fjlWk6k3k4fUlMk4k1" +
    "k2fZk7k1k0k0fkkzkykzk1gLk7k1kyk3galAk2k1k-gYk9k2k3k9gS.cfgegdgb._._._.sf.lef.kdfgh.ibcdfghhh.gf.Bh.Sdgegf.bh" +
    ".Dhgjghglgfgf.dh.Dhgi.Bkgf.lhgi.Bkgbc.Ace.Aef.Aff.Aff.xh.Yii.Eij.Jh.Vh.Zh.Ehh.Yhh.Bh.Ajj.Ahh.Ehh.Jh.Ahh.Eii." +
    "Uhh.Ahh.Bh.Ajj.Uhh.Ann.Ahh.Ano.Acecefh.hh.Kfgfghi.Ph.Ohh.Ohi.Ohh.Ohh.Kklhhlm.Khh.Ajj.Kllhhmm.Whghgigk.Lhgigi" +
    "gk.Af.Bf.Bf._._.mhh.Ehi._._.yhh.Ehi._._.yhh.Eii.Ahh.Ehh.ghh.khh.Eii.khh.Eii.hh.All.Ahh.Alm.Cff.zf.1h.ghh.Kjk" +
    ".Ajk.Khh.Aii.Kkkhhkk.KmHlBljlbegk6k0k3k7fok4k0k0k5fvk2kzk2k1f5k2k0k1k2gSk9k1k3k5gmlGk2k8lBgwmWk6lFlIiOlgk6lZ" +
    "lHe2k2kzk1k1fwk1kzk1k0gNk2kzk1k5gVk5k0k2k-gmlCk1k6lIg3lGk2lClRhAmvlCltl4jAk0kzkzkzf_kzkzkzkzf-k1kzk0k0f4k3k0" +
    "k0kzf0k5k0k0kzfwk1kzk0kyf3k1kzk0k1gQlGk3k3k9gqlWk6k9lHgwlpk8lDlGg8k1kzk0k0f3k0kzk0kzf7k5k0k1k1fxk_k1k0k1fpk7" +
    "k0k0kzfuk2kzk0kwf4k6k0k0k4galOk4k3k-gwlck7k_lIg0lpk8lDlFg8.efgegd._._._._.Tf.lff.kdde.Ahh.6dgf.li.Bjgf.nh.Bi" +
    "gf.nh.Bigbd.Ade.Aff.Aff.Af.yh.Yhh.Ehh._.Dhh.Yhh.Eii.Ihh.Nh.Ehh.Yhh.Eii.Vh.Akk.Ahh.All.Aadeff.Ldffff.Lfgf.Ah." +
    "Kfff.gh.Ahh.uhh.Ahh.ukkhhll.Yhghgj.Nhghgi.Af.Bf._._.rh.Ehh._._.6hh._._.6hh.shh.shh.shh.kjj.Ahh.Akk._._.Th.uj" +
    "jghjj.KmMlBlmlTehk3k0k1k6fuk3k0k0k4fwk1kzk0k4f4k0kzkzk3gIk3k0k0k6gUk7k0k2k-gcl-k8lFlXhjlWk4lPlFfBk1kzk0k2f0k" +
    "zkzkzk2f_k0kzk0k4gKk1kzk0k6gUk4k0k2k_gek7k0k4lEgjl5k9lVlxh8kzkzkzkzf-kzkzkzkzf_k0kzkzkzf6k1kzkzkzf3k2k0kzk0f" +
    "3k0kzkzkzf5k0kzkzk2gIk7k0k0k7gYlEk2k3lBgclZk6k9lHgmk0kzk0kzf5kzkzkzkzf-k2k0k0kzf2k5k0k0k0fyk3k0kzk0f1k1kzkzk" +
    "0f6k1kzkzk3gOlBk2k1k8gclGk3k3lCgelak6k8lHgl.Scef.Pdeeff.Njigi.Vhghgh._._._._._._.Tf.Gfh.Dh.DPXYZbafffffeffff" +
    "ffffffff.WWbbbddffffgf.iXbccedffffgf.iOTTXUYWZYaYaccdedddededefffgfffgfgegfffffffgfffgeffgfffgfgegfffgfgfgfg" +
    "f.Kh.Weffgfgegfgehfgfgfgfgfge.Ffgfgf.Bf.Bf.Bfh.Bh.Bh.Bh.Bh.Fh.Vh.Ihh.Jh.th.Ihh.ghhOUVYYbVYXaZcWaXbadadddefce" +
    "deefefefegeffffffffffffgfgfgbbccdededeefefefegfffffffffffffgf.Cf.Cfgf.Df.Bfffffffffff.Bf.Ah.Iff.Rh.Th.Kf.Wf." +
    "Bffefefe.Hfgffffefefe.Gffeeffeefeddfffegffefffegfgf.Aff.Afe.Jfffffgffeffeefffffffeffeegfff.Affgfff.Jf.Vf.If." +
    "7fffeffeeffedffffgffffffe.Bf.Aff.Aff.Mff.Aff.Affffffgfffffee.Aff.Aff.Aff._._._._._._._.jeefeedfefeedeeeddcgf" +
    ".Bfgfgfgffffefe.Hfgf.Afffffefeeefefeeefeeddd.Ffgfgffefffe.Jf.Bfffffffffgfffffffffee.Kffffff.gff.ik7k2kllCeMk" +
    "3kzk0lHftk1kxkykvfwk0kxkzk3fvkykykzklgDk0kzkzkrgGk0kzk0kxgDkzkzkzkzgBlCktk5lZeskykxkzlAfwkwkxkzkigJkykyk0kqg" +
    "Gk1kzk0kigPk1kzk0kugJk0kzk0kzgDkzkzkzkzgBk3k0k1k2f5k0kzk0k0f9k9k1k2k3f0lFk2k2k4fulDk2k1k4fvk_k1k0k3fxk0kzkzk" +
    "0f9kzkzkzkzgAkzkzkzkzgAkzkzkzkzgAk3k0k1k2f6k2k0k1k2f7k_k1k2k4fxlIk2k2k5fplEk2k1k4frk-k1k1k3fvkzkzkzkygAkzkzk" +
    "zkzgAkzkzkzkzgAkzkzkzkzgAihgh._._._._._._._.EPZaadcffgfgfffgf.Af.Af.YXcdcfe.oadeefe.oOQWZXaYaacaceeefffeffgf" +
    ".Ff.Bfgfffffffffgfgfgf.Bf.Bf.veffgfgfgfgf.Bf.Bf.Pf.Df.Bf._._.lORXbbdXaYbbdZbacbdZcceefcdceefdedeefefefffefef" +
    "fgfffffgcdeffgefffffffffffffff.Afgfgfgfffff.Ffgf.Ffgfff.Jf._.ffgfgfgf.Hf.Bfffgfgf.Gffffgfffgfffffffgfffgfffg" +
    "f.Df.Aff.Mff.Aff.Affffffgfff.Afff._.0ffff.Aff.Affffff.Aff.Bf.Xfgf.Bf.Bfgfgf.Aff.Aff._._._._._._._.veefffffff" +
    "fffffffff.Dfgfffgffffeff.Dfgfgfgfffffffffffgfffgfffgfffff.Ffffgfffffff.Ffgfgfffffffgf.Nf.Nfgf.Pf.0lDkukyk7fM" +
    "k0kzk0k3f5k1kzk0k2f4k0kzkzkwf7kzkzkzkxgAkzkzkzkygAkzkzkzkzgAkzkzkzkzgAlKkxlDlEfYkzkzkzk1f8kzkzkzkxf-kzkzkzks" +
    "gDkzkzkzkwgCkzkzkzkygBkzkzkzkzgAkzkzkzkzgAk0kzk0k0f-k0kzkzk0f-k1k0k0k0f8k4k0k0k0f5k2k0kzk0f8k1k0kzk0f8kzkzkz" +
    "kzgAkzkzkzkzgAkzkzkzkzgAkzkzkzkzgAk1kzk0k0f9k1kzk0k0f9k2k0k0k0f7k6k0k0k0f3k3k0k0k0f6k1k0kzk0f8kzkzkzkzgAkzkz" +
    "kzkzgAkzkzkzkzgAkzkzkzkzgAhh.Vhghgi.Bfgfgf._._._.Xhh.lhh.jfhh.jfghh.5fgffgf.Hhghhgh.BMWZZddfefefffffefffffef" +
    "f.Jh.Dh.BhgiWZcceffffff.Eh.Dh.Dh.Dh.Dh.DiXacdeffffffgfgf.Ah.Dh.Dh.Dh.Dh.BhgiKRTYTXWaacbeddcdddcededfffefffeg" +
    "fffgffefffeffffhffdfefdfffegffefffegfffh.Jh.Jh.Cfgfgf.Ahefegefehfffifffgffegfffh.Afgfgfgf.Ahf.Bfgf.Ch.Bhfgfh" +
    "f.Ai.Bhf.Ahfghi.Ihh.Fh.Ahi.Bh.Bh.Ahi.Fh.Ahi.Fh.Ahi.Fh.Ahi.Ihh.Fh.Ahi.Ahh.Ahh.Aij.Jh.Ehh.Ahi.Ahh.AhighijKSUYa" +
    "cTXWabeXbaddhccdeffdeeffgefe.Ahdeeffgeffffgfgf.Ahbbccdecdcddfdee.Aifffffgfffgfgfgf.Aigffffgfgfgf.Dhheeeeefff" +
    "effgfgfghifgf.Dfgf.Chhi.Nhhiffefeffgfgf.Ahghij.Lhghhi.Jhhhhhiigf.Bfgegege.Bhgh.Bfgfgefdfdfe.Bhgh.Cedgfdcffdc" +
    ".Afegfedgfed.Aff.Aff.Afe.Ff.Aff.Afe.Afegfee.Afegffegfee.Aff.Aff.Aff.Ff.Aff.Uf.Kf.Ff.Bf.weeffedffdd.Aff.Afegf" +
    "ee.Aff.Aff.Aff.Ff.Eff.Aff.Aff.Aff.Afegfee.Aff.Aff.Aff.Ef.Bf._._._._.Zhh.Jh.4hh.Ihh.th.Fh.Ahh.Vh.Ihh.Bh.Ahh.A" +
    "higffffefefeedddccdc.Ffgfffffffff.Hfgfgfgfgfgffffffffeeeeddddd.Ffgfffffffff.Hfff.Aff.Dfffgfffffffeeff.Hfgf.A" +
    "ff.Lf.Af.Efff.Affff.Aff.Jfgf.Nf.FhlFkYkNlwcDlEkvk2lze8k5kpkqlUfLk6knk8ltfQktkvk0lBf8kykxk0k3gQk2kyk0k1gRk4ky" +
    "k2k0gRlCkXkzmldDk5kwk6lxfFkzkwk-k7gQkwkuk7lYf6k5kyk2kzgok-kzk1k3gmk6kzk2k4gbk5kzk5k5gVk-k1k5k6fuk2k0k1k2f4lV" +
    "k5k9lAfcljk8k8lBfMlck6k5lAfQlIk0k2k6fbkykxk1kzf8kzkzk1kygGk1k0k0k0gDkzkzk0kzgBlEk2k9k9fqk7k0k3k6fxlck6lAlBfR" +
    "lok4k5lBfAlXk0kzk-fJlCkuk2k5fbkvkwk2k0f_k0kzk1kygIk1k0k0k0gEkzkzk0kzgB.Xhghgh._._._._._._.rRXaZedffffgfgfffg" +
    "fgfgfgf.Ph.DhYbddffgf.Th.Dh.Kacedff.Vh.Dh.KOTUYWZXabcaeeedeeedeefeffffgfffgfgfgffffffeffffgffefffegfgf.Dfgfg" +
    "ffgh.Vh.Jhffefffegfffgfgfgfgfgfgf.Dfgf.Ch.Ef.Oh.Fh.Bh.Vh.Ihh.Vh.Jh.Jh.Jh.Ihh.Zh.Ah.BhhOTXacdUYYbdeXZZccfZbbd" +
    "eeacbddfadbddgcddeefdedeefdfdffhededefeeeeffeeeffgfffeffffefffefeffgfffffffffffgfgf.Ahffffffgffffgfgf.Cfffff" +
    ".Afgf.Bf.Dfgfgfgfgf.Chgh.Uf.Df.Uhhi.Dfgfgfgf.Hf.Bfgegegf.Ifegfedgfed.Affgffe.Afe.Bf.Aff.Aff.Nf.Afe.Aff.Bf.Af" +
    "f.Aff.Bf.Aff.Aff.Jf.Uf.3f.Pff.Afe.Afe.Aff.Aff.Aff.Aff.Aff.Aff.Nf.Bf.Aff.Aff.Aff.Aff.Eff.Aff.If._._.jf._._._." +
    "_.8fgffffefe.Dfgfgffffffefe.Bfffgffffffffeee.Hfgfgfgfff.Dfgfgfgfffffff.Bfgfgffffffffefe.Nf.Jfgfgfffff.Bfff.B" +
    "fffffffff.nf.Cff.Afgfflhkkk2lSeGlBk0k5lOfelBk0k1lEfdk9kykzlEflkzkzkzk3f6kzkzkzkzgCk0kzkzkzgDk0kzk0kzgClZktlM" +
    "lgelk4kzk4lJfkk0kzkzlBfxk0kzkzk9f7k0kzk0k0gIk1kzk0k1gIk0kzk0k1gEk0kzk0k0gCk1kzk0k0f6k0kzkzkzf-k7k0k2k1f0lCk2" +
    "k2k1fvk_k1k0k0fxk7k0kzkzfzkzkzkzkzf9kzkzkzkzgAkzkzkzkzgAkzkzkzkzgAk4k0k2k0f4k1kzk0k0f7lEk2k5k1ftlNk4k3k2fmlE" +
    "k2k0k1frk6kzkzkzfykzkzkzkzf-kzkzkzkzgBkzkzkzkzgAkzkzkzkzgA.Xhghgh.Bfgegf._._._.Yh.lhh.jfghh.iffhh.iff.Ah.hh." +
    "ETYaaeefffffffffff.Affff.Kh.Dh.BhgiZbddf.Qh.Dh.Dh.Dh.Dibceef.Qh.Dh.Dh.Dh.DiOSVYVZXbbdcfeedeeedfeeegfffgfffgf" +
    "fghfffffffgfffgfffgffegfffhfgfgfffgffgh.Jh.Jh.Fhfghifffgffegffgifffgfffgffgh.Fhf.Ai.Jh.Fh.Bh.Bh.Bh.Ahi.Jh.Ih" +
    "h.Bh.Ahh.Aij.Ihh.Ihh.Fh.Aii.Jh.Fh.Ahi.Bh.Ahh.Aij.Jh.Ihh.Ahh.AhighjjPTVZbdVYXbceZcbefhdeeffffffffgfgf.Aheffff" +
    "ffffgfgf.Chccddeededeefefeghi.Af.Lhh.Af.Lhiffeffffffffgf.Ahhj.Ohi.Ohiffffeffgfgf.Ahhhij.Lhhhij.Jhghhhij.Dfgf" +
    "gfgf.Bhghghgf.Begegegf.Bhgigh.Afegfedgfed.Aff.Afe.Afe.Bf.Aff.Aff.Nf.Aff.Aff.Aff.Aff.Aff.Bf.Aff.Aff._.rff.Afe" +
    "gfee.Bf.Aff.Aff.Ff.Aff.Rf.Af.Bff.Aff.Aff.Eff.Aff._.oh._._.Lh.shh.shh.Vh.Ihh.Ihh.shi.ghh.Ehh.Aii.Bffffffefeee" +
    "edee.Nff.Ufffgffffffeeeee.Nf.Xfgfgffffffff.of.Bfff.Dh.Ohh.OhhmKkSkyl8cHlfk5lLmDeglRk2k4lqeulHkzk8lte5kykyk0l" +
    "Ff4k4kzk0k8gYk9k0k2k6gdlCkyk2k7gmmIktlwmucmlHk1lHlze0k9kzk9lUf4lBk0k6ligClDk2k5lEgzlHk2k5lEg5lDk1k7lEgtlEkzk" +
    "-lDgyk4k0k2k2fxk0kzk0k0f5lFk2k4k6fhlgk8k6k-fJldk8k5k9fHlPk5k1k6fMkvkxk0kzf7kzkzkzkygMk3k0k0k1gKk3k0k3k2gFk_k" +
    "1k6k6fpk5k0k2k3fxlgk7lBlCfKl3lCk-lEe0lmk_k6lCe5lFk1k2k9fOkvkxk0k0gAk3k0kzk0gRk7k1k1k2gMk2k0k2k2gF.bh.Bfgf._." +
    "_._._.qh.jf._.TVadcffgfgf.Bfgf.Xh.Dhbdfef.ch.Dh.Dhdefff.ih.DhRVWZYbZcdedfffefffeffffgf.Bfgfgf.Bfgfffffgfgfgf" +
    "ffgfgfgf.Mh.ghhfffgfffgfgf.Ff.Ch.Jh.Vh.Ihh.Vh.Fh.Ahi.Vh.Ihh.Vh.Fh.Ahh.Vh.Ahh.Ahh.AiiSVZcdeYbcdefacceegbddeef" +
    "deefffcedffgefefffefeffgegf.Aheefffffffffgfffffgfffffffgfffgfgf.Bfgfgfgfgfgf.Chhiffffff.Of.ahhi.Cf.qhhhii.Ff" +
    "gfgf.Dh.Ffgfgfgf.Bhgh.Cff.Afe.Afe.Bf.Aff.Aff.Jf.Nf.Bf.Aff.Bf.Aff.Aff._.4f.Aff.Aff.Ff.Aff._._.jh._._.7h._.bhh" +
    ".sh.qh.Ahh.Hf.Bfffff.Ffgf.Affffff.Hfffffffff.Lfgfgf.Lfffff.Ifffffff._.jf.Em8k9lylydLlLk3lAlUfHlKk2k4lNfFlBk1" +
    "k1lIfVk0kzkzk6f4k1kzk0k3gGk2k0k0k2gIk3kzk0k2gKmelAmLl6dfk8k0k7lLfWk3kzk1lEfnk4k0k1lCf7k4k0k1k7gPk4k0k1k7gRk4" +
    "k0k1k5gOk5k0k3k6gOk0kzk0k0f7k0kzkzkzf9k5k0k1k0fylDk2k1k1fmlCk2k0k1fok9k1k0k0fqkykzkzk0f9kzkzkzkzgDk0kzk0k0gC" +
    "kzkzkzkzgBk4k0k2k0f0k1kzk0k0f5lEk2k4k2fmlRk5k3k2fblIk4k1k2fgk-k1k0k2fokykzkzk0f_k0k0kzk0gFk1k0kzk0gDkzkzk0k0" +
    "gB.dfgegege._._._._.mfghh.iefgh.ieefghh.Uf.Hh.GZbedffgf.Df.Ph.Bhgh.Bhgjghgjdeff.Xh.Dh.Dhgi.Biefff.dh.Dhgi.Bi" +
    "WZYbacadefefffefffeffff.Hf.Bfff.Bf.Dfgf.Bf.Lf.Ah.Vh.Ahi.Ehifgfhfgfgf.Ah.Jh.Bh.Fh.Nh.Fh.Ahh.Ehi.Vh.Aii.Bh.Aij" +
    ".Ahh.Ehh.Bh.Ehh.Ahh.Ehi.Mhh.Ehh.Aii.Ahh.Aij.Mhh.Ehh.Akk.Ahh.AkkWZadefZcbeefcfcfgiffff.Afgf.Ghffff.Afgf.Ch.Ah" +
    "heeffffeffffgfhfghi.Ohh.Khh.Ahhfffffgfgf.Bhhghii.Lh.Ahh.Khh.Ahh.Cf.Fjkhhjk.Kiighij.Kjjhhjj.Ffgfgf.Bhgigigf.B" +
    "fgfgf.Bhghgigi.Afe.Afe.Aff.Aff.Aff.Aff.Zf.Bf.Ff.Bf._.vhh.Fh.Aff.Aff.Aff.Bf._._.ihh.Ehh._._.yhh.Ehh.Bh.hh.Ehh" +
    ".hh.Ahh.Ehh.khh.Ehh.ghh.Ajj.Ahh.Ajj.Ffgfgffefeff.pfgfgfffffff._.jii.Ahh.Khh.Ahh.Kii.AiinJk5l0mCcclMk3lBlfe4l" +
    "Hk2k4lVe_lCk1k5lVfMk4k0k1lAgIlDk2k4lAgqlNk4k9lDg2ltk6lBlIhYmtlDmamPc1k7k0k7lOfOk5kzk5lGgEk_k0k5lOgQlGk2k6lHg" +
    "-lOk3k-lOhNlRk4lGlQhLl9k-lXldh2k5k0k2k2ftk0kzk0k0f4k-k1k1k2fllJk3k2k3fXlFk2k1k3fZk9k1k0k4fgkzkyk0k2gJlBk3k2k" +
    "5.AlFk3k5k8gblCk1k6k8gTlAk1k7k4fhk2k0k1k2fxlQk4k6k7fQlck8k5k9fDlLk4k2k6fPk7k0k1k6fkk2kzk0k4gRlHk4k2k6gklIk3k" +
    "5k8gdlEk2k8k9gV.ffgfgf._._._._.mf.lf.lff.radfe.jhgh.Bhefff.lh.Bhff.nh.BhXaacceceeffgfffffffgf.Nfgf.eh.Yhh.Eh" +
    "hf.Df.2h.Fh.Yhh.Ehh.Yhh.Ehh.Yhh.Ehh.Vh.Aij.Ahh.AiiXbdeffcdeffgdeef.Adffffgefff.Aeff.Bfffffgfgf.Chfghhfffffgf" +
    "ff.Bfgf.Bff.Cf.Vhhghhi.uhhghhi.ujjhhij.Nhghgh.Ff.Fhghgh.Aff.Aff.Aff.Bf.Bf._._.rf.Bf._._.yhh._._.6hh._.Uh.Ehh" +
    ".lh.Ehh.kii.Eii.Lfgf.Nfgf.Pf._._.ahh.AhhnclMmPltdCk-k1k5lDfUk_k1k1lAfSk5k0k0k-fhk0kzk0k6gAk4k0k0k6gRk6k0k2k7" +
    "gVlNk3k5lBglmZlCmIlndnk3kzk3k9fhk1kzk0k8fzk2kzk0k9gEk3k0k0k8gVk6k0k2k_gek7k0k4lAgdlLk3k_lJgvk1kzk0k0f2k0kzkz" +
    "kzf7k2k0k0k0fzk7k0k0k0frk5k0kzk0fvk3k0kzk0fwkzkzkzk1gDk4k0k0k2gMk5k0k1k3gKk3k0k1k3gGk4k0k2k0fvk1kzk0k0f3k-k1" +
    "k2k0fnlDk2k1k1fik7k0k0k0fqk3k0kzk1fwk1kzkzk2gIk7k1k0k3gPk6k1k1k4gLk4k0k2k3gGgh.Bf.Bh.Bf.JpW.ASfh.Ifhgff.Bf.A" +
    "fghgh.Ah.Ch.NtS.AQ.Kfhgff.Gh";
  // --- priors: end

  // ---------------------------------------------------------------------------
  // API
  // ---------------------------------------------------------------------------

  function readHeader(bytes) {
    const magic = String.fromCharCode(bytes[0], bytes[1], bytes[2], bytes[3]);
    if (bytes.length < HEADER_SIZE || (magic !== MAGIC && !OLD_MAGICS.includes(magic))) {
      if (bytes.length >= 4 && String.fromCharCode(bytes[0], bytes[1], bytes[2]) === "J3K") {
        throw new Error("JPEG 3000 file from another version (J3K" + String.fromCharCode(bytes[3]) +
          "): this decoder reads " + MAGIC);
      }
      throw new Error("not a JPEG 3000 (.j3k) file");
    }
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const hd = {
      width: dv.getUint32(4, true), height: dv.getUint32(8, true), channels: bytes[12], levels: bytes[13],
      step: dv.getFloat32(14, true), chroma: dv.getFloat32(18, true), version: magic,
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
    const version = Number(hd.version[3]);
    const rc = new RangeDecoder(bytes, HEADER_SIZE);
    const start = startState(version === 5);
    const dirs = version >= 4 ? codeDirs(rc, start.dirs, dirShapes(h, w, levels)) : null;
    const specs = bandSpecs(h, w, levels);
    const luma = start.luma, chromaModels = start.colors;
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
          const model = md.bands[2 * (Math.min(sp.lev, 4) - 1) + (sp.o === HH ? 1 : 0)];
          const X = ci ? planesBands[0][bi] : null;
          if (version === 5) {
            band.n = new Int32Array(bh * bw);
            codeBandDq(rc, model, band, parents[sp.o] || null, C, X);
          } else {
            codeBand(rc, model, band, parents[sp.o] || null, C, X, version === 4);
          }
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
        const s = sp.o !== LL && sp.lev === 1 && version >= 4 ? st * LEVEL1_STEP / sp.norm : st / sp.norm;
        const dq = version === 5 && sp.o !== LL, sd = s * DQ_SCALE, q = dq ? bands[bi].n : bands[bi].d;
        for (let y = sp.y0, j = 0; y < sp.y1; y++) {
          for (let x = sp.x0; x < sp.x1; x++, j++) {
            const v = q[j];
            if (sp.o === LL) a[y * w + x] = v * s;
            else if (dq) a[y * w + x] = Math.sign(v) * (Math.abs(v) + DQ_BIAS) * sd;
            else a[y * w + x] = Math.sign(v) * (Math.abs(v) + BIAS) * s;
          }
        }
      });
      if (dirs) idwt2Dir(a, h, w, levels, dirs);
      else idwt2(a, h, w, levels);
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
