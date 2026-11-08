/* The LED output's two hot loops in C, for Linux (the Raspberry Pi), called through ctypes, which lets
   go of the interpreter lock for each call.

   tl_send_paced: the frame transmit loop of output/fpga.py. The Python loop makes 200-odd sendto()
   calls a frame, and each of them takes the interpreter lock back from the render and packing threads;
   on a Pi 4 that held the sign near 28 frames a second whatever its setting. Here the whole frame goes
   without the lock: one sendmmsg() per burst, an absolute-time sleep for each gap. Same pacing as
   FpgaDriver._transmit.

   tl_pack: hub75/pack.py's Packer.pack, the same bytes (tests/test_hub75_native.py checks), in a
   fraction of the time and without the lock.

   Built by scripts/install_pi.sh:  cc -O2 -shared -fPIC -o _native-<machine>.so native.c
   (native.py loads it; without it the Python code runs.) */
#define _GNU_SOURCE
#include <errno.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdint.h>
#include <linux/sockios.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <time.h>

#define MAX_BURST 64
#define BACKLOG_GIVE_UP_NS 20000000     /* a queue that does not drain in this long is not waited for */
#define BACKLOG_POLL_NS 20000

static int64_t now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (int64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}

static void sleep_until(int64_t t) {
    struct timespec ts = {(time_t)(t / 1000000000), (long)(t % 1000000000)};
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR) {
    }
}

int tl_abi(void) { return 4; }

/* Bytes of this socket's datagrams the system has not finished sending (SIOCOUTQ: still in the queue or
   the network card's ring). */
static int unsent(int fd) {
    int q = 0;
    return ioctl(fd, SIOCOUTQ, &q) == 0 ? q : 0;
}

/* Datagram i is lengths[i] bytes at buf + i * stride. They go to ip:port (both in network byte order)
   in groups of `burst`; before each group, wait until gap_ns after the previous group's last send (no
   wait when gap_ns is 0). *last_ns is that time (CLOCK_MONOTONIC), carried from the call before (0:
   none), so a frame's first group keeps its distance from the last frame's last one too. *min_gap_ns
   gets the shortest gap kept, if shorter or < 0. Returns 0, or -errno of a failed send. A full socket
   buffer is waited out, as in the Python loop.

   A gap kept here is only a gap on the wire if the system sends each group when it gets it. On a busy
   Raspberry Pi it sometimes holds packets back (its send completions wait for a core) and then lets
   several groups go at once, more than the card's receive FIFO takes (11 packets): a frame lost. So
   when the last group has not left by the time the gap is over, wait until it has, then a whole gap
   more; *backlogs counts those waits. */
int tl_send_paced(int fd, const uint8_t *buf, int64_t stride, const uint32_t *lengths, int n,
                  uint32_t ip, uint16_t port, int burst, int64_t gap_ns, int64_t *last_ns,
                  int64_t *min_gap_ns, int64_t *backlogs) {
    struct sockaddr_in addr;
    struct mmsghdr msgs[MAX_BURST];
    struct iovec iov[MAX_BURST];
    int64_t last = *last_ns;

    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = port;
    addr.sin_addr.s_addr = ip;
    if (burst < 1 || burst > MAX_BURST)
        burst = MAX_BURST;

    for (int i = 0; i < n; i += burst) {
        int k = n - i < burst ? n - i : burst;
        if (last && gap_ns > 0) {
            sleep_until(last + gap_ns);
            if (unsent(fd) > 0) {
                ++*backlogs;
                int64_t give_up = now_ns() + BACKLOG_GIVE_UP_NS;
                while (unsent(fd) > 0 && now_ns() < give_up)
                    sleep_until(now_ns() + BACKLOG_POLL_NS);
                sleep_until(now_ns() + gap_ns);
            }
            int64_t gap = now_ns() - last;
            if (*min_gap_ns < 0 || gap < *min_gap_ns)
                *min_gap_ns = gap;
        }
        memset(msgs, 0, sizeof(msgs[0]) * k);
        for (int j = 0; j < k; j++) {
            iov[j].iov_base = (void *)(buf + (int64_t)(i + j) * stride);
            iov[j].iov_len = lengths[i + j];
            msgs[j].msg_hdr.msg_name = &addr;
            msgs[j].msg_hdr.msg_namelen = sizeof addr;
            msgs[j].msg_hdr.msg_iov = &iov[j];
            msgs[j].msg_hdr.msg_iovlen = 1;
        }
        for (int done = 0; done < k;) {
            int r = sendmmsg(fd, msgs + done, (unsigned)(k - done), 0);
            if (r >= 0) {
                done += r;
            } else if (errno == EAGAIN || errno == EWOULDBLOCK) {
                struct pollfd p = {fd, POLLOUT, 0};
                poll(&p, 1, 1);
            } else if (errno != EINTR) {
                *last_ns = now_ns();
                return -errno;
            }
        }
        *last_ns = last = now_ns();
    }
    return 0;
}

