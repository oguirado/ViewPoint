/* Syscall witness compiled only into the private offline fixture. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/openat2.h>
#include <sched.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/prctl.h>
#include <sys/ptrace.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <unistd.h>

static int denied(int result, int expected, const char *label)
{
    if (result < 0 && errno == expected) {
        printf("%s errno=%d\n", label, errno);
        return 0;
    }
    fprintf(stderr, "%s returned %d errno=%d, expected errno=%d\n",
            label, result, errno, expected);
    return 1;
}

int main(int argc, char *argv[])
{
    if (argc == 2 && strcmp(argv[1], "baseline") == 0)
        return ptrace(PTRACE_TRACEME, 0, NULL, NULL) == 0 ? 0 : 1;
    if (argc != 2)
        return 2;
    struct open_how how = {O_RDONLY, 0, RESOLVE_BENEATH};
    int workspace = open("/work", O_PATH | O_DIRECTORY);
    if (workspace < 0)
        return 2;

    int failed = getuid() != 10000U + (unsigned)(argv[1][0] - '0') ||
                 getgid() != getuid() || getgroups(0, NULL) != 0 ||
                 prctl(PR_GET_NO_NEW_PRIVS, 0L, 0L, 0L, 0L) != 1 ||
                 prctl(PR_GET_SECCOMP, 0L, 0L, 0L, 0L) != 2;
    if (failed)
        fputs("lane credential check failed\n", stderr);
    failed |= denied(syscall(SYS_openat2, workspace, "../ungranted", &how, sizeof(how)),
                     EXDEV, "openat2 traversal");
    failed |= denied(open("/work/escape-link", O_RDONLY), ENOENT, "open symlink");
    failed |= denied(open("/proc/1/root/ungranted", O_RDONLY), ENOENT, "open proc");
    failed |= denied(ptrace(PTRACE_TRACEME, 0, NULL, NULL), EPERM, "ptrace");
    failed |= denied(open("/sys/fs/cgroup/cgroup.procs", O_WRONLY), ENOENT, "cgroup move");
    failed |= denied(mount("/work", "/tmp", NULL, MS_BIND, NULL), EPERM, "bind mount");
    failed |= denied(link("/work/../ungranted", "/work/linked"), ENOENT, "hardlink");
    failed |= denied(rename("/work/../ungranted", "/work/stolen"), EXDEV, "rename");
    failed |= denied(unshare(CLONE_NEWUSER), EPERM, "user namespace");
    failed |= denied(socket(AF_VSOCK, SOCK_STREAM, 0), EPERM, "vsock");
    failed |= denied(open("/etc/unauthorized", O_CREAT | O_WRONLY, 0600), EROFS, "root write");
    failed |= denied(open("/objects/unauthorized", O_CREAT | O_WRONLY, 0600), EROFS, "Git object store write");
    failed |= denied(open("/work/.git/objects/info/alternates", O_WRONLY),
                     EACCES, "Git alternates write");
    failed |= denied(rename("/work/.git/objects/info/alternates", "/work/moved-alternates"),
                     EBUSY, "Git alternates rename");
    return failed != 0;
}
