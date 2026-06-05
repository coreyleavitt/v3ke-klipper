/* On-device confirmation that the Ingenic 4.4.94 kernel really supports the o32 nan2008/fp64 ABI
 * our toolchain targets — i.e. that glibc-patches/0100-* (lowering the nan2008 min-kernel floor
 * from 4.5.0 to 4.4.0) describes a real device capability, not a hopeful override.
 *
 *   - nan2008: a quiet NaN must have the significand MSB set (2008 convention).
 *   - fp64 (FR=1): run double-precision math across threads; if the kernel mis-saves the upper
 *     halves of the 64-bit FP registers on a context switch, a thread's result diverges from the
 *     identical serial computation. Same seed + same ops => must be bit-identical.
 *
 * Build with this toolchain's DEFAULT flags (no -mnan/-mfp64 — they're the compiler default) and
 * run on the device:
 *   podman run --rm -v "$PWD":/w v3ke-toolchain \
 *     /opt/x-tools/mipsel-buildroot-linux-gnu/bin/mipsel-buildroot-linux-gnu-gcc \
 *     -O2 /w/toolchain/tests/fp64test.c -o /w/fp64test -lpthread -lm
 *   ssh v3ke 'cat > /tmp/fp64test && chmod +x /tmp/fp64test' < fp64test
 *   ssh v3ke '/tmp/fp64test; rm -f /tmp/fp64test'
 * Expected: "FP64/NAN2008 ON-DEVICE TEST PASSED" (exit 0). Confirmed 2026-06-05 on F005-1440.
 */
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <pthread.h>

static double work(double seed) {
    double a[8];
    for (int i = 0; i < 8; i++) a[i] = seed + i * 0.5;
    double s = 0;
    for (int iter = 0; iter < 200000; iter++)
        for (int i = 0; i < 8; i++)
            s += sqrt(a[i]) * 1.0000001 - a[i] / 3.0;
    return s;
}
static void *thr(void *p) { double *r = (double *)p; *r = work(*r); return NULL; }

int main(void) {
    double nan = __builtin_nan("");
    uint64_t bits; memcpy(&bits, &nan, 8);
    int nan2008 = (int)((bits >> 51) & 1u);
    printf("qNaN bits = 0x%016llx  2008-quiet-bit=%d\n", (unsigned long long)bits, nan2008);

    pthread_t t[4]; double r[4];
    for (int i = 0; i < 4; i++) { r[i] = 1.0 + i; pthread_create(&t[i], NULL, thr, &r[i]); }
    double single = work(1.0);                 /* same seed as thread 0 */
    for (int i = 0; i < 4; i++) pthread_join(t[i], NULL);

    int fp_ok = (r[0] == single);              /* bit-exact match under contention */
    printf("FP64 under contention: thread=%.12f single=%.12f -> %s\n",
           r[0], single, fp_ok ? "MATCH" : "MISMATCH (FR=1 save/restore broken)");

    int pass = nan2008 && fp_ok;
    printf("%s\n", pass ? "FP64/NAN2008 ON-DEVICE TEST PASSED" : "TEST FAILED");
    return pass ? 0 : 1;
}
