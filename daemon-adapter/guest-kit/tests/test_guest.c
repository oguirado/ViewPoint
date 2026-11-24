#define main guest_pid_one
#include "init.c"
#undef main

#include <assert.h>
#include <pthread.h>

static const char nonce[] = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
static const char wrong[] = "f123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

struct connection {
    int server;
    int client;
    int *used_job;
    int stopped;
    pthread_t thread;
};

static void *serve(void *data)
{
    struct connection *c = data;
    c->stopped = serve_connection(c->server, nonce, c->used_job);
    assert(close(c->server) == 0);
    return NULL;
}

static void connect_pair(struct connection *c, int *used_job)
{
    int sockets[2];
    assert(socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, sockets) == 0);
    c->client = sockets[0];
    c->server = sockets[1];
    c->used_job = used_job;
    assert(pthread_create(&c->thread, NULL, serve, c) == 0);
}

static void exchange(struct connection *c, const char *request, const char *expected)
{
    char actual[LIMIT];
    assert(reply(c->client, request) == 0);
    assert(read_line(c->client, actual) == 0);
    assert(strcmp(actual, expected) == 0);
}

static void command(struct connection *c, const char *op, const char *response, const char *args)
{
    char request[LIMIT], expected[LIMIT];
    snprintf(request, sizeof request, "%s %s%s\n", op, nonce, args);
    snprintf(expected, sizeof expected, "%s %s", response, nonce);
    exchange(c, request, expected);
}

static void close_pair(struct connection *c, int stopped)
{
    assert(close(c->client) == 0);
    assert(pthread_join(c->thread, NULL) == 0);
    assert(c->stopped == stopped);
}

static void reject(int *used, int greet, const char *frame)
{
    struct connection c;
    connect_pair(&c, used);
    if (greet)
        command(&c, "HELLO", "HELLO", "");
    assert(reply(c.client, frame) == 0);
    struct pollfd readable = {.fd = c.client, .events = POLLIN};
    assert(poll(&readable, 1, 1000) == 1);
    char byte;
    ssize_t n = read(c.client, &byte, 1);
    assert(n == 0 || (n == -1 && errno == ECONNRESET));
    close_pair(&c, 0);
}

int main(void)
{
    assert(guest_pid_one() == 70);
    int used = 0;
    char frame[2 * LIMIT];
    snprintf(frame, sizeof frame, "HELLO %s\n", wrong);
    reject(&used, 0, frame);
    snprintf(frame, sizeof frame, "STOP %s\n", nonce);
    reject(&used, 0, frame);
    snprintf(frame, sizeof frame, "EXEC %s id\n", nonce);
    reject(&used, 1, frame);
    snprintf(frame, sizeof frame, "SUMSQ %s 10001\n", nonce);
    reject(&used, 1, frame);
    snprintf(frame, sizeof frame, "SUMSQ %s 01\n", nonce);
    reject(&used, 1, frame);
    memset(frame, 'X', LIMIT);
    strcpy(frame + LIMIT, "\n");
    reject(&used, 0, frame);
    assert(used == 0);

    struct connection c;
    connect_pair(&c, &used);
    command(&c, "HELLO", "HELLO", "");
    command(&c, "PING", "PONG", "");
    struct utsname host;
    assert(uname(&host) == 0);
    char expected[2 * LIMIT];
    snprintf(expected, sizeof expected, "UNAME %s %s %s %s",
             nonce, host.sysname, host.release, host.machine);
    snprintf(frame, sizeof frame, "UNAME %s\n", nonce);
    exchange(&c, frame, expected);
    snprintf(frame, sizeof frame, "SUMSQ %s 10000\n", nonce);
    snprintf(expected, sizeof expected, "SUMSQ %s 10000 333383335000", nonce);
    exchange(&c, frame, expected);
    close_pair(&c, 0);
    assert(used == 1);

    snprintf(frame, sizeof frame, "SUMSQ %s 0\n", nonce);
    reject(&used, 1, frame);
    int zero = 0;
    connect_pair(&c, &zero);
    command(&c, "HELLO", "HELLO", "");
    snprintf(expected, sizeof expected, "SUMSQ %s 0 0", nonce);
    exchange(&c, frame, expected);
    close_pair(&c, 0);

    connect_pair(&c, &used);
    command(&c, "HELLO", "HELLO", "");
    command(&c, "STOP", "STOPPING", "");
    close_pair(&c, 1);
    puts("C guest protocol PASS");
    return 0;
}