/* Swap the off-diagonal 1x1, 2x2 and 4x4 blocks of the 8 x 8 bit matrix in x (byte i = row i): byte m of
   the result holds bit m of every byte. Hacker's Delight "transpose8", as pack.py's _ROUNDS. */
static inline uint64_t transpose8(uint64_t x) {
    uint64_t t;
    t = ((x >> 7) ^ x) & 0x00AA00AA00AA00AAull;
    x ^= t ^ (t << 7);
    t = ((x >> 14) ^ x) & 0x0000CCCC0000CCCCull;
    x ^= t ^ (t << 14);
    t = ((x >> 28) ^ x) & 0x00000000F0F0F0F0ull;
    x ^= t ^ (t << 28);
    return x;
}

#define LANES 48
#define FRACTION_BITS 4

/* Packer.pack: index[(a * cols + x) * 48 + l] is where lane l of column x, row address a is in the card
   image (pack.lane_index); lut is the gamma table; err the dithering carry in lane order, or NULL for
   no dithering. out gets planes records of rows x cols x 6 bytes, plane-major (FIRMWARE.md section 5). */
void tl_pack(const uint8_t *image, const int32_t *index, const uint16_t *lut, uint16_t *err,
             int rows, int cols, int planes, uint8_t *out) {
    const int64_t plane_bytes = (int64_t)rows * cols * (LANES / 8);
    const int nbytes = (planes + 7) / 8;
    for (int64_t cell = 0; cell < (int64_t)rows * cols; cell++) {
        const int32_t *ix = index + cell * LANES;
        uint16_t v[LANES];
        if (err) {
            uint16_t *e = err + cell * LANES;
            for (int l = 0; l < LANES; l++) {
                unsigned acc = (unsigned)lut[image[ix[l]]] + e[l];
                e[l] = (uint16_t)(acc & ((1u << FRACTION_BITS) - 1));
                v[l] = (uint16_t)(acc >> FRACTION_BITS);
            }
        } else {
            for (int l = 0; l < LANES; l++)
                v[l] = (uint16_t)(((unsigned)lut[image[ix[l]]] + (1u << (FRACTION_BITS - 1))) >> FRACTION_BITS);
        }
        uint8_t *o = out + cell * (LANES / 8);
        for (int j = 0; j < nbytes; j++) {
            for (int q = 0; q < LANES / 8; q++) {
                uint64_t w = 0;
                for (int i = 0; i < 8; i++)
                    w |= (uint64_t)((v[8 * q + i] >> (8 * j)) & 0xFF) << (8 * i);
                w = transpose8(w);
                for (int m = 0; m < 8 && 8 * j + m < planes; m++)
                    o[(8 * j + m) * plane_bytes + q] = (uint8_t)(w >> (8 * m));
            }
        }
    }
}
