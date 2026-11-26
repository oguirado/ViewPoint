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
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/syscall.h>
#include <unistd.h>

static int check(int result, int expected, const char *label)
{
    if (expected ? result < 0 && errno == expected : result >= 0) {
        printf("%s errno=%d\n", label, expected);
        return 0;
    }
    fprintf(stderr, "%s returned %d errno=%d, expected errno=%d\n",
            label, result, errno, expected);
    return 1;
}

static int check_fd(int fd, int expected, const char *label)
{
    int failed = check(fd, expected, label);
    if (fd >= 0)
        close(fd);
    return failed;
}

static int check_readonly(const char *path)
{
    struct statvfs status;
    if (statvfs(path, &status) == 0 && (status.f_flag & ST_RDONLY)) {
        printf("%s read-only mount\n", path);
        return 0;
    }
    fprintf(stderr, "%s read-only mount missing\n", path);
    return 1;
}

int main(int argc, char *argv[])
{
    int baseline = argc == 4 && strcmp(argv[1], "baseline") == 0;
    if (!baseline && (argc != 2 || (strcmp(argv[1], "0") && strcmp(argv[1], "1"))))
        return 2;
    struct open_how how = {.flags = O_RDONLY};
    int workspace = open(".", O_PATH | O_DIRECTORY);
    if (workspace < 0)
        return 2;

    int failed = !baseline &&
        (getuid() != 10000U + (unsigned)(argv[1][0] - '0') ||
         getgid() != getuid() || getgroups(0, NULL) != 0 ||
         prctl(PR_GET_NO_NEW_PRIVS, 0L, 0L, 0L, 0L) != 1 ||
         prctl(PR_GET_SECCOMP, 0L, 0L, 0L, 0L) != 2);
    if (failed)
        fputs("lane credential check failed\n", stderr);
    int missing = baseline ? 0 : ENOENT;
    int blocked = baseline ? 0 : EPERM;
    failed |= check_fd(syscall(SYS_openat2, workspace, "../ungranted", &how, sizeof(how)),
                       missing, "openat2 traversal");
    failed |= check_fd(open("escape-link", O_RDONLY), missing, "open symlink");
    failed |= check_fd(open(baseline ? argv[2] : "/proc/1/root/ungranted", O_RDONLY),
                       missing, "open proc");
    failed |= check(ptrace(PTRACE_TRACEME, 0, NULL, NULL), blocked, "ptrace");
    failed |= check_fd(open(baseline ? argv[3] : "/sys/fs/cgroup/cgroup.procs", O_WRONLY),
                       missing, "cgroup write open");
    const char *target = baseline ? "mount-target" : "/tmp";
    if (baseline && mkdirat(workspace, target, 0700))
        return 2;
    int mounted = mount(".", target, NULL, MS_BIND, NULL);
    failed |= check(mounted, blocked, "bind mount");
    if (!mounted && umount(target))
        return 2;
    if (baseline && unlinkat(workspace, target, AT_REMOVEDIR))
        return 2;
    failed |= check(link("../ungranted", "linked"), missing, "hardlink");
    if (baseline && unlink("linked"))
        return 2;
    failed |= check(rename("../ungranted", "stolen"), baseline ? 0 : EXDEV, "rename");
    if (baseline && rename("stolen", "../ungranted"))
        return 2;
    failed |= check_fd(socket(AF_VSOCK, SOCK_STREAM, 0), blocked, "vsock");
    failed |= check(unshare(CLONE_NEWUSER), blocked, "user namespace");
    if (!baseline) {
        const char *alternate = "/work/.git/objects/info/alternates";
        failed |= check_readonly("/");
        failed |= check_readonly("/etc");
        failed |= check_readonly("/objects");
        failed |= check_readonly(alternate);
        failed |= check_fd(open("/etc/unauthorized", O_CREAT | O_WRONLY, 0600), EROFS, "root write");
        failed |= check_fd(open("/objects/unauthorized", O_CREAT | O_WRONLY, 0600),
                           EROFS, "Git object store write");
        failed |= check_fd(open(alternate, O_WRONLY), EACCES, "Git alternates write");
        failed |= check(rename(alternate, "/work/moved-alternates"), EBUSY, "Git alternates rename");
    }
    close(workspace);
    return failed;
}
