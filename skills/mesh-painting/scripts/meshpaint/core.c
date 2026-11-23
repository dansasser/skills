/* meshpaint native core: triangle rasteriser and brushstroke painter. */
#include <math.h>
#include <stdint.h>

static inline float clampf(float x, float a, float b) { return x < a ? a : (x > b ? b : x); }

/* q is interpolated linearly on screen; the larger value wins (1/z for a camera, height for a plan view). */
void mp_raster(const double *xs, const double *ys, const double *q, const int32_t *faces,
               const uint8_t *fmask, int nf, int W, int H, int persp,
               float *Q, int32_t *TID, float *B1, float *B2) {
    for (int f = 0; f < nf; f++) {
        if (!fmask[f]) continue;
        int i0 = faces[3 * f], i1 = faces[3 * f + 1], i2 = faces[3 * f + 2];
        double xa = xs[i0], xb = xs[i1], xc = xs[i2];
        double ya = ys[i0], yb = ys[i1], yc = ys[i2];
        double qa = q[i0], qb = q[i1], qc = q[i2];
        double minx = fmin(xa, fmin(xb, xc)), maxx = fmax(xa, fmax(xb, xc));
        double miny = fmin(ya, fmin(yb, yc)), maxy = fmax(ya, fmax(yb, yc));
        if (maxx < 0 || minx >= W || maxy < 0 || miny >= H) continue;
        double ar = (xb - xa) * (yc - ya) - (xc - xa) * (yb - ya);
        if (fabs(ar) < 1e-7) continue;
        /* Clamp floating bounds before converting: near-plane projection can be
           finite while extending far beyond an int32 screen coordinate. */
        int xm = minx < 0 ? 0 : (int)minx;
        int xM = maxx >= W - 1 ? W : (int)maxx + 1;
        int ym = miny < 0 ? 0 : (int)miny;
        int yM = maxy >= H - 1 ? H : (int)maxy + 1;
        for (int y = ym; y < yM; y++) {
            double py = y + 0.5;
            for (int x = xm; x < xM; x++) {
                double px = x + 0.5;
                double w1 = ((px - xa) * (yc - ya) - (xc - xa) * (py - ya)) / ar;
                double w2 = ((xb - xa) * (py - ya) - (px - xa) * (yb - ya)) / ar;
                double w0 = 1.0 - w1 - w2;
                if (w0 < -1e-4 || w1 < -1e-4 || w2 < -1e-4) continue;
                double iz = w0 * qa + w1 * qb + w2 * qc;
                long idx = (long)y * W + x;
                if (iz > Q[idx]) {
                    Q[idx] = (float)iz; TID[idx] = f;
                    if (persp) { B1[idx] = (float)(w1 * qb / iz); B2[idx] = (float)(w2 * qc / iz); }
                    else { B1[idx] = (float)w1; B2[idx] = (float)w2; }
                }
            }
        }
    }
}

/* P: n rows of 11 floats = cx, cy, angle, length, width, r, g, b, opacity, bend, bristle */
void mp_strokes(float *canvas, float *height, int W, int H, int n, const float *P,
                const int32_t *reg, const float *masks, const float *BR, uint32_t seed) {
    uint32_t s = seed ? seed : 1u;
    for (int i = 0; i < n; i++) {
        const float *p = P + 11 * i;
        float cx = p[0], cy = p[1], th = p[2], L = p[3], w = p[4];
        float cr = p[5], cg = p[6], cb = p[7], op = p[8], bend = p[9], bristle = p[10];
        s ^= s << 13; s ^= s >> 17; s ^= s << 5;
        float off = (float)(s & 8191u);
        float r = 0.5f * L + 0.5f * w + fabsf(bend) + 2.0f;
        float left = cx - r, right = cx + r, top = cy - r, bottom = cy + r;
        int x0 = left <= 0 ? 0 : left >= W ? W : (int)left;
        int x1 = right >= W - 1 ? W : right <= -1 ? 0 : (int)right + 1;
        int y0 = top <= 0 ? 0 : top >= H ? H : (int)top;
        int y1 = bottom >= H - 1 ? H : bottom <= -1 ? 0 : (int)bottom + 1;
        if (x1 <= x0 || y1 <= y0) continue;
        float c = cosf(th), sn = sinf(th);
        float sf = 0.1f * w; if (sf < 0.7f) sf = 0.7f;
        float nb = w * 0.9f; if (nb < 3.0f) nb = 3.0f;
        const float *mask = masks + (long)reg[i] * W * H;
        for (int y = y0; y < y1; y++) {
            float dy = (float)y - cy;
            for (int x = x0; x < x1; x++) {
                float dx = (float)x - cx;
                float u = dx * c + dy * sn, v = dy * c - dx * sn;
                float al = clampf((0.5f * L - fabsf(u)) / sf + 0.5f, 0.f, 1.f);
                if (al <= 0.f) continue;
                float tc = clampf(u / L + 0.5f, 0.f, 1.f);
                float arc = 4.f * tc * (1.f - tc);
                float vv = v - bend * (arc - 0.5f);
                float hw = 0.5f * w * (0.5f + 0.5f * sqrtf(arc));
                float a = clampf((hw - fabsf(vv)) / sf + 0.5f, 0.f, 1.f) * al;
                if (a <= 0.f) continue;
                long idx = (long)y * W + x;
                float m = mask[idx];
                if (m <= 0.f) continue;
                float st = BR[((int)(off + (vv / w + 0.5f) * nb)) & 8191];
                a *= clampf(1.f - bristle * st * (0.5f + 1.3f * tc), 0.f, 1.f) * op * m;
                float *px = canvas + 3 * idx;
                px[0] += (cr - px[0]) * a; px[1] += (cg - px[1]) * a; px[2] += (cb - px[2]) * a;
                height[idx] += ((1.f - 0.7f * st) - height[idx]) * a;
            }
        }
    }
}
