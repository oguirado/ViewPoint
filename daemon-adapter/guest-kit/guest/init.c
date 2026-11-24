/* Port of MetaViewer's x64 runtime-prototype/init.c at 6b0f398fe23c. */
#define _GNU_SOURCE
#include <errno.h>
#include <linux/vm_sockets.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/socket.h>
#include <sys/utsname.h>
#include <time.h>
#include <unistd.h>

#define PORT 19001
#define LIMIT 256
#define MAX_N 10000

static int64_t millis(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
        return -1;
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static int read_line(int fd, char out[LIMIT])
{
    int64_t start = millis();
    if (start < 0)
        return -1;
    for (size_t i = 0; i < LIMIT; i++) {
        int64_t now = millis();
        int64_t left = start + 3000 - now;
        struct pollfd input = {.fd = fd, .events = POLLIN};
        char byte;
        if (now < 0 || left <= 0 || poll(&input, 1, (int)left) <= 0 ||
            recv(fd, &byte, 1, MSG_DONTWAIT) != 1)
            return -1;
        if (byte == '\n') {
            out[i] = '\0';
            return 0;
        }
        if (byte < 32 || byte > 126)
            return -1;
        out[i] = byte;
    }
    return -1;
}

static int reply(int fd, const char *text)
{
    /* Every response fits in a single nonblocking send to the trusted host. */
    size_t size = strlen(text);
    return send(fd, text, size, MSG_NOSIGNAL | MSG_DONTWAIT) == (ssize_t)size ? 0 : -1;
}

static int read_nonce(char nonce[65])
{
    FILE *file = fopen("/proc/cmdline", "r");
    if (file == NULL)
        return -1;
    char cmdline[4096];
    int complete = fgets(cmdline, sizeof cmdline, file) != NULL && strchr(cmdline, '\n') != NULL;
    fclose(file);
    if (!complete)
        return -1;

    int found = 0;
    char *save = NULL;
    for (char *arg = strtok_r(cmdline, " \n", &save); arg != NULL; arg = strtok_r(NULL, " \n", &save)) {
        if (strncmp(arg, "mv_nonce=", 9) != 0)
            continue;
        const char *value = arg + 9;
        if (found || strlen(value) != 64 || strspn(value, "0123456789abcdef") != 64)
            return -1;
        memcpy(nonce, value, 65);
        found = 1;
    }
    return found ? 0 : -1;
}

static int valid_n(const char *arg)
{
    size_t length = strlen(arg);
    return length >= 1 && length <= 5 && strspn(arg, "0123456789") == length &&
           (length == 1 || arg[0] != '0') && strtoul(arg, NULL, 10) <= MAX_N;
}

/* Return 1 only after acknowledging a valid STOP on a nonce-bound connection. */
static int serve_connection(int fd, const char *nonce, int *used_job)
{
    char line[LIMIT], out[LIMIT];
    int greeted = 0;
    while (read_line(fd, line) == 0) {
        char *token = strchr(line, ' ');
        if (token == NULL)
            break;
        *token++ = '\0';
        char *arg = strchr(token, ' ');
        if (arg != NULL)
            *arg++ = '\0';
        if (strcmp(token, nonce) != 0)
            break;

        int length;
        if (!greeted) {
            if (strcmp(line, "HELLO") != 0 || arg != NULL)
                break;
            length = snprintf(out, sizeof out, "HELLO %s\n", nonce);
            greeted = 1;
        } else if (strcmp(line, "PING") == 0 && arg == NULL) {
            length = snprintf(out, sizeof out, "PONG %s\n", nonce);
        } else if (strcmp(line, "UNAME") == 0 && arg == NULL) {
            struct utsname host;
            if (uname(&host) != 0)
                break;
            length = snprintf(out, sizeof out, "UNAME %s %s %s %s\n",
                              nonce, host.sysname, host.release, host.machine);
        } else if (strcmp(line, "SUMSQ") == 0 && arg != NULL && !*used_job && valid_n(arg)) {
            *used_job = 1;
            unsigned long n = strtoul(arg, NULL, 10);
            uint64_t sum = 0;
            for (uint64_t i = 1; i <= n; i++)
                sum += i * i;
            length = snprintf(out, sizeof out, "SUMSQ %s %lu %llu\n",
                              nonce, n, (unsigned long long)sum);
        } else if (strcmp(line, "STOP") == 0 && arg == NULL) {
            length = snprintf(out, sizeof out, "STOPPING %s\n", nonce);
            return length > 0 && length < LIMIT && reply(fd, out) == 0;
        } else {
            break;
        }
        if (length <= 0 || length >= LIMIT || reply(fd, out) != 0)
            break;
    }
    return 0;
}

static void halt_guest(void)
{
    reboot(RB_POWER_OFF);
    for (;;)
        pause();
}

int main(void)
{
    if (getpid() != 1)
        return 70;

    char nonce[65];
    if (mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL) != 0 ||
        read_nonce(nonce) != 0)
        halt_guest();

    int server = socket(AF_VSOCK, SOCK_STREAM | SOCK_CLOEXEC, 0);
    struct sockaddr_vm address = {
        .svm_family = AF_VSOCK, .svm_cid = VMADDR_CID_ANY, .svm_port = PORT,
    };
    if (server < 0 || bind(server, (struct sockaddr *)&address, sizeof address) != 0 ||
        listen(server, 1) != 0)
        halt_guest();

    int used_job = 0;
    for (;;) {
        int guest = accept4(server, NULL, NULL, SOCK_CLOEXEC);
        if (guest < 0) {
            if (errno == EINTR)
                continue;
            halt_guest();
        }
        int stop = serve_connection(guest, nonce, &used_job);
        close(guest);
        if (stop) {
            close(server);
            halt_guest();
        }
    }
}
